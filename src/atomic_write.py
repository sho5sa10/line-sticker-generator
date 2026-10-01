"""テキストファイルを「一時ファイルに書いてから置き換える」形で保存します（アトミックな保存）。

書き込みの途中で終わっても（電源断・強制終了・ディスクの空き不足など）、元のファイルは置き換えの瞬間まで
変わらず、書きかけのファイルが残りません。改行と文字コードは Path.write_text と同じ変換で書くため、
保存される中身（バイト列）は今までと同じです。アプリの中のほかのモジュールには依存しません。
"""

from __future__ import annotations

import contextlib
import os
import stat
import time
import uuid
from pathlib import Path

# 置き換えが PermissionError のときの再試行の間隔（秒）。Windows では、ウイルス対策ソフトや別の処理が
# ファイルを開いている瞬間に置き換えられないことがあるため、少し待ってやり直します（無限には待ちません）
_REPLACE_RETRY_DELAYS = (0.01, 0.025, 0.05, 0.1, 0.2, 0.4)

# 一時ファイルの名前がぶつかったときに、別の名前で作り直す回数の上限
_TEMP_NAME_ATTEMPTS = 100

# O_BINARY: Windows で C ランタイムの改行の変換を止めます（テキストモードの変換と重なると \r\r\n になるため）。
# O_NOINHERIT: 子プロセスへハンドルを引き継ぎません。どちらも無い環境では付けません
_TEMP_FLAGS = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
               | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0))


def atomic_write_text(
    path,
    text: str,
    *,
    encoding: str = "utf-8",
    newline: str | None = None,
) -> None:
    """text を path へ保存します（Path.write_text(text, encoding=encoding, newline=newline) と同じ中身）。

    失敗したときは、起きた例外をそのまま返します。元のファイルは変わらず、一時ファイルは消します。
    """
    # シンボリックリンクは、今までの write_text と同じくリンク先へ書きます（リンクそのものは置き換えません）
    target = Path(os.path.realpath(path))
    mode = _existing_mode(target)
    fd, tmp = _create_temp(target)
    replaced = False
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline=newline) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        _replace_with_retry(tmp, target)
        replaced = True
    finally:
        if not replaced:
            with contextlib.suppress(OSError):   # 後始末の失敗より、元の失敗を伝えます
                os.unlink(tmp)


def _existing_mode(target: Path) -> int | None:
    """既存のファイルの権限（POSIX）。新しいファイルと Windows では None（権限を付け直しません）。

    Windows の chmod は読み取り専用の属性を切り替えるだけなので、属性を余計に変えないよう何もしません。
    """
    if os.name == "nt":
        return None
    try:
        return stat.S_IMODE(os.stat(target).st_mode)
    except FileNotFoundError:
        return None


def _create_temp(target: Path) -> tuple[int, str]:
    """対象と同じフォルダに一時ファイルを作ります（同じファイルシステムでないと置き換えがアトミックにならないため）。

    tempfile.mkstemp は権限を 0o600 に固定するため使いません。0o666 で作り、今までの write_text と同じく
    OS の umask を適用します。
    """
    for _ in range(_TEMP_NAME_ATTEMPTS):
        tmp = str(target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            return os.open(tmp, _TEMP_FLAGS, 0o666), tmp
        except FileExistsError:
            continue
    raise FileExistsError(f"一時ファイルを作れませんでした: {target.parent}")


def _replace_with_retry(tmp: str, target: Path) -> None:
    """PermissionError のときだけ、間隔を空けて置き換えをやり直します。最後の PermissionError はそのまま返します。"""
    for delay in (*_REPLACE_RETRY_DELAYS, None):
        try:
            os.replace(tmp, target)
            return
        except PermissionError:
            if delay is None:
                raise
            time.sleep(delay)
