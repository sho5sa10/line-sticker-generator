"""候補の画像の書き込み先（<番号>.png.part）を、API を呼ぶ前に確かめること（Phase 5B L-B の残り）。

番号の上限（VARIANT_NUMBER_MAX）以内でも、パス全体が長すぎる（Windows で長いパスが無効なら 260 文字以上）
などで書き込めない場所があります。API を呼んでから書き込みに失敗すると課金済みの画像を失うため、
採番（allocate_variant）の時点で実際に作って確かめ、作れなければ API を呼ばずに VariantError にします。

パスの長さの上限は環境で変わるため、Windows の MAX_PATH の境界は os.open を差し替えて再現します
（どの環境でも同じ結果になるように）。
"""

from __future__ import annotations

import json
import os

import pytest

from src import variants as vr
from tests.test_candidate_numbering import FakeProvider, _entry, _generator

MAX_PATH = 260          # Windows で長いパスが無効なときの上限（終端を含む。259 文字まで作れる）


def _put(config, next_seq) -> None:
    vr.update(config, lambda s: s.setdefault("stickers", {}).update(
        {"001": {"adopted": None, "adopted_at": None, "variants": [{"variant_id": "v001", "file": "x"}],
                 "next_seq": next_seq}}))


def _part_path_length(config, digits: int) -> int:
    return len(str(vr.variant_dir(config, "001") / ("v" + "1" * digits + ".png.part")))


def _digits_for(config, length: int) -> int:
    """.png.part のパス全体がちょうど length 文字になる番号の桁数。"""
    return length - _part_path_length(config, 0)


@pytest.fixture
def windows_max_path(monkeypatch):
    """Windows（長いパスが無効）と同じく、260 文字以上のパスでファイルを作れないようにします。"""
    real_open = os.open

    def limited_open(path, flags, *args, **kwargs):
        if len(os.fspath(path)) >= MAX_PATH:
            raise FileNotFoundError(2, "No such file or directory", os.fspath(path))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(vr.os, "open", limited_open)


def _generate(config, count: int = 1):
    provider = FakeProvider()
    results = vr.generate_variants(config, [_entry()], count, _generator(config, provider))
    return provider, results


# ===========================================================================
# 書き込める場所: これまでどおり API を呼ぶ
# ===========================================================================
def test_short_path_calls_the_api(tmp_config):
    provider, results = _generate(tmp_config)
    assert provider.calls == 1 and [r["status"] for r in results] == ["generated"]
    folder = vr.variant_dir(tmp_config, "001")
    assert sorted(p.name for p in folder.iterdir()) == ["v001.png"]     # 確かめた空のファイルは残さない


def test_allocate_leaves_no_probe_file(tmp_config):
    vr.update_sticker(tmp_config, "001", lambda state: vr.allocate_variant(
        tmp_config, vr.ensure_record(tmp_config, state, "001"), "001"))
    assert list(vr.variant_dir(tmp_config, "001").iterdir()) == []


# ===========================================================================
# Windows の MAX_PATH の境界（259 文字まで作れる・260 文字から作れない）
# ===========================================================================
def test_part_path_at_the_windows_limit_calls_the_api(tmp_config, windows_max_path):
    digits = _digits_for(tmp_config, MAX_PATH - 1)
    assert 3 < digits <= len(str(vr.VARIANT_NUMBER_MAX))
    _put(tmp_config, int("1" * digits))
    provider, results = _generate(tmp_config)
    assert provider.calls == 1 and [r["status"] for r in results] == ["generated"]


@pytest.mark.parametrize("extra", [0, 1, 50], ids=["260", "261", "310"])
def test_part_path_over_the_windows_limit_does_not_call_the_api(tmp_config, windows_max_path, extra):
    digits = _digits_for(tmp_config, MAX_PATH + extra)
    assert digits <= len(str(vr.VARIANT_NUMBER_MAX))         # 番号の上限以内（上限の確認では止まらない）
    _put(tmp_config, int("1" * digits))
    before = tmp_config.variants_path.read_bytes()
    provider = FakeProvider()
    with pytest.raises(vr.VariantError):
        vr.generate_variants(tmp_config, [_entry()], 2, _generator(tmp_config, provider))
    assert provider.calls == 0                                        # API は呼ばない
    assert tmp_config.variants_path.read_bytes() == before            # 記録（next_seq）も変えない


def test_previous_review_case_1e200_does_not_call_the_api(tmp_config, windows_max_path):
    """前回のレビューで再現したケース（1e200 相当の next_seq で、API の後に書き込みに失敗していた）。"""
    _put(tmp_config, 10 ** 200)
    assert _part_path_length(tmp_config, 201) >= MAX_PATH
    provider = FakeProvider()
    with pytest.raises(vr.VariantError):
        vr.generate_variants(tmp_config, [_entry()], 1, _generator(tmp_config, provider))
    assert provider.calls == 0
    assert json.loads(tmp_config.variants_path.read_text(encoding="utf-8"))["stickers"]["001"]["next_seq"] == 10 ** 200


# ===========================================================================
# 長さ以外の理由で作れない場所（差し替えなし・どの環境でも同じ）
# ===========================================================================
def test_unwritable_folder_does_not_call_the_api(tmp_config):
    """候補の置き場がファイルで塞がれている: 書き込み先を作れないので、API を呼ばない。"""
    folder = vr.variant_dir(tmp_config, "001")
    folder.parent.mkdir(parents=True, exist_ok=True)
    folder.write_text("not a folder", encoding="utf-8")
    provider = FakeProvider()
    with pytest.raises(vr.VariantError):
        vr.generate_variants(tmp_config, [_entry()], 1, _generator(tmp_config, provider))
    assert provider.calls == 0
    assert folder.read_text(encoding="utf-8") == "not a folder"
