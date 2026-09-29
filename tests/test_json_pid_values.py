"""記録（ロックファイルの JSON・variants.json の実行の owner）から読んだ PID の型と範囲。

PID を OS に問い合わせてよいのは、正の整数で OS の PID の範囲内のものだけです（_valid_pid）。
真偽値（bool は int の一種）・0・負の数・小数・文字列・範囲外の整数は OS に渡さず、これまでの
「pid が整数でない」場合と同じに扱います（鍵は作られてからの時間で判断・実行は止まったもの）。
範囲外の整数を渡すと、Windows では DWORD の下位 32 ビットに切り詰められて別のプロセス
（自分自身のことも）を指し、それ以外では os.kill が OverflowError になるためです。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from src import variants as vr

INVALID = [True, False, 0, -1, -100, 1.0, 1.5, "12345", " 12345 ", "abc", None,
           2 ** 32, 2 ** 32 + 5, 10 ** 20]
INVALID_IDS = ["true", "false", "zero", "minus1", "minus100", "float1.0", "float1.5", "str", "str-spaces",
               "abc", "none", "2^32", "2^32+5", "10^20"]


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


@pytest.fixture
def asked(monkeypatch):
    """OS に問い合わせた PID を記録します（問い合わせ自体は本物に任せます）。"""
    calls: list = []
    real = vr._process_start

    def record(pid):
        calls.append(pid)
        return real(pid)

    monkeypatch.setattr(vr, "_process_start", record)
    return calls


def _json_lock(tmp_path, pid, *, age: float = 0.0):
    path = tmp_path / "variants.run-001.lock"
    path.write_text(json.dumps({"pid": pid, "host": vr._this_host(), "started": None,
                                "created": time.time()}), encoding="utf-8")
    if age:
        past = time.time() - age
        os.utime(path, (past, past))
    return path


def _owner(pid) -> dict:
    return {"pid": pid, "host": vr._this_host(), "started": None, "token": "t" * 32}


# ===========================================================================
# PID の判定そのもの
# ===========================================================================
def test_valid_pids():
    assert vr._valid_pid(12345) and vr._valid_pid(1) and vr._valid_pid(os.getpid())
    assert vr._valid_pid(vr._PID_MAX) and not vr._valid_pid(vr._PID_MAX + 1)


@pytest.mark.parametrize("pid", INVALID, ids=INVALID_IDS)
def test_invalid_pids(pid):
    assert vr._valid_pid(pid) is False


def test_pid_limit_matches_the_os_api():
    """Windows は OpenProcess の DWORD、それ以外は os.kill の pid_t（符号付き 32 ビット）。"""
    assert vr._PID_MAX == (0xFFFFFFFF if os.name == "nt" else 0x7FFFFFFF)


# ===========================================================================
# JSON 形式のロック
# ===========================================================================
def test_json_lock_with_a_valid_pid_asks_the_os(tmp_path, asked):
    dead = _dead_pid()
    assert vr._lock_is_stale(_json_lock(tmp_path, dead)) is True
    assert vr._lock_is_stale(_json_lock(tmp_path, os.getpid(), age=3600)) is False   # 生きていれば古くても外さない
    assert asked == [dead, os.getpid()]


@pytest.mark.parametrize("pid", INVALID, ids=INVALID_IDS)
def test_json_lock_with_an_invalid_pid_is_judged_by_age(tmp_path, asked, pid):
    """OS には渡さず（切り詰められた PID で問い合わせない）、作られてからの時間で判断します。"""
    assert vr._lock_is_stale(_json_lock(tmp_path, pid)) is False
    assert vr._lock_is_stale(_json_lock(tmp_path, pid, age=vr.LOCK_STALE_SEC + 60)) is True
    assert asked == []


def test_json_lock_pointing_at_this_process_after_truncation_is_not_kept_forever(tmp_path):
    """2**32 + 自分の PID は、Windows の DWORD では自分自身になります。古くなれば外せること。"""
    path = _json_lock(tmp_path, 2 ** 32 + os.getpid(), age=vr.LOCK_STALE_SEC + 60)
    assert vr._lock_is_stale(path) is True


@pytest.mark.parametrize("pid", [True, 2 ** 32 + 5, 10 ** 20], ids=["true", "2^32+5", "10^20"])
def test_generation_lock_with_an_invalid_json_pid(tmp_config, asked, pid):
    path = tmp_config.variants_path.with_name("variants.run-001.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": pid, "host": vr._this_host()}), encoding="utf-8")
    assert vr.generation_running_elsewhere(tmp_config, "001") is True           # 作られた直後: 使用中
    with pytest.raises(vr.GenerationBusyError):
        with vr.generation_lock(tmp_config, "001"):
            pass
    past = time.time() - (vr.LOCK_STALE_SEC + 60)
    os.utime(path, (past, past))
    assert vr.generation_running_elsewhere(tmp_config, "001") is False          # 時間が経てば古い鍵
    with vr.generation_lock(tmp_config, "001"):
        assert path.exists()
    assert not path.exists()
    assert set(asked) <= {os.getpid()}      # 問い合わせたのは自分の鍵の持ち主情報だけ（壊れた PID は渡さない）


@pytest.mark.parametrize("pid", [False, 2 ** 32 + 5], ids=["false", "2^32+5"])
def test_state_lock_with_an_invalid_json_pid(tmp_config, pid):
    path = tmp_config.variants_path.with_name(tmp_config.variants_path.name + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": pid, "host": vr._this_host()}), encoding="utf-8")
    with pytest.raises(vr.LockBusyError):
        vr.update(tmp_config, lambda state: None, timeout=0.3)
    past = time.time() - (vr.LOCK_STALE_SEC + 60)
    os.utime(path, (past, past))
    vr.update(tmp_config, lambda state: state.setdefault("stickers", {}), timeout=5)
    assert not path.exists()


# ===========================================================================
# 実行の記録（initial_run / regen_run の owner）
# ===========================================================================
def test_owner_with_a_valid_pid_asks_the_os(asked):
    dead = _dead_pid()
    assert vr._regen_owner_alive(_owner(os.getpid())) is True
    assert vr._regen_owner_alive(_owner(dead)) is False
    assert asked == [os.getpid(), dead]


@pytest.mark.parametrize("pid", INVALID, ids=INVALID_IDS)
def test_owner_with_an_invalid_pid_is_a_stopped_run(asked, pid):
    """OS には渡さず、owner の pid が無い場合と同じ「止まった実行」として扱います。"""
    assert vr._regen_owner_alive(_owner(pid)) is False
    assert asked == []


def test_owner_pointing_at_this_process_after_truncation_is_not_running():
    """2**32 + 自分の PID を、自分（実行中）と取り違えないこと。"""
    assert vr._regen_owner_alive(_owner(2 ** 32 + os.getpid())) is False


def _run(pid) -> dict:
    return {"since": "2026-01-01T00:00:00", "count": 2, "done": [], "reserved": ["v002", "v003"],
            "owner": _owner(pid)}


@pytest.mark.parametrize("key", ["initial_run", "regen_run"])
@pytest.mark.parametrize("pid", [True, 0, -1, 1.5, "12345", 2 ** 32 + 5, 10 ** 20],
                         ids=["true", "zero", "minus1", "float", "str", "2^32+5", "10^20"])
def test_run_with_an_invalid_owner_pid_is_not_reserved_as_live(asked, key, pid):
    """止まった実行として扱い、予約中の番号を「動いている実行の予約」にしません（例外にしない）。"""
    record = {"variants": [{"variant_id": "v001"}], "next_seq": 4, key: _run(pid)}
    assert vr._live_run_reservations(record) == set()
    if key == "initial_run":
        assert vr.initial_status(record) == "pending"
    else:
        assert vr._regen_pending_run(record) is record["regen_run"]
    assert asked == []


@pytest.mark.parametrize("key", ["initial_run", "regen_run"])
def test_run_with_this_process_as_owner_is_live(key):
    """正しい PID（自分）の実行は、これまでどおり動いている実行です。"""
    record = {"variants": [{"variant_id": "v001"}], "next_seq": 4, key: _run(os.getpid())}
    assert vr._live_run_reservations(record) == {"v002", "v003"}
    if key == "initial_run":
        assert vr.initial_status(record) == "running"
    else:
        assert vr._regen_pending_run(record) is None
