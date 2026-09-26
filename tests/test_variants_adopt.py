"""候補の採用（Phase 1c）のテスト。

画像生成APIは一切呼びません（採用処理はAPIを使いません）。
重視する点:
  - 候補ファイルは履歴として残る（移動・削除しない）
  - 既存の退避処理・合成処理・検証処理をそのまま通る
  - 途中で失敗したら variants.json を「採用済み」にしない
"""

from __future__ import annotations

import hashlib
import json

import pytest

from src import image_processor as ip
from src import pipeline
from src import validator as vd
from src import variants as vr
from src.csv_loader import StickerEntry
from tests.conftest import make_character


def _entry(sid="001", text="了解！"):
    return StickerEntry(id=sid, text=text, action="敬礼", expression="笑顔", category="basic")


def _sha1(path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def _state(cfg) -> dict:
    return json.loads(cfg.variants_path.read_text(encoding="utf-8"))


def _make_variant(cfg, sid, vid, color=(220, 80, 80, 255)):
    path = vr.variant_dir(cfg, sid) / f"{vid}.png"
    ip.save_png(make_character((512, 512), color=color), path)
    return path


def _setup(cfg, *, adopted="v001", vids=("v001", "v002"), with_generated=True):
    """variants.json ＋ 候補PNG ＋（任意で）採用中の原画を用意します。"""
    items = []
    for i, vid in enumerate(vids):
        _make_variant(cfg, "001", vid, color=(200 + i * 10, 80, 80, 255))
        items.append({"variant_id": vid, "file": f"output/variants/001/{vid}.png",
                      "source": "api", "verdict": "adopted" if vid == adopted else "pending",
                      "human_rating": 3 if vid == "v001" else None})
    cfg.variants_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.variants_path.write_text(json.dumps({
        "schema": 1,
        "stickers": {"001": {"adopted": adopted, "adopted_at": "2026-09-26T10:00:00",
                             "next_seq": len(vids) + 1, "variants": items}}},
        ensure_ascii=False), encoding="utf-8")
    if with_generated:
        ip.save_png(make_character((512, 512), color=(10, 10, 200, 255)),
                    cfg.dir_generated / "001.png")


# --- 正常系 (1〜7) ---------------------------------------------------------
def test_adopt_updates_generated_final_and_state(tmp_config):
    _setup(tmp_config)
    variant_png = tmp_config.dir_variants / "001" / "v002.png"
    variant_sha = _sha1(variant_png)

    result = vr.adopt(tmp_config, _entry(), "v002")

    # 2) 候補は履歴として残る
    assert variant_png.exists() and _sha1(variant_png) == variant_sha
    # 3) 採用中の原画が候補と同一バイトになる
    generated = tmp_config.dir_generated / "001.png"
    assert _sha1(generated) == variant_sha
    # 4) 完成画像が作り直される
    final = tmp_config.dir_final / "001.png"
    assert final.exists() and result["final"] == final
    assert result["size_bytes"] > 0
    # 5) 既存の検証を通っている
    assert result["validation_ok"] is True
    assert vd.validate_sticker(final, tmp_config).ok
    # 6) 7) 記録の更新
    record = _state(tmp_config)["stickers"]["001"]
    assert record["adopted"] == "v002"
    assert record["adopted_at"] != "2026-09-26T10:00:00"
    assert record["adopted_at"].startswith("20")           # 既存と同じISO8601（ローカル時刻）


def test_final_is_rendered_not_copied(tmp_config):
    """final は候補のコピーではなく、既存の合成処理の結果であること。"""
    _setup(tmp_config)
    vr.adopt(tmp_config, _entry(), "v002")
    final = tmp_config.dir_final / "001.png"
    assert _sha1(final) != _sha1(tmp_config.dir_variants / "001" / "v002.png")
    with ip.load_rgba(final) as im:
        assert im.size == tmp_config.sticker_size      # LINE規格に合わせてある


# --- 既存候補からの切り替え (8〜10) ----------------------------------------
def test_switching_adopted_keeps_other_verdicts_and_files(tmp_config):
    _setup(tmp_config, adopted="v001", vids=("v001", "v002", "v003"))
    # v003 は「不採用」と人が評価済みの状態にしておく
    data = _state(tmp_config)
    data["stickers"]["001"]["variants"][2]["verdict"] = "rejected"
    tmp_config.variants_path.write_text(json.dumps(data), encoding="utf-8")

    vr.adopt(tmp_config, _entry(), "v002")

    record = _state(tmp_config)["stickers"]["001"]
    verdicts = {v["variant_id"]: v["verdict"] for v in record["variants"]}
    assert record["adopted"] == "v002"
    assert verdicts["v002"] == "adopted"
    assert verdicts["v001"] == "adopted"        # 旧候補の評価履歴は勝手に変えない
    assert verdicts["v003"] == "rejected"
    # 人が付けた評価も残る
    assert record["variants"][0]["human_rating"] == 3
    # 9) 旧候補のPNGは消さない
    assert (tmp_config.dir_variants / "001" / "v001.png").exists()


# --- archive (11, 12) ------------------------------------------------------
def test_previous_generated_is_archived_by_existing_mechanism(tmp_config):
    from src.importer import archive_dir

    _setup(tmp_config)
    old_sha = _sha1(tmp_config.dir_generated / "001.png")

    result = vr.adopt(tmp_config, _entry(), "v002")

    archived = list(archive_dir(tmp_config).glob("001_*.png"))   # 既存の退避先・命名規則
    assert len(archived) == 1
    assert _sha1(archived[0]) == old_sha
    assert result["archived"] == archived[0]


def test_adopt_uses_importer_archive_function(tmp_config, monkeypatch):
    """独自の退避処理ではなく、既存の archive_existing を呼んでいること。"""
    from src import importer

    called = []
    original = importer.archive_existing

    def spy(config, sticker_id):
        called.append(sticker_id)
        return original(config, sticker_id)

    monkeypatch.setattr(importer, "archive_existing", spy)
    _setup(tmp_config)
    vr.adopt(tmp_config, _entry(), "v002")
    assert called == ["001"]


# --- legacy (13, 14) -------------------------------------------------------
def test_adopt_from_legacy_only_environment(tmp_config):
    """variants.json が無く generated/001.png だけある状態から、新しい候補を採用できる。"""
    ip.save_png(make_character((512, 512), color=(10, 10, 200, 255)),
                tmp_config.dir_generated / "001.png")
    legacy_sha = _sha1(tmp_config.dir_generated / "001.png")
    _make_variant(tmp_config, "001", "v002")

    # legacy の v001 と、ファイルだけある v002 が見えている状態にする
    data = vr.load(tmp_config)
    record = vr.ensure_record(tmp_config, data, "001")
    vr.register_variant(tmp_config, record, "v002",
                        tmp_config.dir_variants / "001" / "v002.png", meta={})
    vr.save(tmp_config, data)

    vr.adopt(tmp_config, _entry(), "v002")

    record = _state(tmp_config)["stickers"]["001"]
    assert record["adopted"] == "v002"
    # 14) legacy の画像を variants/ へコピーしない
    assert not (tmp_config.dir_variants / "001" / "v001.png").exists()
    assert record["variants"][0]["source"] == "legacy"
    assert record["variants"][0]["file"] == "output/generated/001.png"
    # 旧原画は退避されている（中身は legacy のもの）
    from src.importer import archive_dir
    assert _sha1(list(archive_dir(tmp_config).glob("001_*.png"))[0]) == legacy_sha


def test_adopting_legacy_itself_does_not_lose_the_image(tmp_config):
    """legacy の v001（generated/001.png 自身）を採用しても、画像が消えない。"""
    ip.save_png(make_character((512, 512)), tmp_config.dir_generated / "001.png")
    before = _sha1(tmp_config.dir_generated / "001.png")

    result = vr.adopt(tmp_config, _entry(), "v001")

    assert (tmp_config.dir_generated / "001.png").exists()
    assert _sha1(tmp_config.dir_generated / "001.png") == before
    assert result["archived"] is None                  # 自分自身なので退避もしない
    assert _state(tmp_config)["stickers"]["001"]["adopted"] == "v001"
    assert (tmp_config.dir_final / "001.png").exists()


# --- 異常系 (15〜20) -------------------------------------------------------
def test_unknown_variant_is_rejected(tmp_config):
    _setup(tmp_config)
    with pytest.raises(vr.AdoptError, match="候補が見つかりません"):
        vr.adopt(tmp_config, _entry(), "v999")
    assert _state(tmp_config)["stickers"]["001"]["adopted"] == "v001"


def test_unknown_sticker_is_rejected(tmp_config):
    with pytest.raises(vr.AdoptError, match="候補がありません"):
        vr.adopt(tmp_config, _entry("999"), "v001")


def test_missing_png_is_rejected(tmp_config):
    _setup(tmp_config)
    (tmp_config.dir_variants / "001" / "v002.png").unlink()
    with pytest.raises(vr.AdoptError, match="候補の画像がありません"):
        vr.adopt(tmp_config, _entry(), "v002")
    assert _state(tmp_config)["stickers"]["001"]["adopted"] == "v001"


def test_corrupt_png_is_rejected(tmp_config):
    _setup(tmp_config)
    (tmp_config.dir_variants / "001" / "v002.png").write_bytes(b"not a png")
    with pytest.raises(vr.AdoptError, match="読めません"):
        vr.adopt(tmp_config, _entry(), "v002")
    assert _state(tmp_config)["stickers"]["001"]["adopted"] == "v001"
    # 原画も差し替わっていない
    assert _sha1(tmp_config.dir_generated / "001.png") != \
        _sha1(tmp_config.dir_variants / "001" / "v001.png")


def test_render_failure_does_not_mark_as_adopted(tmp_config, monkeypatch):
    _setup(tmp_config)

    def boom(*a, **k):
        raise RuntimeError("合成に失敗しました")

    monkeypatch.setattr(pipeline, "render_final", boom)
    with pytest.raises(RuntimeError):
        vr.adopt(tmp_config, _entry(), "v002")

    assert _state(tmp_config)["stickers"]["001"]["adopted"] == "v001"


def test_validation_failure_does_not_mark_as_adopted(tmp_config, monkeypatch):
    _setup(tmp_config)

    class FakeReport:
        ok = False
        errors = [type("I", (), {"message": "サイズが上限を超えています"})()]
        warnings: list = []

    monkeypatch.setattr(vd, "validate_sticker", lambda *a, **k: FakeReport())
    with pytest.raises(vr.AdoptError, match="LINE仕様を満たしません"):
        vr.adopt(tmp_config, _entry(), "v002")

    assert _state(tmp_config)["stickers"]["001"]["adopted"] == "v001"


def test_broken_state_file_does_not_crash_adopt(tmp_config):
    """variants.json が壊れていても落ちない（legacy として扱い、候補が無ければエラー）。"""
    ip.save_png(make_character((512, 512)), tmp_config.dir_generated / "001.png")
    tmp_config.variants_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_config.variants_path.write_text("{壊れている", encoding="utf-8")

    with pytest.raises(vr.AdoptError, match="候補が見つかりません"):
        vr.adopt(tmp_config, _entry(), "v002")

    # legacy の v001 なら採用できる
    assert vr.adopt(tmp_config, _entry(), "v001")["validation_ok"] is True


# --- 失敗後の再実行 --------------------------------------------------------
def test_retry_after_render_failure_succeeds(tmp_config, monkeypatch):
    """合成に失敗して原画だけ差し替わった状態でも、もう一度実行すれば直る。"""
    _setup(tmp_config)
    original = pipeline.render_final
    monkeypatch.setattr(pipeline, "render_final",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("失敗")))
    with pytest.raises(RuntimeError):
        vr.adopt(tmp_config, _entry(), "v002")

    monkeypatch.setattr(pipeline, "render_final", original)
    result = vr.adopt(tmp_config, _entry(), "v002")

    assert result["validation_ok"] is True
    assert _state(tmp_config)["stickers"]["001"]["adopted"] == "v002"
    assert _sha1(tmp_config.dir_generated / "001.png") == \
        _sha1(tmp_config.dir_variants / "001" / "v002.png")


# --- 既存の検証結果との連動 ------------------------------------------------
def test_adoption_invalidates_previous_validation_result(tmp_config):
    """採用すると、既存の fingerprint 方式で「検証済み」が自動で外れる。"""
    _setup(tmp_config)
    vr.adopt(tmp_config, _entry(), "v002")
    reports = vd.validate_all(tmp_config)
    vd.record_result(tmp_config, len(reports), 0, 0)
    assert vd.load_result(tmp_config)["passed"] is True

    vr.adopt(tmp_config, _entry(), "v001")          # 別の候補に切り替える
    after = vd.load_result(tmp_config)
    assert after["stale"] is True and after["passed"] is False

    reports = vd.validate_all(tmp_config)           # 既存の検証をやり直せば戻る
    vd.record_result(tmp_config, len(reports), 0, 0)
    assert vd.load_result(tmp_config)["passed"] is True
