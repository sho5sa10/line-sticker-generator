"""next_seq が整数以外（古い・手で編集した記録）でも、採番が壊れないこと（Phase 7 L-A）。

next_seq の読み取りは _stored_next_seq に一本化しています。
- 正の整数として読める値（5、"5"、5.0）は、その整数として使う
- 読めない値（null・0・負の数・数字でない文字列）は、記録にある候補と実行の記録の予約・完了の番号の次を使う
どの経路（候補0件の取り込み・候補ありの採番・repair）でも、例外にならず、予約済み・使用済みの番号を
使い回さず、next_seq が後退しないことを確かめます。
画像生成APIは呼びません（偽のプロバイダ。通信も遮断します。fixture は test_candidate_numbering と共通）。
"""

from __future__ import annotations

import re

import pytest

from src import variants as vr
from tests.conftest import make_character
from tests.test_candidate_numbering import (  # noqa: F401 - 通信遮断の fixture（autouse）も読み込む
    FakeProvider, _entry, _generator, _ids, _record, no_real_provider,
)

READABLE = {"5": 5, 5.0: 5}                         # 正の整数として読める値 → その整数
UNREADABLE = [None, -1, 0, "abc"]                   # 読めない値 → 安全な既定値
ALL_VALUES = list(READABLE) + UNREADABLE
ID = re.compile(r"^v\d{3}$")


def _put(cfg, **record):
    vr.update(cfg, lambda s: s.setdefault("stickers", {}).update(
        {"001": {"adopted": None, "adopted_at": None, "variants": [], **record}}))


def _png(cfg, name, color):
    folder = vr.variant_dir(cfg, "001")
    folder.mkdir(parents=True, exist_ok=True)
    make_character((256, 256), color=color).save(folder / name, format="PNG")
    return folder / name


def _original(cfg):
    make_character((256, 256), color=(250, 205, 60, 255)).save(cfg.dir_generated / "001.png")
    return vr.sha1_file(cfg.dir_generated / "001.png")


def _numbers(ids):
    return [int(v[1:]) for v in ids]


def _regen_run(*reserved):
    return {"since": "2026-01-01T00:00:00.000000", "count": 3, "done": [], "reserved": list(reserved),
            "owner": None}


# ===========================================================================
# 読み取りの規則
# ===========================================================================
@pytest.mark.parametrize("value, expected", [
    (5, 5), ("5", 5), (" 7 ", 7), (5.0, 5), (1, 1),
    (None, None), (0, None), (-1, None), ("abc", None), ("5.0", None), (5.5, None), (True, None), ([5], None),
])
def test_stored_next_seq_reads_only_positive_integers(value, expected):
    assert vr._stored_next_seq({"next_seq": value}) == expected
    assert vr._stored_next_seq({}) is None and vr._stored_next_seq(None) is None


# ===========================================================================
# A: 候補0件の記録（予約後に失敗した跡）
# ===========================================================================
@pytest.mark.parametrize("original", [True, False])
@pytest.mark.parametrize("value", ALL_VALUES)
def test_a_empty_record_with_an_unusual_next_seq(tmp_config, value, original):
    """再生成で v001〜v003 を予約済み（ファイルなし）の候補0件の記録。原画あり・なしで1枚作る。"""
    _put(tmp_config, next_seq=value, regen_run=_regen_run("v001", "v002", "v003"))
    original_sha = _original(tmp_config) if original else None
    results = vr.generate_variants(tmp_config, [_entry()], 1, _generator(tmp_config, FakeProvider()))
    assert [r["status"] for r in results] == ["generated"]         # 例外にならない

    record = _record(tmp_config)
    ids = _ids(tmp_config)
    assert all(ID.match(v) for v in ids) and len(ids) == len(set(ids))
    assert not set(ids) & {"v001", "v002", "v003"}                  # 予約済みの番号を使わない
    floor = READABLE.get(value, 4)                                  # 読めない値は、予約の次（v004）から
    assert min(_numbers(ids)) == floor
    assert isinstance(record["next_seq"], int) and record["next_seq"] == max(_numbers(ids)) + 1
    if original:
        legacy = next(v for v in record["variants"] if v["source"] == vr.SOURCE_LEGACY)
        assert int(legacy["variant_id"][1:]) == floor and record["adopted"] == legacy["variant_id"]
        assert vr.sha1_file(tmp_config.root / legacy["file"]) == original_sha
    folder = vr.variant_dir(tmp_config, "001")
    assert sorted(p.name for p in folder.iterdir()) == sorted(f"{v}.png" for v in ids)


def test_a_existing_file_is_not_overwritten_with_an_unusual_next_seq(tmp_config):
    """読めない next_seq でも、置き場にある別の画像（v004）は上書きせず、その次に原画を置く。"""
    other = _png(tmp_config, "v004.png", (200, 60, 60, 255))
    other_sha = vr.sha1_file(other)
    _put(tmp_config, next_seq=None, regen_run=_regen_run("v001", "v002", "v003"))
    _original(tmp_config)
    vr.generate_variants(tmp_config, [_entry()], 1, _generator(tmp_config, FakeProvider()))
    assert vr.sha1_file(other) == other_sha
    assert _record(tmp_config)["adopted"] == "v005" and _ids(tmp_config) == ["v005", "v006"]


# ===========================================================================
# B: 候補がある記録
# ===========================================================================
@pytest.mark.parametrize("value", ALL_VALUES)
def test_b_record_with_candidates_and_an_unusual_next_seq(tmp_config, value):
    """候補 v001・v003（v002 は欠番）の記録で1枚作る: 例外にならず、欠番も使用済みの番号も使わない。"""
    for vid, color in (("v001", (30, 60, 200, 255)), ("v003", (60, 200, 30, 255))):
        path = _png(tmp_config, f"{vid}.png", color)

        def reg(state, vid=vid, path=path):
            record = state.setdefault("stickers", {}).setdefault(
                "001", {"adopted": None, "adopted_at": None, "variants": [], "next_seq": 4})
            vr.register_variant(tmp_config, record, vid, path, meta={})
        vr.update(tmp_config, reg)
    vr.update(tmp_config, lambda s: s["stickers"]["001"].update({"next_seq": value}))

    results = vr.generate_variants(tmp_config, [_entry()], 1, _generator(tmp_config, FakeProvider()))
    assert [r["status"] for r in results] == ["generated"]
    new = results[0]["variant_id"]
    assert ID.match(new) and new not in ("v000", "v001", "v002", "v003")
    assert int(new[1:]) == READABLE.get(value, 4)                   # 読めない値は、使用済みの番号の次
    record = _record(tmp_config)
    assert isinstance(record["next_seq"], int) and record["next_seq"] == int(new[1:]) + 1
    assert _ids(tmp_config) == ["v001", "v003", new]


# ===========================================================================
# C: repair
# ===========================================================================
@pytest.mark.parametrize("value", ALL_VALUES)
def test_c_repair_of_an_empty_record_with_an_unusual_next_seq(tmp_config, value):
    """候補0件の記録（予約 v001・v002）＋記録に無い画像 v003＋原画 → repair。"""
    stray = _png(tmp_config, "v003.png", (30, 60, 200, 255))
    stray_sha = vr.sha1_file(stray)
    _put(tmp_config, next_seq=value, regen_run=_regen_run("v001", "v002"))
    original_sha = _original(tmp_config)

    vr.repair_state(tmp_config)                                     # 例外にならない
    record = _record(tmp_config)
    ids = _ids(tmp_config)
    assert "v003" in ids and vr.sha1_file(stray) == stray_sha       # 既存の画像は壊さない
    assert not set(ids) & {"v001", "v002"}                          # 予約済みの番号を使わない
    legacy = record["adopted"]
    assert ID.match(legacy) and int(legacy[1:]) == READABLE.get(value, 4)
    assert vr.sha1_file(tmp_config.root / next(v["file"] for v in record["variants"]
                                               if v["variant_id"] == legacy)) == original_sha
    assert record["regen_run"]["reserved"] == ["v001", "v002"]
    assert isinstance(record["next_seq"], int) and record["next_seq"] >= max(_numbers(ids)) + 1
    if value in READABLE:
        assert record["next_seq"] >= READABLE[value]                # 読める値からは後退しない


@pytest.mark.parametrize("value", ALL_VALUES)
def test_c_repair_of_a_record_with_candidates_and_an_unusual_next_seq(tmp_config, value):
    """候補 v001 の記録＋記録に無い画像 v002 → repair（追加の経路）。"""
    path = _png(tmp_config, "v001.png", (30, 60, 200, 255))

    def reg(state):
        record = state.setdefault("stickers", {}).setdefault(
            "001", {"adopted": None, "adopted_at": None, "variants": [], "next_seq": 2})
        vr.register_variant(tmp_config, record, "v001", path, meta={})
    vr.update(tmp_config, reg)
    vr.update(tmp_config, lambda s: s["stickers"]["001"].update({"next_seq": value}))
    _png(tmp_config, "v002.png", (60, 200, 30, 255))

    report = vr.repair_state(tmp_config)                            # 例外にならない
    assert report["stickers"]["001"]["added"] == ["v002"]
    record = _record(tmp_config)
    assert _ids(tmp_config) == ["v001", "v002"]
    assert isinstance(record["next_seq"], int) and record["next_seq"] == max(3, READABLE.get(value, 3))
    results = vr.generate_variants(tmp_config, [_entry()], 1, _generator(tmp_config, FakeProvider()))
    assert int(results[0]["variant_id"][1:]) == record["next_seq"]  # その後の採番も使用済みの番号の次から


# ===========================================================================
# L-A2: 数字に見えても int() で変換できない文字列で、例外にしない
# ===========================================================================
SUPERSCRIPT = "²"                               # "²": isdigit() は真だが int() は変換できない
TOO_LONG = "9" * 4301                                # 整数変換の桁数の上限（4300）を超える


@pytest.mark.parametrize("value", [SUPERSCRIPT, "¹²³", f" {SUPERSCRIPT} ", TOO_LONG],
                         ids=["superscript", "superscripts", "superscript-with-spaces", "4301-digits"])
def test_la2_unconvertible_digit_strings_are_unreadable(value):
    assert vr._stored_next_seq({"next_seq": value}) is None          # 例外にならず、読めない値
    assert vr._next_seq_of({"next_seq": value, "variants": []}) == 1  # 安全な既定値


@pytest.mark.parametrize("value, expected", [("５", 5), ("٥", 5), (" 5 ", 5), ("9" * 4300, int("9" * 4300))],
                         ids=["fullwidth", "arabic-indic", "spaces", "4300-digits"])
def test_la2_decimal_strings_that_int_accepts_are_still_read(value, expected):
    """int() が受け付ける 10 進の数字（全角・アラビア・インド数字、上限ちょうどの桁数）は、従来どおり読む。"""
    assert vr._stored_next_seq({"next_seq": value}) == expected


@pytest.mark.parametrize("value", [SUPERSCRIPT, TOO_LONG], ids=["superscript", "4301-digits"])
def test_la2_empty_record_with_an_unconvertible_next_seq(tmp_config, value):
    """予約 v001〜v003 の候補0件の記録: 例外にならず、予約の次（v004）から作る。"""
    _put(tmp_config, next_seq=value, regen_run=_regen_run("v001", "v002", "v003"))
    results = vr.generate_variants(tmp_config, [_entry()], 1, _generator(tmp_config, FakeProvider()))
    assert [(r["variant_id"], r["status"]) for r in results] == [("v004", "generated")]
    record = _record(tmp_config)
    assert record["next_seq"] == 5 and record["regen_run"]["reserved"] == ["v001", "v002", "v003"]


def _with_broken_numbers(cfg):
    """正常な候補 v001 と、番号の壊れた候補 v²、予約（壊れた番号 v² と v002）を持つ記録。"""
    path = _png(cfg, "v001.png", (30, 60, 200, 255))

    def put(state):
        record = state.setdefault("stickers", {}).setdefault(
            "001", {"adopted": None, "adopted_at": None, "variants": [], "next_seq": 2})
        vr.register_variant(cfg, record, "v001", path, meta={})
        record["variants"].append({"variant_id": f"v{SUPERSCRIPT}", "file": f"output/variants/001/v{SUPERSCRIPT}.png"})
        record["next_seq"] = SUPERSCRIPT
        record["regen_run"] = _regen_run(f"v{SUPERSCRIPT}", "v002")
    vr.update(cfg, put)
    return vr.sha1_file(path)


def test_la2_broken_candidate_numbers_do_not_stop_numbering(tmp_config):
    v001_sha = _with_broken_numbers(tmp_config)
    record = _record(tmp_config)
    assert vr._next_seq_of(record) == 3                            # 壊れた番号は無視し、v001・v002 の次
    results = vr.generate_variants(tmp_config, [_entry()], 1, _generator(tmp_config, FakeProvider()))
    assert [(r["variant_id"], r["status"]) for r in results] == [("v003", "generated")]
    record = _record(tmp_config)
    assert [v["variant_id"] for v in record["variants"]] == ["v001", f"v{SUPERSCRIPT}", "v003"]
    assert vr.sha1_file(vr.variant_dir(tmp_config, "001") / "v001.png") == v001_sha    # 正常な候補は壊さない
    assert record["next_seq"] == 4


def test_la2_repair_with_broken_candidate_numbers(tmp_config):
    """壊れた番号があっても repair（記録に無い画像の追加）が止まらない。"""
    _with_broken_numbers(tmp_config)
    _png(tmp_config, "v003.png", (60, 200, 30, 255))
    report = vr.repair_state(tmp_config)
    assert report["stickers"]["001"]["added"] == ["v003"]
    record = _record(tmp_config)
    assert isinstance(record["next_seq"], int) and record["next_seq"] == 4
    assert "v001" in _ids(tmp_config) and record["regen_run"]["reserved"] == [f"v{SUPERSCRIPT}", "v002"]


def test_la2_repair_rebuild_with_broken_reserved_numbers(tmp_config):
    """候補0件の記録の作り直し（予約に壊れた番号 v²）でも例外にならず、予約済みの v002 を使わない。"""
    _png(tmp_config, "v003.png", (30, 60, 200, 255))
    _put(tmp_config, next_seq=SUPERSCRIPT, regen_run=_regen_run(f"v{SUPERSCRIPT}", "v001", "v002"))
    original_sha = _original(tmp_config)
    vr.repair_state(tmp_config)
    record = _record(tmp_config)
    ids = _ids(tmp_config)
    assert "v003" in ids and not {"v001", "v002"} & set(ids)
    assert record["adopted"] == "v004"                             # 原画は予約と v003 を避けた v004
    assert vr.sha1_file(tmp_config.root / "output/variants/001/v004.png") == original_sha
    assert isinstance(record["next_seq"], int) and record["next_seq"] == 5


# ===========================================================================
# L-A3 / L-A4: 保存されている候補の番号は _variant_number で読む（v で始まる正の番号だけ）
# ===========================================================================
@pytest.mark.parametrize("variant_id, expected", [
    ("v001", 1), ("v005", 5), ("v999", 999),
    ("v000", None), ("v-01", None), ("vabc", None), (f"v{SUPERSCRIPT}", None),
    ("x001", None), ("x009", None), ("abc", None), ("", None), (None, None), (1, None),
])
def test_la3_variant_number_reads_only_positive_v_numbers(variant_id, expected):
    assert vr._variant_number(variant_id) == expected


MIXED = ["v001", "x009", "v000", "v002"]                 # 有効: v001・v002 / 無効: x009・v000


def test_la4_repair_add_path_ignores_malformed_numbers(tmp_config):
    """repair の追加の経路: x009・v000 を使用済みの番号（9・0）として数えない。"""
    path = _png(tmp_config, "v001.png", (30, 60, 200, 255))

    def put(state):
        record = state.setdefault("stickers", {}).setdefault(
            "001", {"adopted": None, "adopted_at": None, "variants": [], "next_seq": 2})
        vr.register_variant(tmp_config, record, "v001", path, meta={})
        for vid in ("x009", "v000"):                       # 手で壊した番号
            record["variants"].append({"variant_id": vid, "file": f"output/variants/001/{vid}.png"})
        record["next_seq"] = None                           # 読めない → 既定値
    vr.update(tmp_config, put)
    _png(tmp_config, "v002.png", (60, 200, 30, 255))        # 記録に無い画像

    report = vr.repair_state(tmp_config)
    assert report["stickers"]["001"]["added"] == ["v002"]
    record = _record(tmp_config)
    assert sorted(v["variant_id"] for v in record["variants"]) == sorted(MIXED)
    # 有効な番号（v001・v002）の次の 3 と、候補の数（4）+ 1 の大きいほう。x009 を 9 と数えれば 10 になる
    assert record["next_seq"] == 5


def test_la4_repair_merge_path_ignores_malformed_reserved_numbers(tmp_config):
    """repair の作り直しと併合の経路: 予約の x009・v000 を使用済みの番号として数えない。"""
    _png(tmp_config, "v003.png", (30, 60, 200, 255))                     # 記録に無い画像
    _put(tmp_config, next_seq=None, regen_run=_regen_run(*MIXED))
    vr.repair_state(tmp_config)
    record = _record(tmp_config)
    assert _ids(tmp_config) == ["v003"]
    assert record["regen_run"]["reserved"] == MIXED                     # 予約の記録はそのまま
    # 作り直した値（v003 の次の 4）と、有効な予約（v001・v002）の次の最大。x009 を 9 と数えれば 10 になる
    assert record["next_seq"] == 4


def test_la4_next_seq_of_uses_the_same_rule():
    record = {"next_seq": None, "variants": [{"variant_id": v} for v in MIXED]}
    assert vr._next_seq_of(record) == 5                                  # max(v002 + 1, 候補の数 4 + 1)
    record = {"next_seq": None, "variants": [], "regen_run": {"reserved": MIXED, "done": []}}
    assert vr._next_seq_of(record) == 3                                  # v002 + 1（x009・v000 は数えない）
