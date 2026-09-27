"""候補の状態管理（Phase 1d: verdict / human_rating / list）のテスト。

画像生成APIは呼びません（これらの操作はAPIを使いません）。
"""

from __future__ import annotations

import json

import pytest

from src import image_processor as ip
from src import variants as vr
from tests.conftest import make_character


def _make_variant(cfg, sid, vid):
    path = vr.variant_dir(cfg, sid) / f"{vid}.png"
    ip.save_png(make_character((512, 512)), path)
    return path


def _state(cfg) -> dict:
    return json.loads(cfg.variants_path.read_text(encoding="utf-8"))


def _verdicts(cfg, sid="001") -> dict:
    return {v["variant_id"]: v["verdict"] for v in _state(cfg)["stickers"][sid]["variants"]}


def _setup(cfg, adopted="v003"):
    """v001(pending) / v002(rejected) / v003(adopted・採用中) を用意します。"""
    items = []
    for vid, verdict in (("v001", "pending"), ("v002", "rejected"), ("v003", "adopted")):
        _make_variant(cfg, "001", vid)
        items.append({"variant_id": vid, "file": f"output/variants/001/{vid}.png",
                      "source": "api", "verdict": verdict, "human_rating": None, "note": ""})
    cfg.variants_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.variants_path.write_text(json.dumps({
        "schema": 1,
        "stickers": {"001": {"adopted": adopted, "adopted_at": "2026-09-26T10:00:00",
                             "next_seq": 4, "variants": items}}}, ensure_ascii=False),
        encoding="utf-8")


# --- verdict (1〜6) --------------------------------------------------------
@pytest.mark.parametrize("before,after", [
    ("pending", "rejected"),      # 1
    ("pending", "regen"),         # 2
    ("rejected", "pending"),      # 3
])
def test_verdict_can_be_changed(tmp_config, before, after):
    _setup(tmp_config)
    vr.set_verdict(tmp_config, "001", "v001", before)
    vr.set_verdict(tmp_config, "001", "v001", after)
    assert _verdicts(tmp_config)["v001"] == after
    # ほかの候補は変えない
    assert _verdicts(tmp_config)["v002"] == "rejected"


def test_verdict_of_adopted_variant_does_not_change_current(tmp_config):
    """4: 採用中の候補に rejected を付けても、現在の採用は変わらない。"""
    _setup(tmp_config, adopted="v003")
    vr.set_verdict(tmp_config, "001", "v003", "rejected")

    record = _state(tmp_config)["stickers"]["001"]
    assert record["adopted"] == "v003"                     # CURRENT は不変
    assert record["adopted_at"] == "2026-09-26T10:00:00"   # 採用日時も触らない
    assert _verdicts(tmp_config)["v003"] == "rejected"
    assert vr.get_sticker(tmp_config, "001").adopted == "v003"


def test_unknown_variant_is_not_saved(tmp_config):
    """5: 存在しない候補では保存しない。"""
    _setup(tmp_config)
    before = tmp_config.variants_path.read_text(encoding="utf-8")
    with pytest.raises(vr.VariantError, match="候補が見つかりません"):
        vr.set_verdict(tmp_config, "001", "v999", "rejected")
    with pytest.raises(vr.VariantError, match="候補が見つかりません"):
        vr.set_verdict(tmp_config, "999", "v001", "rejected")
    assert tmp_config.variants_path.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("bad", ["adopted", "ok", "", "REJECTED"])
def test_invalid_verdict_is_not_saved(tmp_config, bad):
    """6: 不正な判断（adopted を含む）では保存しない。"""
    _setup(tmp_config)
    before = tmp_config.variants_path.read_text(encoding="utf-8")
    with pytest.raises(vr.VariantError, match="指定できない判断です"):
        vr.set_verdict(tmp_config, "001", "v001", bad)
    assert tmp_config.variants_path.read_text(encoding="utf-8") == before


# --- human_rating (7〜11) --------------------------------------------------
@pytest.mark.parametrize("rating", [1, 2, 3, 4, 5])
def test_rating_is_saved(tmp_config, rating):
    _setup(tmp_config)
    vr.set_rating(tmp_config, "001", "v003", rating)
    assert _state(tmp_config)["stickers"]["001"]["variants"][2]["human_rating"] == rating


@pytest.mark.parametrize("bad", [0, 6, -1, 100])
def test_rating_out_of_range_is_rejected(tmp_config, bad):
    """8, 9: 0 や 6 は保存しない。"""
    _setup(tmp_config)
    before = tmp_config.variants_path.read_text(encoding="utf-8")
    with pytest.raises(vr.VariantError, match="1〜5"):
        vr.set_rating(tmp_config, "001", "v001", bad)
    assert tmp_config.variants_path.read_text(encoding="utf-8") == before


def test_rating_can_be_cleared(tmp_config):
    """10: None に戻せる。"""
    _setup(tmp_config)
    vr.set_rating(tmp_config, "001", "v003", 4)
    vr.set_rating(tmp_config, "001", "v003", None)
    assert _state(tmp_config)["stickers"]["001"]["variants"][2]["human_rating"] is None


def test_rating_does_not_change_anything_else(tmp_config):
    """11: 評価を変えても adopted / verdict / file は変わらない。"""
    _setup(tmp_config)
    before = _state(tmp_config)["stickers"]["001"]
    vr.set_rating(tmp_config, "001", "v002", 5)
    after = _state(tmp_config)["stickers"]["001"]

    assert after["adopted"] == before["adopted"]
    assert after["adopted_at"] == before["adopted_at"]
    assert [v["verdict"] for v in after["variants"]] == [v["verdict"] for v in before["variants"]]
    assert [v["file"] for v in after["variants"]] == [v["file"] for v in before["variants"]]


def test_rating_on_unknown_variant_is_not_saved(tmp_config):
    _setup(tmp_config)
    before = tmp_config.variants_path.read_text(encoding="utf-8")
    with pytest.raises(vr.VariantError):
        vr.set_rating(tmp_config, "001", "v999", 3)
    assert tmp_config.variants_path.read_text(encoding="utf-8") == before


# --- list (12〜15) ---------------------------------------------------------
def test_current_comes_from_sticker_adopted(tmp_config):
    """12, 13: verdict=adopted が複数あっても、CURRENT は sticker.adopted の1つだけ。"""
    _setup(tmp_config, adopted="v003")
    data = _state(tmp_config)
    data["stickers"]["001"]["variants"][0]["verdict"] = "adopted"   # 過去に採用した履歴
    tmp_config.variants_path.write_text(json.dumps(data), encoding="utf-8")

    sticker = vr.get_sticker(tmp_config, "001")
    assert sticker.adopted == "v003"
    assert sticker.adopted_variant().variant_id == "v003"
    assert [v.variant_id for v in sticker.variants if v.verdict == "adopted"] == ["v001", "v003"]


def test_list_cli_shows_current_and_states(tmp_config, capsys):
    _setup(tmp_config, adopted="v003")
    vr.set_rating(tmp_config, "001", "v002", 3)
    vr.set_verdict(tmp_config, "001", "v001", "regen")
    _write_csv(tmp_config)

    from src.main import cmd_variants_list

    assert cmd_variants_list(tmp_config, _Args(id="001")) == 0
    out = capsys.readouterr().out
    assert "001「了解！」" in out
    assert "CURRENT: v003" in out
    assert "REGEN" in out                      # 4: regen は目立つ表示
    assert "rating:3" in out
    assert out.count("CURRENT:") == 1


def test_list_cli_works_without_state_file(tmp_config, capsys):
    """14, 15: legacy 環境でも一覧でき、ファイルを作らない。"""
    for sid in ("001", "002"):
        ip.save_png(make_character((512, 512)), tmp_config.dir_generated / f"{sid}.png")
    _write_csv(tmp_config)

    from src.main import cmd_variants_list

    assert cmd_variants_list(tmp_config, _Args()) == 0
    out = capsys.readouterr().out
    assert out.count("CURRENT: v001") == 2
    assert "legacy" in out
    assert not tmp_config.variants_path.exists()
    assert not tmp_config.dir_variants.exists()


def test_list_cli_reports_missing_image(tmp_config, capsys):
    _setup(tmp_config)
    (tmp_config.dir_variants / "001" / "v002.png").unlink()
    _write_csv(tmp_config)

    from src.main import cmd_variants_list

    cmd_variants_list(tmp_config, _Args(id="001"))
    assert "(画像なし)" in capsys.readouterr().out


# --- 原子的保存 (16) -------------------------------------------------------
def test_updates_use_atomic_replace(tmp_config, monkeypatch):
    """16: 既存の save()（.tmp → replace）を通っていること。"""
    _setup(tmp_config)
    seen = []
    original = vr.save

    def spy(config, data):
        path = original(config, data)
        seen.append(path)
        assert not path.with_suffix(".json.tmp").exists()   # 一時ファイルを残さない
        return path

    monkeypatch.setattr(vr, "save", spy)
    vr.set_verdict(tmp_config, "001", "v001", "rejected")
    vr.set_rating(tmp_config, "001", "v001", 2)
    assert seen == [tmp_config.variants_path, tmp_config.variants_path]


def test_state_file_stays_valid_json_after_updates(tmp_config):
    _setup(tmp_config)
    vr.set_verdict(tmp_config, "001", "v002", "regen")
    vr.set_rating(tmp_config, "001", "v002", 5)
    data = _state(tmp_config)
    assert data["schema"] == vr.SCHEMA and data["updated_at"]
    assert data["stickers"]["001"]["variants"][1]["verdict"] == "regen"
    assert data["stickers"]["001"]["variants"][1]["human_rating"] == 5


# --- legacy 候補への操作 ---------------------------------------------------
def test_verdict_on_legacy_variant_materializes_record(tmp_config):
    """Phase 5b(C1) で変更: 記録に残す時点で、候補置き場へ実体をコピーします。"""
    import hashlib

    ip.save_png(make_character((512, 512)), tmp_config.dir_generated / "001.png")
    original = hashlib.sha1((tmp_config.dir_generated / "001.png").read_bytes()).hexdigest()
    vr.set_rating(tmp_config, "001", "v001", 5)

    record = _state(tmp_config)["stickers"]["001"]
    assert record["adopted"] == "v001"
    assert record["variants"][0]["source"] == "legacy"
    assert record["variants"][0]["file"] == "output/variants/001/v001.png"
    assert record["variants"][0]["human_rating"] == 5
    copied = tmp_config.dir_variants / "001" / "v001.png"
    assert hashlib.sha1(copied.read_bytes()).hexdigest() == original      # 中身は同じ
    assert (tmp_config.dir_generated / "001.png").exists()                # 元は動かさない


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _write_csv(cfg):
    path = cfg.root / "data" / "stickers.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "id,text,action,expression,category\n"
        "001,了解！,敬礼する,笑顔,basic\n"
        "002,ありがとう！,手を合わせる,笑顔,thanks\n",
        encoding="utf-8")
    cfg.raw["data"]["csv"] = "data/stickers.csv"
