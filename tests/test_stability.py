"""Phase 5b：安定化のための回帰テスト。

総点検で指摘された問題（C1/C2/H1/H2/H3/M1/M2）を再現し、修正後に再発しないよう固定します。
画像生成APIは呼びません。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time

import pytest

from src import image_processor as ip
from src import pipeline
from src import scoring
from src import validator as vd
from src import variants as vr
from src.csv_loader import StickerEntry
from tests.conftest import make_character
from tests.test_scoring import sticker_like

flask = pytest.importorskip("flask", reason="Flask が未インストールです")

ENTRY = StickerEntry(id="001", text="了解！", action="敬礼", expression="笑顔", category="basic")


def _sha(path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def _state(cfg) -> dict:
    return json.loads(cfg.variants_path.read_text(encoding="utf-8"))


def _put_variant(cfg, sid, vid, color) -> "object":
    path = vr.variant_dir(cfg, sid) / f"{vid}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    sticker_like(body=color).save(path, format="PNG")
    return path


def _legacy_with_candidates(cfg, colors=((90, 170, 220, 255), (210, 110, 140, 255))):
    """旧環境（generated だけある）＋ 候補を追加した状態を作ります。"""
    sticker_like(body=(250, 205, 60, 255)).save(cfg.dir_generated / "001.png", format="PNG")
    original = _sha(cfg.dir_generated / "001.png")
    data = vr.load(cfg)
    record = vr.ensure_record(cfg, data, "001")
    for color in colors:
        vid, path = vr.allocate_variant(cfg, record, "001")
        path.parent.mkdir(parents=True, exist_ok=True)
        sticker_like(body=color).save(path, format="PNG")
        vr.register_variant(cfg, record, vid, path, meta={})
    vr.save(cfg, data)
    return original


@pytest.fixture
def web(tmp_config):
    """CSVを用意したテスト用クライアント。"""
    from src.webapp import create_app

    csv_path = tmp_config.root / "data" / "stickers.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text("id,text,action,expression,category\n001,了解！,敬礼,笑顔,basic\n",
                        encoding="utf-8")
    tmp_config.raw["data"]["csv"] = "data/stickers.csv"
    app = create_app(tmp_config)
    app.config["TESTING"] = True
    return app.test_client()


# ===========================================================================
# C1: legacy の v001 が「採用中の画像」の別名になってはいけない
# ===========================================================================
def test_c1_legacy_variant_keeps_its_own_image(tmp_config):
    original = _legacy_with_candidates(tmp_config)

    vr.adopt(tmp_config, ENTRY, "v003")          # 別の候補を採用

    v001 = vr.get_sticker(tmp_config, "001").find("v001")
    assert _sha(v001.path(tmp_config)) == original, "v001 が採用中の画像に置き換わっている"
    assert _sha(tmp_config.dir_variants / "001" / "v003.png") != original
    assert _sha(tmp_config.dir_generated / "001.png") == \
        _sha(tmp_config.dir_variants / "001" / "v003.png")


def test_c1_readopting_legacy_restores_the_original_image(tmp_config):
    original = _legacy_with_candidates(tmp_config)
    vr.adopt(tmp_config, ENTRY, "v003")

    result = vr.adopt(tmp_config, ENTRY, "v001")   # 元に戻す

    assert _sha(tmp_config.dir_generated / "001.png") == original, "元の画像に戻っていない"
    assert result["archived"] is not None          # 直前の画像は退避される
    assert vr.get_sticker(tmp_config, "001").adopted == "v001"


def test_c1_read_only_access_still_creates_nothing(tmp_config):
    """読むだけのときは、今までどおりファイルを作らないこと（Phase 1a の約束）。"""
    sticker_like().save(tmp_config.dir_generated / "001.png", format="PNG")
    vr.get_sticker(tmp_config, "001")
    vr.list_variants(tmp_config, "001")
    vr.get_adopted(tmp_config, "001")
    assert not tmp_config.variants_path.exists()
    assert not tmp_config.dir_variants.exists()


# ===========================================================================
# C2: 壊れた variants.json を上書きして記録を消してはいけない
# ===========================================================================
def _break_state(cfg):
    text = cfg.variants_path.read_text(encoding="utf-8")
    cfg.variants_path.write_text(text[:-40], encoding="utf-8")   # 末尾が欠けた状態


@pytest.mark.parametrize("operation", ["rate", "verdict", "adopt", "score"])
def test_c2_writes_are_refused_while_state_is_corrupt(tmp_config, operation):
    _legacy_with_candidates(tmp_config)
    vr.set_rating(tmp_config, "001", "v002", 5)
    before = tmp_config.variants_path.read_text(encoding="utf-8")
    _break_state(tmp_config)
    broken = tmp_config.variants_path.read_text(encoding="utf-8")

    with pytest.raises(vr.VariantError, match="壊れて"):
        if operation == "rate":
            vr.set_rating(tmp_config, "001", "v001", 3)
        elif operation == "verdict":
            vr.set_verdict(tmp_config, "001", "v001", "rejected")
        elif operation == "adopt":
            vr.adopt(tmp_config, ENTRY, "v002")
        else:
            scoring.score_all(tmp_config, ["001"], force=True)

    # 壊れたファイルは勝手に書き換えない（人が直せるように残す）
    assert tmp_config.variants_path.read_text(encoding="utf-8") == broken
    assert before != broken


def test_c2_quarantine_keeps_the_broken_file_and_allows_restart(tmp_config):
    _legacy_with_candidates(tmp_config)
    _break_state(tmp_config)

    moved = vr.quarantine_corrupt_state(tmp_config)

    assert moved is not None and moved.exists() and "corrupt" in moved.name
    assert not tmp_config.variants_path.exists()
    vr.set_rating(tmp_config, "001", "v001", 3)      # 退避後は書ける
    assert _state(tmp_config)["stickers"]["001"]["variants"][0]["human_rating"] == 3


def test_c2_api_reports_corrupt_state(web, tmp_config):
    _legacy_with_candidates(tmp_config)
    _break_state(tmp_config)
    broken = tmp_config.variants_path.read_text(encoding="utf-8")

    r = web.post("/api/variants/001/v002/rating", json={"rating": 3})
    assert r.status_code in (409, 400)
    assert "壊れて" in r.get_json()["error"]
    assert tmp_config.variants_path.read_text(encoding="utf-8") == broken
    assert web.get("/api/variants/001").status_code == 200      # 読み取りは続けられる


# ===========================================================================
# H1: 同時書き込みで更新が消えてはいけない
# ===========================================================================
def test_h1_rating_during_adoption_is_not_lost(tmp_config, monkeypatch):
    _legacy_with_candidates(tmp_config)
    original_render = pipeline.render_final

    def render_with_interruption(*args, **kwargs):
        # 採用の合成中に、GUIの別リクエストが評価を保存した状況
        thread = threading.Thread(target=vr.set_rating, args=(tmp_config, "001", "v001", 4))
        thread.start()
        thread.join()
        return original_render(*args, **kwargs)

    monkeypatch.setattr(pipeline, "render_final", render_with_interruption)
    vr.adopt(tmp_config, ENTRY, "v002")

    record = _state(tmp_config)["stickers"]["001"]
    assert record["adopted"] == "v002"
    assert record["variants"][0]["human_rating"] == 4, "採用処理が古い状態で上書きしている"


def test_h1_parallel_ratings_all_survive(tmp_config):
    _legacy_with_candidates(tmp_config, colors=[(90, 170, 220, 255)] * 5)
    ids = [v.variant_id for v in vr.list_variants(tmp_config, "001")]

    threads = [threading.Thread(target=vr.set_rating, args=(tmp_config, "001", vid, 3))
               for vid in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ratings = {v["variant_id"]: v["human_rating"]
               for v in _state(tmp_config)["stickers"]["001"]["variants"]}
    assert all(ratings[vid] == 3 for vid in ids), ratings


def test_h1_double_adoption_leaves_consistent_state(tmp_config):
    """Enter連打の想定。どちらが勝っても、記録と画像が食い違わないこと。"""
    _legacy_with_candidates(tmp_config)
    results = []

    def adopt(vid):
        try:
            vr.adopt(tmp_config, ENTRY, vid)
            results.append(vid)
        except Exception as exc:  # noqa: BLE001 - 失敗してもよいが状態は壊さない
            results.append(f"{vid}:{type(exc).__name__}")

    threads = [threading.Thread(target=adopt, args=(v,)) for v in ("v002", "v003")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    sticker = vr.get_sticker(tmp_config, "001")
    adopted = sticker.adopted
    assert adopted in ("v002", "v003")
    assert _sha(tmp_config.dir_generated / "001.png") == \
        _sha(tmp_config.dir_variants / "001" / f"{adopted}.png"), \
        f"記録は {adopted} なのに generated が違う画像（{results}）"


def test_h1_batch_generation_does_not_overwrite_concurrent_changes(tmp_config, monkeypatch):
    """generate --variants の実行中に付けた評価が、バッチ終了時に消えないこと。"""
    from src.image_generator import ImageGenerator
    from src.logger import RunLogger, StateStore

    sticker_like().save(tmp_config.master_image_path.parent.mkdir(parents=True, exist_ok=True)
                        or tmp_config.master_image_path, format="PNG")
    _legacy_with_candidates(tmp_config, colors=[(90, 170, 220, 255)])

    class FakeProvider:
        def generate(self, prompt, reference_image=None, output_path=None):
            from pathlib import Path
            p = Path(output_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            sticker_like().save(p, format="PNG")
            # 生成の合間に、人が既存候補へ評価を付けた
            vr.set_rating(tmp_config, "001", "v001", 5)
            return b""

        def estimate_cost_usd(self, count):
            return 0.011 * count

    gen = ImageGenerator(tmp_config, RunLogger(tmp_config.log_path, echo=False),
                         StateStore(tmp_config.state_path))
    gen._provider = FakeProvider()
    vr.generate_variants(tmp_config, [ENTRY], 1, gen)

    record = _state(tmp_config)["stickers"]["001"]
    assert record["variants"][0]["human_rating"] == 5, "バッチが古い状態で上書きしている"
    assert len(record["variants"]) == 3      # 既存2件＋新規1件


# ===========================================================================
# H2: generated が別処理で変わったら気付けること
# ===========================================================================
def test_h2_detects_generated_changed_after_adoption(tmp_config):
    _legacy_with_candidates(tmp_config)
    vr.adopt(tmp_config, ENTRY, "v002")
    assert vr.get_sticker(tmp_config, "001").generated_mismatch is False

    # 既存の「AIで作り直す」「画像を入れる」に相当する書き換え
    time.sleep(0.01)
    sticker_like(body=(120, 200, 130, 255)).save(tmp_config.dir_generated / "001.png",
                                                 format="PNG")

    sticker = vr.get_sticker(tmp_config, "001")
    assert sticker.adopted == "v002"
    assert sticker.generated_mismatch is True, "採用画像の差し替えを検出できていない"


def test_h2_mismatch_is_reported_to_the_gui(web, tmp_config):
    _legacy_with_candidates(tmp_config)
    web.post("/api/variants/001/v002/adopt")
    assert web.get("/api/variants/001").get_json()["generated_mismatch"] is False
    assert web.get("/api/stickers").get_json()["stickers"][0]["generated_mismatch"] is False

    time.sleep(0.01)
    sticker_like(body=(120, 200, 130, 255)).save(tmp_config.dir_generated / "001.png",
                                                 format="PNG")
    assert web.get("/api/variants/001").get_json()["generated_mismatch"] is True
    assert web.get("/api/stickers").get_json()["stickers"][0]["generated_mismatch"] is True


def test_h2_no_mismatch_flag_without_adoption(tmp_config):
    """採用していないスタンプでは、この判定を出さないこと。"""
    sticker_like().save(tmp_config.dir_generated / "001.png", format="PNG")
    assert vr.get_sticker(tmp_config, "001").generated_mismatch is False


# ===========================================================================
# H3: 外部サイトからの POST を受け付けないこと
# ===========================================================================
GUI_HEADERS = {"X-Sticker-Client": "1"}


def test_h3_normal_gui_requests_still_work(web, tmp_config):
    _legacy_with_candidates(tmp_config)
    assert web.post("/api/variants/001/v002/rating", json={"rating": 3},
                    headers=GUI_HEADERS).status_code == 200
    assert web.post("/api/variants/001/v002/adopt", headers=GUI_HEADERS).status_code == 200
    assert web.get("/api/variants/001").status_code == 200      # GET は今までどおり


@pytest.mark.parametrize("kwargs", [
    {"data": "x", "content_type": "text/plain"},
    {"data": {"rating": "3"}, "content_type": "application/x-www-form-urlencoded"},
    {"data": "x", "content_type": "multipart/form-data"},
])
def test_h3_cross_site_posts_are_rejected(web, tmp_config, kwargs):
    _legacy_with_candidates(tmp_config)
    for path in ("/api/variants/001/v002/adopt", "/api/variants/001/v002/rating",
                 "/api/variants/001/v002/score", "/api/generated/archive"):
        r = web.post(path, headers={"Origin": "https://evil.example"}, **kwargs)
        assert r.status_code == 403, f"{path} が {r.status_code} で通った"
    # legacy の v001 が採用中のまま（外部からの操作で採用が動いていない）
    assert vr.get_sticker(tmp_config, "001").adopted == "v001"


def test_h3_foreign_origin_and_host_are_rejected(web, tmp_config):
    _legacy_with_candidates(tmp_config)
    r = web.post("/api/variants/001/v002/adopt",
                 headers={**GUI_HEADERS, "Origin": "https://evil.example"})
    assert r.status_code == 403
    r = web.post("/api/variants/001/v002/adopt",
                 headers={**GUI_HEADERS, "Host": "attacker.example"})
    assert r.status_code == 403
    assert vr.get_sticker(tmp_config, "001").adopted == "v001"     # 採用は動いていない


def test_h3_uploads_from_the_gui_still_work(web, tmp_config):
    import io

    from PIL import Image

    buf = io.BytesIO()
    make_character((512, 512)).save(buf, format="PNG")
    r = web.post("/api/stickers/001/upload", headers=GUI_HEADERS,
                 data={"file": (io.BytesIO(buf.getvalue()), "001.png")},
                 content_type="multipart/form-data")
    assert r.status_code == 200
    assert Image.open(tmp_config.dir_generated / "001.png").size[0] > 0


# ===========================================================================
# M1: 採用に失敗したら、画像も元に戻すこと
# ===========================================================================
def test_m1_rollback_when_rendering_fails(tmp_config, monkeypatch):
    original = _legacy_with_candidates(tmp_config)
    vr.adopt(tmp_config, ENTRY, "v002")
    adopted_sha = _sha(tmp_config.dir_generated / "001.png")
    final_sha = _sha(tmp_config.dir_final / "001.png")

    monkeypatch.setattr(pipeline, "render_final",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("合成失敗")))
    with pytest.raises(Exception):
        vr.adopt(tmp_config, ENTRY, "v003")

    assert _sha(tmp_config.dir_generated / "001.png") == adopted_sha, "原画が戻っていない"
    assert _sha(tmp_config.dir_final / "001.png") == final_sha
    assert vr.get_sticker(tmp_config, "001").adopted == "v002"
    assert vr.get_sticker(tmp_config, "001").generated_mismatch is False
    assert original  # 元画像は v001 として残っている
    assert _sha(vr.get_sticker(tmp_config, "001").find("v001").path(tmp_config)) == original


def test_m1_rollback_when_validation_fails(tmp_config, monkeypatch):
    _legacy_with_candidates(tmp_config)
    vr.adopt(tmp_config, ENTRY, "v002")
    adopted_sha = _sha(tmp_config.dir_generated / "001.png")
    final_sha = _sha(tmp_config.dir_final / "001.png")

    class FailingReport:
        ok = False
        errors = [type("I", (), {"message": "サイズ超過"})()]
        warnings: list = []

    monkeypatch.setattr(vd, "validate_sticker", lambda *a, **k: FailingReport())
    with pytest.raises(vr.AdoptError):
        vr.adopt(tmp_config, ENTRY, "v003")

    assert _sha(tmp_config.dir_generated / "001.png") == adopted_sha
    assert _sha(tmp_config.dir_final / "001.png") == final_sha, "完成画像が新候補のまま"
    assert vr.get_sticker(tmp_config, "001").adopted == "v002"


def test_m1_first_adoption_failure_leaves_no_generated(tmp_config, monkeypatch):
    """採用中の原画が無い状態で失敗した場合は、作りかけを残さないこと。"""
    _put_variant(tmp_config, "001", "v001", (90, 170, 220, 255))
    data = vr.load(tmp_config)
    record = vr.ensure_record(tmp_config, data, "001")
    vr.register_variant(tmp_config, record, "v001",
                        vr.variant_dir(tmp_config, "001") / "v001.png", meta={})
    vr.save(tmp_config, data)

    monkeypatch.setattr(pipeline, "render_final",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("合成失敗")))
    with pytest.raises(Exception):
        vr.adopt(tmp_config, ENTRY, "v001")

    assert not (tmp_config.dir_generated / "001.png").exists()
    assert vr.get_sticker(tmp_config, "001").adopted is None


# ===========================================================================
# M2: 過去の採用履歴を「現在採用中」と誤解させないこと
# ===========================================================================
def test_m2_past_adoption_is_labelled_as_history(tmp_config):
    from pathlib import Path

    app_js = (Path(__file__).resolve().parent.parent / "src" / "web" / "static" / "app.js"
              ).read_text(encoding="utf-8")
    label_line = next(line for line in app_js.splitlines() if "VERDICT_LABEL" in line)
    assert "採用中" not in label_line.split("adopted:")[1].split(",")[0], \
        "verdict=adopted を『採用中』と表示すると、CURRENT と混同される"


def test_m2_current_is_still_only_the_adopted_one(tmp_config):
    _legacy_with_candidates(tmp_config)
    vr.adopt(tmp_config, ENTRY, "v002")
    vr.adopt(tmp_config, ENTRY, "v003")

    sticker = vr.get_sticker(tmp_config, "001")
    assert sticker.adopted == "v003"
    verdicts = {v.variant_id: v.verdict for v in sticker.variants}
    assert verdicts["v002"] == "adopted" and verdicts["v003"] == "adopted"   # 履歴は残る
    assert len([v for v in sticker.variants if v.variant_id == sticker.adopted]) == 1


def test_h2_readopting_the_same_variant_fixes_the_mismatch(tmp_config):
    """差し替えに気付いたあと、同じ候補を採用し直せば直ること。"""
    _legacy_with_candidates(tmp_config)
    vr.adopt(tmp_config, ENTRY, "v002")
    time.sleep(0.01)
    sticker_like(body=(120, 200, 130, 255)).save(tmp_config.dir_generated / "001.png",
                                                 format="PNG")
    assert vr.get_sticker(tmp_config, "001").generated_mismatch is True

    vr.adopt(tmp_config, ENTRY, "v002")          # 同じ候補をもう一度採用

    sticker = vr.get_sticker(tmp_config, "001")
    assert sticker.generated_mismatch is False
    assert _sha(tmp_config.dir_generated / "001.png") == \
        _sha(tmp_config.dir_variants / "001" / "v002.png")
