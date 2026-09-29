"""ロックファイルの以前の形式（PID だけ）の読み取りで、壊れた中身を例外にしないこと（Phase 7 L-A5）。

以前の形式の PID は _decimal_int で読みます（isdigit() は "²" のように int() が変換できない文字も
真にし、桁数の多すぎる数字は int() が拒むため）。読めない中身は「持ち主の分からない鍵」として、
作られてからの時間で古さを判断します。どの場合も、想定外の例外（ValueError など）を呼び出し元に出さず、
ロックの取得は「使用中」（LockBusyError）か、古い鍵を外して取得、のどちらかになります。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from src import variants as vr

UNREADABLE = ["²", "abc", "9" * 4301, "¹²³"]     # "²"・"abc"・4301 桁・"¹²³"
IDS = ["superscript", "abc", "4301-digits", "superscripts"]


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def _lock(tmp_path, text: str, *, age: float = 0.0):
    path = tmp_path / "variants.run-001.lock"
    path.write_text(text, encoding="utf-8")
    if age:
        past = time.time() - age
        os.utime(path, (past, past))
    return path


# ===========================================================================
# 以前の形式の PID（正常）
# ===========================================================================
@pytest.mark.parametrize("pad", ["", " "], ids=["plain", "spaces"])
def test_legacy_pid_of_a_dead_process_is_stale(tmp_path, pad):
    """"12345" 形式（前後の空白は従来どおり許す）: 持ち主のプロセスが無ければ古い鍵。"""
    assert vr._lock_is_stale(_lock(tmp_path, f"{pad}{_dead_pid()}{pad}")) is True


@pytest.mark.parametrize("pad", ["", " "], ids=["plain", "spaces"])
def test_legacy_pid_of_a_live_process_is_not_stale(tmp_path, pad):
    """"12345" 形式: 持ち主が動いていれば、古くても外さない（PID を読めている）。"""
    assert vr._lock_is_stale(_lock(tmp_path, f"{pad}{os.getpid()}{pad}", age=3600)) is False


# ===========================================================================
# 以前の形式の PID（壊れた中身）: 例外にしない
# ===========================================================================
@pytest.mark.parametrize("text", UNREADABLE, ids=IDS)
def test_unreadable_legacy_pid_does_not_raise(tmp_path, text):
    """読めない中身は、作られた直後なら「使用中」、時間が経っていれば古い鍵（例外にしない）。"""
    assert vr._lock_is_stale(_lock(tmp_path, text)) is False
    assert vr._lock_is_stale(_lock(tmp_path, text, age=vr.LOCK_STALE_SEC + 60)) is True


def test_empty_lock_file_is_handled_as_before(tmp_path):
    assert vr._lock_is_stale(_lock(tmp_path, "")) is False                      # 書き込み途中の猶予
    assert vr._lock_is_stale(_lock(tmp_path, "", age=vr.LOCK_UNWRITTEN_GRACE_SEC + 5)) is True


# ===========================================================================
# 呼び出し元（計画の判定・ロックの取得）まで例外が漏れない
# ===========================================================================
@pytest.mark.parametrize("text", UNREADABLE, ids=IDS)
def test_generation_lock_with_an_unreadable_legacy_pid(tmp_config, text):
    path = tmp_config.variants_path.with_name("variants.run-001.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    assert vr.generation_running_elsewhere(tmp_config, "001") is True           # 作られた直後: 使用中
    with pytest.raises(vr.GenerationBusyError):                                 # 想定外の例外ではない
        with vr.generation_lock(tmp_config, "001"):
            pass

    past = time.time() - (vr.LOCK_STALE_SEC + 60)
    os.utime(path, (past, past))
    assert vr.generation_running_elsewhere(tmp_config, "001") is False          # 時間が経てば古い鍵
    with vr.generation_lock(tmp_config, "001"):                                 # 外して取得できる
        assert path.exists()
    assert not path.exists()


@pytest.mark.parametrize("text", UNREADABLE, ids=IDS)
def test_state_lock_with_an_unreadable_legacy_pid(tmp_config, text):
    """記録の鍵が壊れていても、更新は「使用中」で止まるか、古い鍵を外して進む（例外が漏れない）。"""
    path = tmp_config.variants_path.with_name(tmp_config.variants_path.name + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    with pytest.raises(vr.LockBusyError):
        vr.update(tmp_config, lambda state: None, timeout=0.3)
    past = time.time() - (vr.LOCK_STALE_SEC + 60)
    os.utime(path, (past, past))
    vr.update(tmp_config, lambda state: state.setdefault("stickers", {}), timeout=5)
    assert tmp_config.variants_path.exists() and not path.exists()
