"""候補のWeb API（Phase 2）のテスト。

状態を変える処理は webapp に書かず、既存の variants.py に委ねていることも確認します。
画像生成APIは呼びません。
"""

from __future__ import annotations

import json

import pytest

from src import image_processor as ip
from src import variants as vr
from tests.conftest import make_character

flask = pytest.importorskip("flask", reason="Flask が未インストールです")


@pytest.fixture
def app(tmp_config):
    from src.webapp import create_app

    csv_path = tmp_config.root / "data" / "stickers.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text(
        "id,text,action,expression,category\n"
        "001,了解！,敬礼する,自信のある笑顔,basic\n"
        "002,ありがとう！,手を合わせる,嬉しそうな笑顔,thanks\n",
        encoding="utf-8")
    tmp_config.raw["data"]["csv"] = "data/stickers.csv"
    application = create_app(tmp_config)
    application.config["TESTING"] = True
    return application


@pytest.fixture
def client(app):
    return app.test_client()


def _make_variants(cfg, sid="001", vids=("v001", "v002", "v003"), adopted=None):
    items = []
    for vid in vids:
        ip.save_png(make_character((512, 512)), vr.variant_dir(cfg, sid) / f"{vid}.png")
        items.append({"variant_id": vid, "file": f"output/variants/{sid}/{vid}.png",
                      "source": "api", "verdict": "pending", "human_rating": None, "note": ""})
    cfg.variants_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.variants_path.write_text(json.dumps({
        "schema": 1, "stickers": {sid: {"adopted": adopted, "adopted_at": None,
                                        "next_seq": len(vids) + 1, "variants": items}}},
        ensure_ascii=False), encoding="utf-8")


def _state(cfg) -> dict:
    return json.loads(cfg.variants_path.read_text(encoding="utf-8"))


# --- 読み取り (1, 2) -------------------------------------------------------
def test_list_and_detail(client, tmp_config):
    _make_variants(tmp_config, adopted="v002")
    body = client.get("/api/variants").get_json()
    assert body["stickers"]["001"]["variant_count"] == 3
    assert body["stickers"]["001"]["adopted"] == "v002"

    one = client.get("/api/variants/001").get_json()
    assert [v["variant_id"] for v in one["variants"]] == ["v001", "v002", "v003"]
    assert one["adopted"] == "v002"


def test_sticker_list_carries_variant_count(client, tmp_config):
    """一覧のセル用に候補数・採用中が載ること（追加のAPI呼び出しを増やさないため）。"""
    _make_variants(tmp_config, adopted="v001")
    rows = {s["id"]: s for s in client.get("/api/stickers").get_json()["stickers"]}
    assert rows["001"]["variant_count"] == 3 and rows["001"]["adopted"] == "v001"
    assert rows["002"]["variant_count"] == 0 and rows["002"]["adopted"] is None
    assert rows["002"]["flags"] == []


def test_variant_image_is_served(client, tmp_config):
    _make_variants(tmp_config)
    assert client.get("/img/variants/001/v002.png").status_code == 200
    assert client.get("/img/variants/001/v999.png").status_code == 404
    assert client.get("/img/variants/001/..%2F..%2Fstate.png").status_code == 404


# --- 採用 (3) --------------------------------------------------------------
def test_adopt_api_delegates_to_variants_adopt(client, tmp_config, monkeypatch):
    _make_variants(tmp_config)
    called = {}
    original = vr.adopt

    def spy(config, entry, variant_id, **kw):
        called["args"] = (entry.id, variant_id)
        return original(config, entry, variant_id, **kw)

    monkeypatch.setattr(vr, "adopt", spy)
    r = client.post("/api/variants/001/v002/adopt")
    assert r.status_code == 200
    assert called["args"] == ("001", "v002")

    body = r.get_json()
    assert body["sticker"]["adopted"] == "v002"
    assert body["final_size_kb"] > 0
    assert "validation" in body and "packages_status" in body
    # 既存の経路を通っているので、採用中の原画と完成画像ができている
    assert (tmp_config.dir_generated / "001.png").read_bytes() == \
        (tmp_config.dir_variants / "001" / "v002.png").read_bytes()
    assert (tmp_config.dir_final / "001.png").exists()


def test_adopt_api_failure_keeps_state(client, tmp_config):
    _make_variants(tmp_config, adopted="v001")
    (tmp_config.dir_variants / "001" / "v003.png").write_bytes(b"not a png")
    r = client.post("/api/variants/001/v003/adopt")
    assert r.status_code == 400 and "読めません" in r.get_json()["error"]
    assert _state(tmp_config)["stickers"]["001"]["adopted"] == "v001"


# --- verdict (4) -----------------------------------------------------------
def test_verdict_api_delegates(client, tmp_config, monkeypatch):
    _make_variants(tmp_config, adopted="v001")
    seen = {}
    original = vr.set_verdict

    def spy(config, sid, vid, verdict, **kw):
        seen["args"] = (sid, vid, verdict)
        return original(config, sid, vid, verdict, **kw)

    monkeypatch.setattr(vr, "set_verdict", spy)
    r = client.post("/api/variants/001/v003/verdict", json={"verdict": "rejected"})
    assert r.status_code == 200 and seen["args"] == ("001", "v003", "rejected")
    sticker = r.get_json()["sticker"]
    assert sticker["adopted"] == "v001"          # 採用中は変わらない
    assert sticker["variants"][2]["verdict"] == "rejected"


def test_verdict_api_rejects_adopted_and_unknown(client, tmp_config):
    _make_variants(tmp_config, adopted="v001")
    assert client.post("/api/variants/001/v002/verdict",
                       json={"verdict": "adopted"}).status_code == 400
    assert client.post("/api/variants/001/v002/verdict", json={}).status_code == 400
    assert _state(tmp_config)["stickers"]["001"]["variants"][1]["verdict"] == "pending"


# --- rating (5, 8) ---------------------------------------------------------
def test_rating_api_delegates(client, tmp_config, monkeypatch):
    _make_variants(tmp_config)
    seen = {}
    original = vr.set_rating

    def spy(config, sid, vid, rating, **kw):
        seen["args"] = (sid, vid, rating)
        return original(config, sid, vid, rating, **kw)

    monkeypatch.setattr(vr, "set_rating", spy)
    r = client.post("/api/variants/001/v002/rating", json={"rating": 4})
    assert r.status_code == 200 and seen["args"] == ("001", "v002", 4)
    assert r.get_json()["sticker"]["variants"][1]["human_rating"] == 4

    r = client.post("/api/variants/001/v002/rating", json={"rating": None})
    assert r.get_json()["sticker"]["variants"][1]["human_rating"] is None


@pytest.mark.parametrize("bad", [0, 6, "abc"])
def test_rating_api_rejects_out_of_range(client, tmp_config, bad):
    _make_variants(tmp_config)
    before = tmp_config.variants_path.read_text(encoding="utf-8")
    assert client.post("/api/variants/001/v002/rating", json={"rating": bad}).status_code == 400
    assert tmp_config.variants_path.read_text(encoding="utf-8") == before


# --- 不正な指定 (6, 7, 9) --------------------------------------------------
def test_unknown_sticker_and_variant(client, tmp_config):
    _make_variants(tmp_config)
    assert client.post("/api/variants/999/v001/adopt").status_code == 404
    assert client.post("/api/variants/999/v001/verdict",
                       json={"verdict": "pending"}).status_code == 400
    assert client.post("/api/variants/001/v999/adopt").status_code == 400
    assert client.post("/api/variants/001/v999/rating", json={"rating": 3}).status_code == 400


def test_state_file_stays_valid_after_failures(client, tmp_config):
    _make_variants(tmp_config, adopted="v001")
    client.post("/api/variants/001/v999/rating", json={"rating": 3})
    client.post("/api/variants/001/v002/verdict", json={"verdict": "bogus"})
    client.post("/api/variants/001/v999/adopt")
    data = _state(tmp_config)               # 壊れていない
    assert data["stickers"]["001"]["adopted"] == "v001"
    assert client.get("/api/variants/001").status_code == 200


def test_legacy_environment_is_visible_through_api(client, tmp_config):
    ip.save_png(make_character((512, 512)), tmp_config.dir_generated / "001.png")
    body = client.get("/api/variants/001").get_json()
    assert body["legacy"] is True and body["adopted"] == "v001"
    assert body["variants"][0]["source"] == "legacy"
    assert not tmp_config.variants_path.exists()      # 読むだけでは作らない
