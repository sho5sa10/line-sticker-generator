"""壊れた・極端な値を含む記録やロックファイルでも、想定外の例外を外に出さないこと（Phase 5B の残課題）。

- LOW-1: 入れ子が深すぎる JSON（json.loads が RecursionError）は「JSON として読めない」と同じに扱います
  （ロックは持ち主の分からない鍵、variants.json は壊れた記録）。
- LOW-2: variants が list でない記録は、候補0件の記録と同じに扱います（ensure_record が [] に直し、
  repair は候補フォルダの画像から作り直します）。
- L-B: 「読めるか」（_stored_next_seq・_variant_number。L-A2 のとおり 4300 桁も読む）と、「採番・保存に
  使えるか」を分けます。採番に使うのは、番号 1〜VARIANT_NUMBER_MAX（ファイル名の長さから決まる実装上の
  安全限界）、next_seq はその次までで、超える値は採番では読めない値と同じに扱います（使用済みの番号の次から）。
  最後の番号を使い切ったら、API を呼ぶ前に止めます。repair は v1000.png 以降も取り込みます。
- INFO-D: repair の作り直し（_rebuild_record）も、ファイル名の番号を追加取り込み（_add_missing_variants）と
  同じ読み方で読みます。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from src import variants as vr
from tests.conftest import make_character

MAX = vr.VARIANT_NUMBER_MAX
DEEP = "[" * 100000 + "]" * 100000
DEEP_DICT = '{"a":' * 100000 + "1" + "}" * 100000


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def _write_state(config, stickers: dict) -> None:
    config.variants_path.parent.mkdir(parents=True, exist_ok=True)
    config.variants_path.write_text(json.dumps({"schema": 1, "stickers": stickers}), encoding="utf-8")


def _state(config) -> dict:
    return json.loads(config.variants_path.read_text(encoding="utf-8"))


def _png(config, name: str, sticker_id: str = "001"):
    folder = vr.variant_dir(config, sticker_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    make_character().save(path)
    return path


def _allocate(config, sticker_id: str = "001") -> str:
    return vr.update_sticker(config, sticker_id, lambda state: vr.allocate_variant(
        config, vr.ensure_record(config, state, sticker_id), sticker_id))[0]


# ===========================================================================
# LOW-1: ロックファイルの JSON
# ===========================================================================
def _lock(tmp_path, text: str, *, age: float = 0.0):
    path = tmp_path / "variants.run-001.lock"
    path.write_text(text, encoding="utf-8")
    if age:
        past = time.time() - age
        os.utime(path, (past, past))
    return path


def test_normal_json_lock_is_judged_by_its_owner(tmp_path):
    host = vr._this_host()
    assert vr._lock_is_stale(_lock(tmp_path, json.dumps({"pid": _dead_pid(), "host": host}))) is True
    assert vr._lock_is_stale(_lock(tmp_path, json.dumps({"pid": os.getpid(), "host": host}), age=3600)) is False


def test_empty_lock_keeps_the_unwritten_grace(tmp_path):
    assert vr._lock_is_stale(_lock(tmp_path, "")) is False
    assert vr._lock_is_stale(_lock(tmp_path, "", age=vr.LOCK_UNWRITTEN_GRACE_SEC + 5)) is True


UNREADABLE_LOCKS = ["{", "²", "9" * 4301, DEEP, DEEP_DICT]
UNREADABLE_IDS = ["broken-json", "superscript", "4301-digits", "deep-list", "deep-dict"]


@pytest.mark.parametrize("text", UNREADABLE_LOCKS, ids=UNREADABLE_IDS)
def test_unreadable_lock_is_judged_by_age(tmp_path, text):
    """JSON として読めない鍵（深すぎる入れ子を含む）は、作られた直後なら使用中、時間が経てば古い鍵。"""
    assert vr._lock_is_stale(_lock(tmp_path, text)) is False
    assert vr._lock_is_stale(_lock(tmp_path, text, age=vr.LOCK_STALE_SEC + 60)) is True


@pytest.mark.parametrize("text", [DEEP, DEEP_DICT], ids=["deep-list", "deep-dict"])
def test_generation_lock_with_a_deeply_nested_json(tmp_config, text):
    path = tmp_config.variants_path.with_name("variants.run-001.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    assert vr.generation_running_elsewhere(tmp_config, "001") is True           # 使用中（例外にしない）
    with pytest.raises(vr.GenerationBusyError):
        with vr.generation_lock(tmp_config, "001"):
            pass
    past = time.time() - (vr.LOCK_STALE_SEC + 60)
    os.utime(path, (past, past))
    assert vr.generation_running_elsewhere(tmp_config, "001") is False
    with vr.generation_lock(tmp_config, "001"):                                 # 古い鍵を外して取得
        assert path.exists()
    assert not path.exists()


def test_state_lock_with_a_deeply_nested_json(tmp_config):
    path = tmp_config.variants_path.with_name(tmp_config.variants_path.name + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEEP, encoding="utf-8")
    with pytest.raises(vr.LockBusyError):
        vr.update(tmp_config, lambda state: None, timeout=0.3)
    past = time.time() - (vr.LOCK_STALE_SEC + 60)
    os.utime(path, (past, past))
    vr.update(tmp_config, lambda state: state.setdefault("stickers", {}), timeout=5)
    assert not path.exists()


# ===========================================================================
# LOW-1: variants.json の JSON
# ===========================================================================
@pytest.mark.parametrize("text", [DEEP, '{"stickers": ' + DEEP + "}"], ids=["deep", "deep-inside"])
def test_deeply_nested_state_file_is_corrupt(tmp_config, text):
    """深すぎる入れ子の variants.json は「壊れている」（例外にせず、保存は拒否・repair で退避）。"""
    tmp_config.variants_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_config.variants_path.write_text(text, encoding="utf-8")
    assert vr.state_status(tmp_config) == vr.STATE_CORRUPT
    assert vr.load(tmp_config)["stickers"] == {}
    with pytest.raises(vr.StateCorruptError):
        vr.update(tmp_config, lambda state: state.setdefault("stickers", {}))
    assert tmp_config.variants_path.read_text(encoding="utf-8") == text     # 上書きしない
    report = vr.repair_state(tmp_config)
    assert report["kind"] == vr.REPAIR_CORRUPT and report["quarantined"]


# ===========================================================================
# LOW-2: variants の型
# ===========================================================================
JUNK = {"none": None, "empty-dict": {}, "empty-list": [], "str": "abc", "number": 123, "bool": True,
        "dict": {"v001": {"variant_id": "v001"}}, "broken-items": [1, "x", None]}


@pytest.mark.parametrize("name", list(JUNK))
def test_reading_a_record_with_unusable_variants_does_not_raise(tmp_config, name):
    record = {"variants": JUNK[name], "adopted": None}
    _write_state(tmp_config, {"001": record})
    assert vr._next_seq_of(record) >= 1
    assert vr.get_sticker(tmp_config, "001") is None              # 読める候補は無い（原画も無い）
    assert vr.list_all(tmp_config, ["001"]) == {}
    vr.regen_plan(tmp_config, ["001"], 1)
    vr.initial_plan(tmp_config, ["001"], 1)


@pytest.mark.parametrize("name", ["none", "empty-dict", "str", "number", "bool", "dict"])
def test_generation_on_a_record_whose_variants_is_not_a_list(tmp_config, name):
    """候補0件の記録として扱い、variants を [] に直してから採番・登録します（ほかの項目は残します）。"""
    _write_state(tmp_config, {"001": {"variants": JUNK[name], "adopted": None, "custom": "keep"}})
    variant_id = _allocate(tmp_config)
    assert variant_id == "v001"
    path = _png(tmp_config, f"{variant_id}.png")
    vr.update_sticker(tmp_config, "001", lambda state: vr.register_variant(
        tmp_config, vr.ensure_record(tmp_config, state, "001"), variant_id, path, meta={}))
    record = _state(tmp_config)["stickers"]["001"]
    assert [v["variant_id"] for v in record["variants"]] == ["v001"]
    assert record["custom"] == "keep" and record["next_seq"] == 2


@pytest.mark.parametrize("name", ["str", "number", "bool", "dict"])
def test_resuming_an_initial_run_on_a_record_whose_variants_is_not_a_list(tmp_config, name):
    """止まった初回生成の続き（できていた画像の取り込み）は ensure_record を通らないため、登録でも直します。"""
    run = {"since": "2026-01-01T00:00:00", "count": 1, "reserved": ["v001"], "done": [], "owner": None}
    _write_state(tmp_config, {"001": {"variants": JUNK[name], "adopted": None, "initial_run": run}})
    _png(tmp_config, "v001.png")
    result = vr.begin_initial(tmp_config, "001", 1)
    assert result["recovered"] == 1 and result["claim"] is None
    record = _state(tmp_config)["stickers"]["001"]
    assert [v["variant_id"] for v in record["variants"]] == ["v001"] and "initial_run" not in record


def test_record_with_a_valid_and_a_broken_item(tmp_config):
    """list なら候補のある記録のまま（読めない項目は飛ばし、消さない）。"""
    _write_state(tmp_config, {"001": {"variants": [{"variant_id": "v001", "file": "x.png"}, 5],
                                      "adopted": None, "next_seq": 2}})
    assert [v.variant_id for v in vr.get_sticker(tmp_config, "001").variants] == ["v001"]
    assert _allocate(tmp_config) == "v002"
    assert _state(tmp_config)["stickers"]["001"]["variants"][1] == 5


@pytest.mark.parametrize("name", ["none", "str", "number", "bool", "dict", "broken-items"])
def test_repair_rebuilds_a_record_with_unusable_variants(tmp_config, name):
    _write_state(tmp_config, {"001": {"variants": JUNK[name], "adopted": None, "custom": "keep"}})
    _png(tmp_config, "v002.png")
    vr.repair_state(tmp_config)
    record = _state(tmp_config)["stickers"]["001"]
    ids = [v["variant_id"] for v in record["variants"] if isinstance(v, dict)]
    assert "v002" in ids and record["next_seq"] >= 3 and record["custom"] == "keep"


# ===========================================================================
# L-B: next_seq と番号の境界
# ===========================================================================
def test_limit_comes_from_the_file_name_length():
    """v<番号>.png.part が 255 文字に収まる最大の番号（245 桁）。"""
    assert len(f"v{vr.VARIANT_NUMBER_MAX}.png.part") == 255
    assert len(f"v{vr.VARIANT_NUMBER_MAX + 1}.png.part") == 256


def test_reading_is_unchanged_by_the_allocation_limit():
    """読むこと（L-A2・L-A3）は変えません: 4300 桁も読める。採番に使えるかは別に判断します。"""
    assert vr._stored_next_seq({"next_seq": "9" * 4300}) == int("9" * 4300)
    assert vr._stored_next_seq({"next_seq": int("9" * 4300)}) == int("9" * 4300)
    assert vr._variant_number("v" + "9" * 4300) == int("9" * 4300)


@pytest.mark.parametrize("value, expected", [
    (999, 999), (1000, 1000), (MAX, MAX), (MAX + 1, MAX + 1), (1000.0, 1000), ("1000", 1000),
    (MAX + 2, 4), (int("9" * 4300), 4), ("9" * 4300, 4), ("9" * 4301, 4),
    (1e308, 4), (float("inf"), 4), (float("nan"), 4),
], ids=["999", "1000", "max", "max+1", "float1000", "str1000", "max+2", "4300-digits", "4300-digit-str",
        "4301-digit-str", "1e308", "inf", "nan"])
def test_next_seq_used_for_allocation(value, expected):
    """採番に使えない大きさは、読めない値と同じく「使用済みの番号（v003）の次」から。"""
    assert vr._next_seq_of({"next_seq": value, "variants": [{"variant_id": "v003"}]}) == expected


@pytest.mark.parametrize("variant_id, expected", [
    ("v001", 1), ("v999", 999), ("v1000", 1000), (f"v{MAX}", MAX), ("v000", None),
    (f"v{MAX + 1}", None), ("v" + "9" * 4300, None), ("v" + "9" * 4301, None), ("x009", None),
], ids=["v001", "v999", "v1000", "vMAX", "v000", "vMAX+1", "v4300-digits", "v4301-digits", "x009"])
def test_numbers_used_for_allocation(variant_id, expected):
    assert vr._allocatable_number(variant_id) == expected


@pytest.mark.parametrize("next_seq", [int("9" * 4300), "9" * 4300, 1e308, MAX + 2],
                         ids=["4300-digits", "4300-digit-str", "1e308", "max+2"])
def test_huge_next_seq_falls_back_and_can_be_saved(tmp_config, next_seq):
    """4300 桁（+1 すると文字列にできず保存できない）や 1e308（ファイル名が長すぎる）は採番に使わず、
    使用済みの番号の次から採番して、保存・読み込みまで通ること（番号は使用済みより後退しない）。"""
    _write_state(tmp_config, {"001": {"variants": [{"variant_id": "v003", "file": "x"}], "adopted": None,
                                      "next_seq": next_seq}})
    assert _allocate(tmp_config) == "v004"
    assert _state(tmp_config)["stickers"]["001"]["next_seq"] == 5


def test_huge_recorded_numbers_do_not_break_allocation_or_saving(tmp_config):
    """予約・候補に 4300 桁の番号があっても、+1 して保存に失敗しない（その番号は採番の根拠にしない）。"""
    huge = "v" + "9" * 4300
    _write_state(tmp_config, {"001": {"variants": [{"variant_id": "v003", "file": "x"}, {"variant_id": huge}],
                                      "adopted": None,
                                      "regen_run": {"count": 1, "reserved": [huge], "done": []}}})
    assert _allocate(tmp_config) == "v004"
    assert _state(tmp_config)["stickers"]["001"]["next_seq"] == 5


def test_repair_merge_with_huge_values_can_be_saved(tmp_config):
    """候補0件の記録（next_seq・予約が 4300 桁）を repair で作り直しても、保存できる値になる。"""
    huge = "v" + "9" * 4300
    _write_state(tmp_config, {"001": {"variants": [], "adopted": None, "next_seq": "9" * 4300,
                                      "initial_run": {"count": 1, "reserved": [huge, "v004"], "done": []}}})
    _png(tmp_config, "v002.png")
    vr.repair_state(tmp_config)
    assert _state(tmp_config)["stickers"]["001"]["next_seq"] == 5


def test_repair_add_missing_with_a_huge_recorded_number_can_be_saved(tmp_config):
    _write_state(tmp_config, {"001": {"variants": [{"variant_id": "v001", "file": "x"},
                                                   {"variant_id": "v" + "9" * 4300}],
                                      "adopted": None, "next_seq": 2}})
    _png(tmp_config, "v005.png")
    vr.repair_state(tmp_config)
    assert _state(tmp_config)["stickers"]["001"]["next_seq"] == 6


def test_numbers_continue_past_999(tmp_config):
    _write_state(tmp_config, {"001": {"variants": [{"variant_id": "v999", "file": "x"}], "adopted": None,
                                      "next_seq": 1000}})
    assert _allocate(tmp_config) == "v1000"
    assert _allocate(tmp_config) == "v1001"
    assert _state(tmp_config)["stickers"]["001"]["next_seq"] == 1002


def test_allocation_stops_at_the_limit_before_calling_the_api(tmp_config, monkeypatch):
    # ここで確かめるのは番号の上限（VARIANT_NUMBER_MAX）だけです。書き込み先を実際に作れるかの確認
    # （_ensure_part_writable）は、パスの長さの上限が環境で変わるため無効にし、tests/test_part_path_safety.py で別に確かめます
    monkeypatch.setattr(vr, "_ensure_part_writable", lambda path, sticker_id: None)
    _write_state(tmp_config, {"001": {"variants": [{"variant_id": "v001", "file": "x"}], "adopted": None,
                                      "next_seq": MAX}})
    assert _allocate(tmp_config) == f"v{MAX}"
    assert _state(tmp_config)["stickers"]["001"]["next_seq"] == MAX + 1
    before = tmp_config.variants_path.read_bytes()
    with pytest.raises(vr.VariantError):
        _allocate(tmp_config)
    assert tmp_config.variants_path.read_bytes() == before                  # 記録は変えない


def test_repair_picks_up_numbers_from_1000(tmp_config):
    """記録が無いとき、v999.png と v1000.png の両方から作り直します（v000.png は従来どおり取り込みます）。"""
    for name in ("v000.png", "v999.png", "v1000.png", "v01000.png", "v0001.png"):
        _png(tmp_config, name)
    vr.repair_state(tmp_config)
    record = _state(tmp_config)["stickers"]["001"]
    assert sorted(v["variant_id"] for v in record["variants"]) == ["v000", "v1000", "v999"]
    assert record["next_seq"] == 1001


def test_repair_adds_missing_numbers_from_1000(tmp_config):
    _write_state(tmp_config, {"001": {"variants": [{"variant_id": "v001", "file": "x"}], "adopted": None,
                                      "next_seq": 2}})
    _png(tmp_config, "v1000.png")
    report = vr.repair_state(tmp_config)
    assert report["stickers"]["001"]["added"] == ["v1000"]
    assert _state(tmp_config)["stickers"]["001"]["next_seq"] == 1001


# ===========================================================================
# INFO-D: _rebuild_record の番号の読み方
# ===========================================================================
@pytest.mark.parametrize("names, expected", [
    (["v001.png"], 2), (["v000.png"], 1), (["v001.png", "v1000.png"], 1001),
    (["vabc.png"], 1), (["v-01.png"], 1), (["v000.png", "v-01.png", "v002.png"], 3),
], ids=["v001", "v000", "v1000", "not-a-number", "negative", "mixed"])
def test_rebuild_record_reads_numbers_like_the_rest_of_repair(tmp_config, names, expected):
    """形の崩れた番号は数えず（例外にしない・0 以下の next_seq にしない）、v000 も数えません。"""
    files = [_png(tmp_config, name) for name in names]
    record = vr._rebuild_record(tmp_config, "001", files)
    assert record["next_seq"] == expected
