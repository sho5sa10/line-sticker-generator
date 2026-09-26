"""スコアのAPIと、人の判断・採用状態との共存（Phase 4）のテスト。

実APIは使いません。scoring の計算ロジックは scoring.py のみに置き、
webapp は呼び出すだけであることも確認します。
"""

from __future__ import annotations

import hashlib
import json

import pytest

from src import image_processor as ip
from src import scoring
from src import variants as vr
from tests.conftest import make_character
from tests.test_scoring import degrade, sticker_like

flask = pytest.importorskip("flask", reason="Flask が未インストールです")


@pytest.fixture
def app(tmp_config):
    from src.webapp import create_app

    csv_path = tmp_config.root / "data" / "stickers.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text(
        "id,text,action,expression,category\n001,了解！,敬礼する,笑顔,basic\n", encoding="utf-8")
    tmp_config.raw["data"]["csv"] = "data/stickers.csv"
    application = create_app(tmp_config)
    application.config["TESTING"] = True
    return application


@pytest.fixture
def client(app):
    return app.test_client()


def _put(cfg, sid, vid, kind=None):
    img = sticker_like()
    if kind:
        img = degrade(img, kind)
    path = vr.variant_dir(cfg, sid) / f"{vid}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG")
    return path


def _state_file(cfg, vids=("v001", "v002"), adopted=None, extra=None):
    items = []
    for vid in vids:
        item = {"variant_id": vid, "file": f"output/variants/001/{vid}.png", "source": "api",
                "verdict": "pending", "human_rating": None, "note": ""}
        item.update((extra or {}).get(vid, {}))
        items.append(item)
    cfg.variants_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.variants_path.write_text(json.dumps({
        "schema": 1, "stickers": {"001": {"adopted": adopted, "adopted_at": None,
                                          "next_seq": len(vids) + 1, "variants": items}}},
        ensure_ascii=False), encoding="utf-8")


def _state(cfg) -> dict:
    return json.loads(cfg.variants_path.read_text(encoding="utf-8"))


def _sha1(path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


# --- スコアAPI -------------------------------------------------------------
def test_score_api_delegates_to_scoring(client, tmp_config, monkeypatch):
    _put(tmp_config, "001", "v001")
    _state_file(tmp_config, vids=("v001",))
    seen = {}
    original = scoring.score_variant

    def spy(config, sid, vid, **kw):
        seen["args"] = (sid, vid)
        return original(config, sid, vid, **kw)

    monkeypatch.setattr(scoring, "score_variant", spy)
    r = client.post("/api/variants/001/v001/score")
    assert r.status_code == 200 and seen["args"] == ("001", "v001")

    body = r.get_json()
    assert body["formula"] == scoring.SCORING_FORMULA
    item = body["sticker"]["variants"][0]
    assert item["derived_scores"]["quality"] == 100
    assert item["derived_scores"]["consistency"] is None       # 未評価のまま
    assert item["raw_metrics"]["contrast74"] > 0
    assert item["flags"] == []


def test_score_api_reports_gate_flags(client, tmp_config):
    _put(tmp_config, "001", "v001", kind="cropped")
    _state_file(tmp_config, vids=("v001",))
    item = client.post("/api/variants/001/v001/score").get_json()["sticker"]["variants"][0]
    assert "gate:cropped" in item["flags"]
    assert item["verdict"] == "pending"          # Gate でも人の判断は変わらない
    assert (tmp_config.dir_variants / "001" / "v001.png").exists()


def test_score_api_errors(client, tmp_config):
    _put(tmp_config, "001", "v001")
    _state_file(tmp_config, vids=("v001", "v002"))       # v002 のPNGは無い
    assert client.post("/api/variants/001/v999/score").status_code == 400
    assert client.post("/api/variants/001/v002/score").status_code == 400
    assert client.get("/api/variants/001").status_code == 200   # 壊れない


def test_score_flags_appear_in_sticker_list(client, tmp_config):
    """Phase 2 の一覧API（GUIの⚠バッジ）にスコアのflagsが出ること。"""
    _put(tmp_config, "001", "v001", kind="fringe")
    _state_file(tmp_config, vids=("v001",))
    assert client.get("/api/stickers").get_json()["stickers"][0]["flags"] == []

    client.post("/api/variants/001/v001/score")
    row = client.get("/api/stickers").get_json()["stickers"][0]
    assert "warn:fringe" in row["flags"]


# --- 人の判断・採用との共存 ------------------------------------------------
def test_scoring_keeps_rating_verdict_and_adopted(client, tmp_config):
    png = _put(tmp_config, "001", "v001")
    ip.save_png(make_character((512, 512)), tmp_config.dir_generated / "001.png")
    ip.save_png(make_character((370, 320)), tmp_config.dir_final / "001.png")
    _state_file(tmp_config, vids=("v001",), adopted="v001",
                extra={"v001": {"verdict": "rejected", "human_rating": 5}})
    gen, fin = tmp_config.dir_generated / "001.png", tmp_config.dir_final / "001.png"
    before = (_sha1(png), _sha1(gen), gen.stat().st_mtime_ns, _sha1(fin), fin.stat().st_mtime_ns)

    client.post("/api/variants/001/v001/score")

    record = _state(tmp_config)["stickers"]["001"]
    assert record["adopted"] == "v001"
    assert record["variants"][0]["verdict"] == "rejected"
    assert record["variants"][0]["human_rating"] == 5
    assert (_sha1(png), _sha1(gen), gen.stat().st_mtime_ns,
            _sha1(fin), fin.stat().st_mtime_ns) == before


def test_rating_after_scoring_keeps_scores(client, tmp_config):
    _put(tmp_config, "001", "v001")
    _state_file(tmp_config, vids=("v001",))
    client.post("/api/variants/001/v001/score")
    scores = _state(tmp_config)["stickers"]["001"]["variants"][0]["derived_scores"]

    client.post("/api/variants/001/v001/rating", json={"rating": 4})
    client.post("/api/variants/001/v001/verdict", json={"verdict": "regen"})
    item = _state(tmp_config)["stickers"]["001"]["variants"][0]
    assert item["derived_scores"] == scores and item["human_rating"] == 4
    assert item["verdict"] == "regen"


# --- 再現性 ----------------------------------------------------------------
def test_rescoring_is_reproducible(client, tmp_config):
    _put(tmp_config, "001", "v001", kind="fringe")
    _state_file(tmp_config, vids=("v001",))
    first = client.post("/api/variants/001/v001/score").get_json()["sticker"]["variants"][0]
    second = client.post("/api/variants/001/v001/score").get_json()["sticker"]["variants"][0]

    assert first["raw_metrics"] == second["raw_metrics"]
    assert first["flags"] == second["flags"]
    assert {k: v for k, v in first["derived_scores"].items() if k != "scored_at"} == \
           {k: v for k, v in second["derived_scores"].items() if k != "scored_at"}


# --- warn:thin の検証（Phase 4 で確認した内容をテストに固定） ----------------
def _line_art(tmp_path, width):
    from PIL import Image, ImageDraw

    size = 512
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = (40, 30, 25, 255)
    d.ellipse((size * .2, size * .15, size * .8, size * .85), outline=c, width=width)
    d.ellipse((size * .36, size * .38, size * .44, size * .46), outline=c, width=width)
    d.arc((size * .42, size * .52, size * .58, size * .62), 200, 340, fill=c, width=width)
    path = tmp_path / f"line{width}.png"
    img.save(path)
    return path


def test_thin_threshold_separates_line_art_from_normal(tmp_path):
    """実データは 0.73 以上、輪郭だけの細線画像は 0.11 以下。閾値 0.60 で分かれます。"""
    normal = tmp_path / "normal.png"
    sticker_like().save(normal)
    assert scoring.measure(normal)["thin74"] > 0.60
    assert "warn:thin" not in scoring.evaluate(scoring.measure(normal))[1]

    for width in (2, 6, 12):
        m = scoring.measure(_line_art(tmp_path, width))
        assert m["thin74"] < 0.60, (width, m["thin74"])
