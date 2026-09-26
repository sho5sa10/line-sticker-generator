"""候補（variant）管理の読み取り層のテスト（Phase 1a）。

ここでは生成・採用は扱いません。既存の generated/final を壊さないことを重視します。
"""

from __future__ import annotations

import json

from src import image_processor as ip
from src import variants as vr
from tests.conftest import make_character


def _add_generated(cfg, sticker_id: str):
    ip.save_png(make_character((512, 512)), cfg.dir_generated / f"{sticker_id}.png")


def _add_variant_file(cfg, sticker_id: str, variant_id: str):
    path = vr.variant_dir(cfg, sticker_id) / f"{variant_id}.png"
    ip.save_png(make_character((512, 512)), path)
    return path


def _write_state(cfg, data: dict):
    cfg.variants_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.variants_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# --- Test 1: variants.json が無い既存環境 ---------------------------------
def test_legacy_generated_image_is_seen_as_adopted_v001(tmp_config):
    _add_generated(tmp_config, "001")

    sticker = vr.get_sticker(tmp_config, "001")
    assert sticker is not None
    assert sticker.legacy is True
    assert sticker.adopted == "v001"
    assert sticker.variant_count == 1

    v = sticker.variants[0]
    assert v.variant_id == "v001"
    assert v.source == vr.SOURCE_LEGACY
    assert v.verdict == vr.VERDICT_ADOPTED
    assert v.file == "output/generated/001.png"          # 相対パスで持つ
    assert v.path(tmp_config) == tmp_config.dir_generated / "001.png"


def test_legacy_does_not_copy_files_or_create_state(tmp_config):
    _add_generated(tmp_config, "001")
    before = tmp_config.dir_generated / "001.png"
    mtime = before.stat().st_mtime_ns

    vr.get_sticker(tmp_config, "001")
    vr.list_variants(tmp_config, "001")
    vr.get_adopted(tmp_config, "001")

    assert not tmp_config.variants_path.exists()          # 勝手に作らない
    assert not tmp_config.dir_variants.exists()           # 勝手に作らない
    assert before.stat().st_mtime_ns == mtime             # 元画像に触らない


def test_no_image_and_no_state_returns_none(tmp_config):
    assert vr.get_sticker(tmp_config, "001") is None
    assert vr.list_variants(tmp_config, "001") == []
    assert vr.get_adopted(tmp_config, "001") is None


# --- Test 2 / 3: variants.json がある場合 ----------------------------------
def test_state_file_is_used_and_adopted_is_read(tmp_config):
    for vid in ("v001", "v002", "v003"):
        _add_variant_file(tmp_config, "001", vid)
    _add_generated(tmp_config, "001")     # 採用中の原画もある状態
    _write_state(tmp_config, {
        "schema": 1,
        "stickers": {
            "001": {
                "adopted": "v002",
                "adopted_at": "2026-09-26T22:05:00",
                "next_seq": 4,
                "variants": [
                    {"variant_id": "v001", "file": "output/variants/001/v001.png",
                     "source": "api", "verdict": "rejected", "human_rating": 2},
                    {"variant_id": "v002", "file": "output/variants/001/v002.png",
                     "source": "api", "verdict": "adopted", "human_rating": 4,
                     "cost_usd": 0.011},
                    {"variant_id": "v003", "file": "output/variants/001/v003.png",
                     "source": "api", "verdict": "pending"},
                ],
            }
        },
    })

    sticker = vr.get_sticker(tmp_config, "001")
    assert sticker.legacy is False
    assert [v.variant_id for v in sticker.variants] == ["v001", "v002", "v003"]
    assert sticker.adopted == "v002"
    assert sticker.next_seq == 4

    adopted = vr.get_adopted(tmp_config, "001")
    assert adopted.variant_id == "v002" and adopted.human_rating == 4
    assert adopted.exists(tmp_config)
    assert adopted.extra["cost_usd"] == 0.011            # 未知のキーも保持する
    assert sticker.find("v001").verdict == "rejected"


def test_unknown_verdict_falls_back_to_pending(tmp_config):
    _add_variant_file(tmp_config, "002", "v001")
    _write_state(tmp_config, {"stickers": {"002": {"adopted": None, "variants": [
        {"variant_id": "v001", "file": "output/variants/002/v001.png", "verdict": "bogus"}]}}})
    assert vr.list_variants(tmp_config, "002")[0].verdict == vr.VERDICT_PENDING


def test_adopted_pointing_at_missing_variant_is_treated_as_not_adopted(tmp_config):
    _add_variant_file(tmp_config, "003", "v001")
    _write_state(tmp_config, {"stickers": {"003": {"adopted": "v999", "variants": [
        {"variant_id": "v001", "file": "output/variants/003/v001.png", "verdict": "pending"}]}}})
    sticker = vr.get_sticker(tmp_config, "003")
    assert sticker.adopted is None
    assert vr.get_adopted(tmp_config, "003") is None


# --- Test 4: 壊れた variants.json -----------------------------------------
def test_broken_state_falls_back_to_legacy(tmp_config, capsys):
    _add_generated(tmp_config, "001")
    tmp_config.variants_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_config.variants_path.write_text("{壊れている", encoding="utf-8")

    sticker = vr.get_sticker(tmp_config, "001")          # 例外を投げない
    assert sticker.legacy is True and sticker.adopted == "v001"
    assert "WARNING" in capsys.readouterr().err          # 握り潰さず警告は出す


def test_wrong_shape_state_falls_back_to_legacy(tmp_config):
    _add_generated(tmp_config, "001")
    _write_state(tmp_config, {"stickers": "これは辞書ではない"})
    assert vr.get_sticker(tmp_config, "001").legacy is True


def test_record_without_readable_variants_falls_back_to_legacy(tmp_config):
    _add_generated(tmp_config, "001")
    _write_state(tmp_config, {"stickers": {"001": {"adopted": "v001", "variants": [{}, 5]}}})
    sticker = vr.get_sticker(tmp_config, "001")
    assert sticker.legacy is True and sticker.variant_count == 1


# --- Test 5: generated/ が候補画像で汚染されない ----------------------------
def test_variant_images_are_outside_generated_dir(tmp_config):
    """main/tab の自動選択が候補画像を拾わないこと（package_builder は変更しない）。"""
    from src.package_builder import pick_source_image

    _add_generated(tmp_config, "001")
    big = ip.save_png(make_character((1024, 1024)), vr.variant_dir(tmp_config, "001") / "v002.png")

    assert tmp_config.dir_variants not in tmp_config.dir_generated.parents
    assert list(tmp_config.dir_generated.glob("*.png")) == [tmp_config.dir_generated / "001.png"]
    # 候補のほうが大きくても、auto が選ぶのは generated/ の画像
    assert pick_source_image(tmp_config, "auto") == tmp_config.dir_generated / "001.png"
    assert big[0].exists()


# --- まとめ取得・保存 -------------------------------------------------------
def test_list_all_mixes_legacy_and_recorded(tmp_config):
    _add_generated(tmp_config, "001")
    _add_generated(tmp_config, "002")
    _add_variant_file(tmp_config, "002", "v001")
    _add_variant_file(tmp_config, "002", "v002")
    _write_state(tmp_config, {"stickers": {"002": {"adopted": "v002", "next_seq": 3, "variants": [
        {"variant_id": "v001", "file": "output/variants/002/v001.png", "verdict": "rejected"},
        {"variant_id": "v002", "file": "output/variants/002/v002.png", "verdict": "adopted"}]}}})

    found = vr.list_all(tmp_config, ["001", "002", "003"])
    assert set(found) == {"001", "002"}                  # 003 は画像も記録も無い
    assert found["001"].legacy is True and found["001"].variant_count == 1
    assert found["002"].legacy is False and found["002"].variant_count == 2


def test_save_is_atomic_and_round_trips(tmp_config):
    data = {"stickers": {"001": {"adopted": "v001", "next_seq": 2, "variants": [
        {"variant_id": "v001", "file": "output/variants/001/v001.png", "verdict": "adopted"}]}}}
    path = vr.save(tmp_config, data)
    assert path == tmp_config.variants_path
    assert not path.with_suffix(".json.tmp").exists()     # 一時ファイルを残さない

    loaded = vr.load(tmp_config)
    assert loaded["schema"] == vr.SCHEMA and loaded["updated_at"]
    _add_variant_file(tmp_config, "001", "v001")
    assert vr.get_adopted(tmp_config, "001").variant_id == "v001"


# --- 読み取りAPI -----------------------------------------------------------
def test_read_api_returns_legacy_and_recorded(tmp_config):
    from src.webapp import create_app

    csv_path = tmp_config.root / "data" / "stickers.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text(
        "id,text,action,expression,category\n"
        "001,了解！,敬礼する,笑顔,basic\n"
        "002,ありがとう！,手を合わせる,笑顔,thanks\n",
        encoding="utf-8",
    )
    tmp_config.raw["data"]["csv"] = "data/stickers.csv"
    _add_generated(tmp_config, "001")

    client = create_app(tmp_config).test_client()
    body = client.get("/api/variants").get_json()
    assert body["state_exists"] is False
    assert set(body["stickers"]) == {"001"}
    assert body["stickers"]["001"]["adopted"] == "v001"
    assert body["stickers"]["001"]["variants"][0]["source"] == "legacy"

    one = client.get("/api/variants/001").get_json()
    assert one["variant_count"] == 1 and one["legacy"] is True
    assert client.get("/api/variants/002").get_json()["variant_count"] == 0
    assert client.get("/api/variants/999").status_code == 404


def test_read_api_does_not_create_files(tmp_config):
    from src.webapp import create_app

    csv_path = tmp_config.root / "data" / "stickers.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text("id,text,action,expression,category\n001,了解！,敬礼,笑顔,basic\n",
                        encoding="utf-8")
    tmp_config.raw["data"]["csv"] = "data/stickers.csv"
    _add_generated(tmp_config, "001")

    client = create_app(tmp_config).test_client()
    client.get("/api/variants")
    client.get("/api/variants/001")
    assert not tmp_config.variants_path.exists()
    assert not tmp_config.dir_variants.exists()
