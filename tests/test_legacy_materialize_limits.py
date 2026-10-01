"""原画（generated/<id>.png）を候補として置く番号の上限（Phase 6-B）。

_legacy_slot は表示（get_sticker / list_all）でも使うため、上限の確認は書き込む _materialize_legacy だけで行います。
上限（VARIANT_NUMBER_MAX）を超える番号には置かず、VariantError にします（書き込む前・記録を変える前・API の前）。
表示は今までどおり例外にしません（壊れた記録1件で一覧全体が失敗しないように）。
"""

from __future__ import annotations

import json

import pytest

from src import variants as vr
from tests.conftest import make_character
from tests.test_candidate_numbering import FakeProvider, _entry, _generator
from tests.test_stability import GUI_HEADERS, web  # noqa: F401 - web はフィクスチャ

MAX = vr.VARIANT_NUMBER_MAX


def _setup(config, next_seq) -> bytes:
    """原画があり、候補0件で next_seq だけがある記録。保存した variants.json の中身を返します。"""
    original = config.dir_generated / "001.png"
    original.parent.mkdir(parents=True, exist_ok=True)
    make_character((256, 256)).save(original)
    vr.update(config, lambda s: s.setdefault("stickers", {}).update(
        {"001": {"adopted": None, "adopted_at": None, "variants": [], "next_seq": next_seq}}))
    return config.variants_path.read_bytes()


def _variant_files(config) -> list[str]:
    folder = vr.variant_dir(config, "001")
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


# ===========================================================================
# 上限を超える番号: 書き込まず・記録を変えず・API を呼ばずに VariantError
# ===========================================================================
def test_original_over_the_limit_is_rejected_before_the_api(tmp_config):
    before = _setup(tmp_config, MAX + 1)          # 採番に使える最大の next_seq（最後の番号の次）
    provider = FakeProvider()
    with pytest.raises(vr.VariantError) as excinfo:
        vr.generate_variants(tmp_config, [_entry()], 1, _generator(tmp_config, provider))
    assert not isinstance(excinfo.value, OSError)                    # FileNotFoundError ではない
    assert provider.calls == 0
    assert _variant_files(tmp_config) == []                          # 画像も一時ファイルも作らない
    assert tmp_config.variants_path.read_bytes() == before           # 記録も変えない


def test_limit_check_at_the_boundary(tmp_config, monkeypatch):
    """上限ちょうどは置ける・上限を超えたら置かない（コピーは差し替え、上限の判定だけを確かめます）。
    上限ちょうどの番号のファイル名は長く、環境によっては作れないため、コピーそのものは行いません。"""
    copied = []
    monkeypatch.setattr(vr, "_copy_verified", lambda src, dest, sha=None, **kw: copied.append(dest.stem))
    _setup(tmp_config, MAX)
    record = vr.load(tmp_config)["stickers"]["001"]
    legacy = vr.ensure_legacy_variant(tmp_config, "001", record)
    kept, _stamp = vr._materialize_legacy(tmp_config, "001", legacy.variants[0], record)
    assert kept.variant_id == f"v{MAX}" and copied == [f"v{MAX}"]

    _setup(tmp_config, MAX + 1)
    record = vr.load(tmp_config)["stickers"]["001"]
    legacy = vr.ensure_legacy_variant(tmp_config, "001", record)
    with pytest.raises(vr.VariantError):
        vr._materialize_legacy(tmp_config, "001", legacy.variants[0], record)
    assert copied == [f"v{MAX}"]                                     # 上限を超えた番号にはコピーしない


# ===========================================================================
# 正常な番号: これまでどおり
# ===========================================================================
@pytest.mark.parametrize("next_seq, expected", [(1, "v001"), (999, "v999"), (1000, "v1000")])
def test_original_within_the_limit_is_placed_as_before(tmp_config, next_seq, expected):
    _setup(tmp_config, next_seq)
    provider = FakeProvider()
    results = vr.generate_variants(tmp_config, [_entry()], 1, _generator(tmp_config, provider))
    assert provider.calls == 1 and [r["status"] for r in results] == ["generated"]
    record = json.loads(tmp_config.variants_path.read_text(encoding="utf-8"))["stickers"]["001"]
    assert record["variants"][0]["variant_id"] == expected and record["adopted"] == expected


# ===========================================================================
# 表示（get_sticker / list_all / 一覧 API）は、上限を超える記録があっても例外にしない
# ===========================================================================
def test_listing_still_works_with_a_record_over_the_limit(web, tmp_config):  # noqa: F811
    before = _setup(tmp_config, MAX + 1)
    sticker = vr.get_sticker(tmp_config, "001")
    assert sticker is not None and sticker.legacy
    assert "001" in vr.list_all(tmp_config, ["001"])
    for path in ("/api/stickers", "/api/variants"):
        assert web.get(path, headers=GUI_HEADERS).status_code == 200, path
    assert tmp_config.variants_path.read_bytes() == before           # 表示は記録を変えない
