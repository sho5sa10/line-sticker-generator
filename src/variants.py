"""1つのセリフに対する候補画像（variant）の管理。

Phase 1a では **読み取りだけ** を担当します。候補の生成・採用・再生成は行いません。

責務の分け方:
  output/variants/<id>/vNNN.png  候補の履歴（消さない）
  output/generated/<id>.png      いま採用している原画（既存のまま）
  output/final/<id>.png          いま採用している完成画像（既存のまま）
  output/variants.json           候補のメタデータ

既存環境には variants.json がありません。その場合は generated/<id>.png を
「legacy の v001（採用中）」として *読み取り時だけ* 仮想的に扱います。
ファイルのコピーや移動は一切行いません。
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

SCHEMA = 1

# 候補の出どころ
SOURCE_API = "api"          # 画像生成APIで作った
SOURCE_IMPORT = "import"    # 手持ち画像を取り込んだ
SOURCE_LEGACY = "legacy"    # variants.json 導入前からある generated/<id>.png
SOURCE_RECOVERED = "recovered"  # 記録を失ったあと、候補フォルダの画像から復元した

# 人間の判断。Phase 1a では読み取るだけで、変更するAPIは作りません。
VERDICT_PENDING = "pending"
VERDICT_ADOPTED = "adopted"
VERDICT_REJECTED = "rejected"
VERDICT_REGEN = "regen"
VERDICTS = (VERDICT_PENDING, VERDICT_ADOPTED, VERDICT_REJECTED, VERDICT_REGEN)
# 人が手で設定できる値。adopted は「採用」操作の結果としてのみ付きます
# （いま採用中かどうかは sticker.adopted が正のため）。
SETTABLE_VERDICTS = (VERDICT_PENDING, VERDICT_REJECTED, VERDICT_REGEN)

RATING_MIN = 1
RATING_MAX = 5


# ---------------------------------------------------------------------------
# データ構造
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Variant:
    """候補1件。file はプロジェクトからの相対パス（フォルダを移動しても動くように）。"""

    variant_id: str
    file: str
    source: str = SOURCE_API
    verdict: str = VERDICT_PENDING
    created_at: str | None = None
    human_rating: int | None = None
    note: str = ""
    flags: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def path(self, config) -> Path:
        p = Path(self.file)
        return p if p.is_absolute() else (Path(config.root) / p)

    def exists(self, config) -> bool:
        return self.path(config).exists()

    def to_dict(self) -> dict:
        data = {
            "variant_id": self.variant_id,
            "file": self.file,
            "created_at": self.created_at,
            "source": self.source,
            "verdict": self.verdict,
            "human_rating": self.human_rating,
            "note": self.note,
            "flags": list(self.flags),
        }
        data.update(self.extra)
        return data


@dataclass
class StickerVariants:
    """1スタンプ分の候補一覧。"""

    sticker_id: str
    adopted: str | None = None          # いま採用している候補ID（これが正）
    adopted_at: str | None = None
    next_seq: int = 1
    variants: list[Variant] = field(default_factory=list)
    legacy: bool = False                # variants.json に記録が無く、仮想的に作った
    # 採用したときの generated/<id>.png と、いまのファイルが食い違っているか
    # （「AIで作り直す」「画像を入れる」など、採用以外の操作で差し替わった場合に True）
    generated_mismatch: bool = False
    # 採用に失敗し、さらに元に戻す処理も失敗した記録（画像が採用前と違う可能性がある）
    rollback_failed: dict | None = None

    @property
    def variant_count(self) -> int:
        return len(self.variants)

    def find(self, variant_id: str) -> Variant | None:
        return next((v for v in self.variants if v.variant_id == variant_id), None)

    def adopted_variant(self) -> Variant | None:
        return self.find(self.adopted) if self.adopted else None

    def to_dict(self) -> dict:
        return {
            "sticker_id": self.sticker_id,
            "adopted": self.adopted,
            "adopted_at": self.adopted_at,
            "next_seq": self.next_seq,
            "legacy": self.legacy,
            "variant_count": self.variant_count,
            "generated_mismatch": self.generated_mismatch,
            "rollback_failed": self.rollback_failed,
            "variants": [v.to_dict() for v in self.variants],
        }


# ---------------------------------------------------------------------------
# パス
# ---------------------------------------------------------------------------
def variant_dir(config, sticker_id: str) -> Path:
    """候補の置き場 output/variants/<id>/。Phase 1a では作成しません。"""
    return config.dir_variants / sticker_id


def relative_file(config, path: str | Path) -> str:
    """プロジェクト内なら相対パス（/ 区切り）にします。外ならそのまま。"""
    p = Path(path)
    try:
        return p.resolve().relative_to(Path(config.root).resolve()).as_posix()
    except (ValueError, OSError):
        return str(path)


# ---------------------------------------------------------------------------
# 読み書き
# ---------------------------------------------------------------------------
STATE_MISSING = "missing"
STATE_OK = "ok"
STATE_CORRUPT = "corrupt"


# Windows では、他の処理が開いているファイルの読み取り・置き換えが一時的に
# PermissionError になります。壊れているわけではないので、短い間隔で再試行します。
IO_RETRY_DELAYS = (0.01, 0.025, 0.05, 0.1, 0.2, 0.4)
REPAIR_COMMAND = "python -m src.variants repair"


def _retry_io(fn):
    """PermissionError のときだけ、決まった回数だけ待って再試行します（無限には待ちません）。"""
    for delay in (*IO_RETRY_DELAYS, None):
        try:
            return fn()
        except PermissionError:
            if delay is None:
                raise
            time.sleep(delay)


def _replace_with_retry(src: Path, dest: Path) -> None:
    _retry_io(lambda: _replace_file(src, dest))


def _replace_file(src, dest) -> None:
    """src で dest を原子的に置き換えます（テストで失敗を注入する入口もここ）。

    Windows では POSIX 意味論の名前変更を使います。os.replace（MoveFileEx）は、
    置き換え先を誰かが開いていると必ず失敗しますが、こちらは読み取り側が
    FILE_SHARE_DELETE で開いていれば置き換えられます（Windows 10 1709 以降・NTFS）。
    使えない環境では os.replace に戻ります。
    """
    if os.name == "nt" and _posix_replace_windows(src, dest):
        return
    os.replace(src, dest)


def _posix_replace_windows(src, dest) -> bool:
    """成功なら True、この方式が使えない環境なら False（呼び出し側が os.replace に切り替え）。"""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                    ctypes.c_void_p, wintypes.DWORD]
    kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
    delete_access, generic_read, share_all, open_existing, normal = (
        0x00010000, 0x80000000, 0x7, 3, 0x80)
    handle = kernel32.CreateFileW(str(src), delete_access | generic_read, share_all, None,
                                  open_existing, normal, None)
    if handle is None or handle == wintypes.HANDLE(-1).value:
        _raise_windows_error(ctypes.get_last_error(), src)
    try:
        name = os.path.abspath(str(dest))

        class RenameInfo(ctypes.Structure):
            _fields_ = [("Flags", wintypes.DWORD), ("RootDirectory", wintypes.HANDLE),
                        ("FileNameLength", wintypes.DWORD),
                        ("FileName", wintypes.WCHAR * (len(name) + 1))]

        replace_if_exists, posix_semantics, file_rename_info_ex = 0x1, 0x2, 22
        info = RenameInfo(replace_if_exists | posix_semantics, None, len(name) * 2, name)
        if kernel32.SetFileInformationByHandle(handle, file_rename_info_ex, ctypes.byref(info),
                                               ctypes.sizeof(info)):
            return True
        err = ctypes.get_last_error()
        if err in (1, 50, 87, 124):     # この方式に未対応（古いWindows・FAT・ネットワーク共有など）
            return False
        _raise_windows_error(err, dest)
    finally:
        kernel32.CloseHandle(handle)


def _raise_windows_error(err: int, path) -> None:
    import ctypes

    message = ctypes.FormatError(err).strip()
    if err in (2, 3):
        raise FileNotFoundError(2, message, str(path))
    if err in (5, 32, 33):                  # アクセス拒否・共有違反・ロック違反（一時的なことが多い）
        raise PermissionError(13, message, str(path))
    raise OSError(err, message, str(path))


def _read_file_bytes(path: Path) -> bytes:
    """ファイルを読みます。Windows では、読んでいる間も置き換え（os.replace）を妨げない開き方にします。

    Python の open() は Windows で「削除・名前変更の共有」を許さないため、誰かが読んでいる
    瞬間に保存（置き換え）すると PermissionError になります。GUI は頻繁に記録を読むので、
    読み取り側が FILE_SHARE_DELETE を付けて開き、保存が失敗しないようにします。
    """
    if os.name != "nt":
        return Path(path).read_bytes()
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    generic_read, share_all, open_existing, normal = 0x80000000, 0x7, 3, 0x80
    handle = create(str(path), generic_read, share_all, None, open_existing, normal, None)
    if handle is None or handle == wintypes.HANDLE(-1).value:
        _raise_windows_error(ctypes.get_last_error(), path)
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
    except OSError:
        kernel32.CloseHandle(handle)
        raise
    with os.fdopen(fd, "rb") as f:
        return f.read()


def _read_state_file(config) -> tuple[str, dict | None]:
    """variants.json を1回だけ読み、(状態, 中身) を返します。

    「無い」「壊れている（JSONとして読めない・形が違う）」「一時的に読めない」を区別します。
    一時的に読めない場合は StateReadError にします（壊れている扱いにはしません）。
    """
    path = config.variants_path
    try:
        # BOM 付き UTF-8（メモ帳などで保存）も正常として読みます。書き込みは BOM なしです
        text = _retry_io(lambda: _read_file_bytes(path)).decode("utf-8-sig")
    except FileNotFoundError:
        return STATE_MISSING, None
    except ValueError:              # 文字コードとして読めない（UnicodeDecodeError）
        return STATE_CORRUPT, None
    except OSError as exc:
        raise StateReadError(
            f"候補の記録を一時的に読めませんでした（他の処理が使用中の可能性）: {path}\n"
            f"  少し待ってからやり直してください（{type(exc).__name__}: {exc}）") from exc
    try:
        data = json.loads(text)
    except ValueError:
        return STATE_CORRUPT, None
    if not isinstance(data, dict) or not isinstance(data.get("stickers"), dict):
        return STATE_CORRUPT, None
    return STATE_OK, data


def state_status(config) -> str:
    """variants.json の状態。"missing" / "ok" / "corrupt"。

    「ファイルが無い」と「壊れている」を区別します。壊れているときに空の状態で
    上書きすると、採用状態や人の判断がすべて消えてしまうためです。
    一時的に読めないだけのときは StateReadError を送出します（壊れている扱いにしません）。
    """
    return _read_state_file(config)[0]


def is_corrupt(config) -> bool:
    return state_status(config) == STATE_CORRUPT


# TODO(main.py の変更が解禁されたら): 記録が壊れているとき、`generate --variants` と
# `variants score` はトレースバックで終わる（API は呼ばず、記録も変えない）。
# StateCorruptError を捕まえて、この corrupt_message() だけを表示し、終了コード 1 にする。
def corrupt_message(config) -> str:
    return (f"候補の記録が壊れているため保存できません: {config.variants_path}\n"
            f"  中身を直すか、`{REPAIR_COMMAND}` で修復してください"
            "（壊れたファイルは消さずに別名で残し、候補フォルダの画像から記録を作り直します。"
            "判断・評価は失われます）。")


def quarantine_corrupt_state(config) -> Path | None:
    """壊れた variants.json を variants.json.corrupt-<日時>-<一意ID> へ退避します（削除しません）。

    同じ秒に何度実行しても、前に退避したファイルを上書きしません。
    """
    path = config.variants_path
    with state_lock(config):
        if state_status(config) != STATE_CORRUPT:
            return None
        while True:
            dest = path.with_name(
                f"{path.name}.corrupt-{datetime.now():%Y%m%d_%H%M%S}-{uuid.uuid4().hex[:8]}")
            if not dest.exists():
                break
        _replace_with_retry(path, dest)
        return dest


def load(config) -> dict:
    """variants.json を読みます。

    無い場合・壊れている場合は空の状態を返します。
    壊れている場合だけ、既存の StateStore と同じ方式で警告を出します。
    ただし、この状態のまま save() すると記録を失うため、保存側で拒否します。
    一時的に読めない場合は StateReadError を送出します。
    """
    status, data = _read_state_file(config)
    if status == STATE_MISSING:
        return _empty_state()
    if status == STATE_CORRUPT:
        print(f"WARNING: 候補の記録を読めないため、generated/ の画像だけを使います: "
              f"{config.variants_path}", file=sys.stderr)
        return _empty_state()
    data.setdefault("schema", SCHEMA)
    data.setdefault("defaults", {})
    return data


def save(config, data: dict, *, force: bool = False) -> Path:
    """variants.json を原子的に書き換えます（一意な一時ファイルに書いてから置換）。

    壊れたファイルがある状態では、既存の記録を消さないために拒否します
    （force=True は、退避したあとの書き込みなど、明示的に上書きしたい場合だけ）。
    通常は直接呼ばず、update() を通してください（鍵と読み直しのため）。
    """
    if not force and is_corrupt(config):
        raise StateCorruptError(corrupt_message(config))
    path = config.variants_path
    out = dict(data)
    out["schema"] = SCHEMA
    out["updated_at"] = datetime.now().isoformat(timespec="seconds")
    out.setdefault("defaults", {})
    out.setdefault("stickers", {})
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_bytes(path, json.dumps(out, ensure_ascii=False, indent=2).encode("utf-8"))
    return path


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    """同じフォルダの一意な一時ファイルへ書き、fsync してから置き換えます。

    一時ファイル名を毎回変えるのは、同時に保存する処理どうしが同じ一時ファイルを
    上書きし合うと、中身が混ざった JSON が出来てしまうためです。
    """
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        _replace_with_retry(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _empty_state() -> dict:
    return {"schema": SCHEMA, "defaults": {}, "stickers": {}}


# ---------------------------------------------------------------------------
# 排他制御（同時に書き込んでも、あとから来た更新で前の更新が消えないように）
# ---------------------------------------------------------------------------
LOCK_TIMEOUT_SEC = 20.0
# 持ち主を確かめられない鍵（別のPCが作った・中身が読めない）を古いとみなすまでの時間
LOCK_STALE_SEC = 120.0
# 作られた直後で、まだ持ち主が書かれていない鍵を「作りかけ」とみなす時間
LOCK_UNWRITTEN_GRACE_SEC = 5.0
# 古い鍵を外す係（*.break.lock）が異常終了で残ったとみなす時間
LOCK_BREAKER_STALE_SEC = 10.0
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
_HELD = threading.local()            # このスレッドがいま持っている鍵（入れ子で取れるように）


def _thread_lock(key: str) -> threading.RLock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def _held_keys() -> set:
    held = getattr(_HELD, "keys", None)
    if held is None:
        held = _HELD.keys = set()
    return held


@contextmanager
def file_lock(lock_path: Path, timeout: float = LOCK_TIMEOUT_SEC, *, wait: bool = True):
    """指定のロックファイルで、プロセス内（GUIの複数リクエスト）とプロセス間を排他します。

    同じスレッドがすでに持っている鍵は、そのまま入れ子で使えます
    （ロックファイルを二重に作ろうとして自分自身を待ち続けないように）。
    wait=False なら待たずに1回だけ試し、取れなければ LockBusyError にします。
    timeout は「同じプロセスの他のスレッドを待つ時間」と「他のプロセスを待つ時間」の合計です。
    スレッド間の待ちにも上限を付けるのは、万一鍵の順序が崩れて待ち合っても、GUI が
    永久に止まらず「使用中」のエラーとして返すためです。
    """
    lock_path = Path(lock_path)
    key = str(lock_path)
    held = _held_keys()
    if key in held:
        yield
        return
    deadline = time.monotonic() + timeout
    rlock = _thread_lock(key)
    acquired = rlock.acquire(timeout=max(timeout, 0.0)) if wait else rlock.acquire(blocking=False)
    if not acquired:
        raise LockBusyError(
            f"候補の記録が他の処理で使用中です（{lock_path}）。しばらく待ってからやり直してください。")
    try:
        held.add(key)
        try:
            remaining = max(deadline - time.monotonic(), 0.0) if wait else 0.0
            with _exclusive_file(lock_path, remaining):
                yield
        finally:
            held.discard(key)
    finally:
        rlock.release()


# --- 鍵の持ち主（強制終了で残った鍵を見分けるため） -------------------------
def _this_host() -> str:
    import socket

    return socket.gethostname()


def _process_start(pid: int) -> tuple[bool, int | None]:
    """(生きているか, 起動時刻の目印)。起動時刻は PID の再利用を見分けるために使います。

    確かめられない場合（権限がない等）は「生きている・起動時刻は不明」とみなします
    （誤って他人の鍵を外すより、待つほうが安全なため）。
    """
    if pid <= 0:
        return False, None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        query_limited, still_active = 0x1000, 259
        handle = kernel32.OpenProcess(query_limited, False, pid)
        if not handle:
            err = ctypes.get_last_error()
            return (False, None) if err == 87 else (True, None)   # 87: その PID のプロセスは無い
        try:
            code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value != still_active:
                return False, None                                 # 終了済み（ハンドルだけ残っている）
            times = [wintypes.FILETIME() for _ in range(4)]
            if kernel32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
                created = times[0]
                return True, (created.dwHighDateTime << 32) | created.dwLowDateTime
            return True, None
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False, None
    except PermissionError:
        return True, None
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return True, int(fields[19])                               # starttime
    except (OSError, IndexError, ValueError):
        return True, None


_SELF_START = None


def _lock_owner_info() -> bytes:
    global _SELF_START
    if _SELF_START is None:
        _SELF_START = _process_start(os.getpid())[1]
    return json.dumps({"pid": os.getpid(), "host": _this_host(), "started": _SELF_START,
                       "created": time.time()}).encode("utf-8")


def _lock_is_stale(lock_path: Path) -> bool:
    """残っている鍵が、もう誰も持っていない（古い）か。

    - 同じPCの鍵: 持ち主のプロセスが無い、または PID が別のプロセスに再利用されている → 古い
    - 別のPCの鍵・持ち主が読めない鍵: 作られてからの時間で判断（LOCK_STALE_SEC）
    - 作られた直後でまだ持ち主が書かれていない鍵は、少し待ちます
    """
    try:
        stat = lock_path.stat()
        text = _retry_io(lambda: _read_file_bytes(lock_path)).decode("utf-8", "replace").strip()
    except FileNotFoundError:
        return False                      # もう無い（外す必要もない）
    except OSError:
        return False
    age = time.time() - stat.st_mtime
    if not text:
        return age > LOCK_UNWRITTEN_GRACE_SEC
    try:
        info = json.loads(text)
        if not isinstance(info, dict):
            raise ValueError
    except ValueError:
        info = {"pid": int(text)} if text.isdigit() else {}   # 以前の形式（PID だけ）
    pid = info.get("pid")
    if not isinstance(pid, int):
        return age > LOCK_STALE_SEC
    if info.get("host") not in (None, _this_host()):
        return age > LOCK_STALE_SEC       # 別のPCのプロセスは確かめられない
    alive, started = _process_start(pid)
    if not alive:
        return True
    recorded = info.get("started")
    return recorded is not None and started is not None and recorded != started


def _break_stale_lock(lock_path: Path) -> bool:
    """古い鍵を外します。外す係は1つの処理だけで、係になってから古さを確かめ直します。

    （2つの処理が同時に「古い」と判断して、片方が取り直したばかりの鍵をもう片方が
    消してしまうことを防ぐため）
    """
    breaker = lock_path.with_suffix(".break.lock")
    try:
        fd = os.open(breaker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            if time.time() - breaker.stat().st_mtime > LOCK_BREAKER_STALE_SEC:
                breaker.unlink(missing_ok=True)       # 係が異常終了して残ったもの
        except OSError:
            pass
        return False
    except PermissionError:
        return False
    try:
        os.close(fd)
        if _lock_is_stale(lock_path):
            _retry_io(lambda: lock_path.unlink(missing_ok=True))
            return True
        return False
    finally:
        _retry_io(lambda: breaker.unlink(missing_ok=True))


@contextmanager
def _exclusive_file(lock_path: Path, timeout: float):
    """ロックファイルを O_EXCL で作ってプロセス間を排他します（持ち主の情報を書き込みます）。"""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    fd = None
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if _break_stale_lock(lock_path):   # 強制終了などで残った鍵なら、すぐ取り直す
                continue
        except PermissionError:
            pass    # Windows: 削除処理中のロックファイルは一時的に開けません
        if time.monotonic() >= deadline:
            raise LockBusyError(
                f"候補の記録が他の処理で使用中です（{lock_path}）。"
                "しばらく待ってからやり直してください。") from None
        time.sleep(0.05)
    try:
        os.write(fd, _lock_owner_info())
        os.close(fd)
        fd = None
        yield
    finally:
        if fd is not None:
            os.close(fd)
        _retry_io(lambda: lock_path.unlink(missing_ok=True))


def state_lock(config, timeout: float = LOCK_TIMEOUT_SEC):
    """variants.json を書き換える間だけ取る鍵。"""
    return file_lock(Path(str(config.variants_path) + ".lock"), timeout)


def _adopt_lock_path(config, sticker_id: str) -> Path:
    return config.variants_path.with_name(f"variants.adopt-{sticker_id}.lock")


def adopt_lock(config, sticker_id: str, timeout: float = LOCK_TIMEOUT_SEC):
    """1スタンプの採用処理の鍵。

    採用は generated/<id>.png と final/<id>.png を書き換えるため、同じスタンプの採用が
    並行すると「記録は A、画像は B」という食い違いが起きます。状態ファイルの鍵とは別に、
    スタンプ単位で採用だけを直列化します（別のスタンプの採用や、評価・判断は止めません）。
    """
    return file_lock(_adopt_lock_path(config, sticker_id), timeout)


# --- 生成の実行権（Phase 7 STEP 2e） ----------------------------------------
# 同じスタンプの候補生成（GUI の初回生成・再生成、CLI の generate --variants）を、
# プロセスをまたいで同時に走らせないための鍵です。生成を始めるときに待たずに1回だけ取り、
# 生成が終わるまで持ちます（API の呼び出し中も）。記録の鍵・スタンプの鍵とは別物で、
# 採用・repair・候補の登録はこの鍵を取りません（止めません）。取れる・取れないの判定と開始を、
# ロックファイルの作成1回で行うので、「確認してから始める」間の隙間はありません。
# 異常終了で残った鍵は、ほかの鍵と同じく持ち主（pid・ホスト・起動時刻）で古さを判定して外します。
GENERATION_BUSY_MESSAGE = "このスタンプは別の処理で生成中です（GUI または CLI）。完了を待ってから実行してください"


def _generation_lock_path(config, sticker_id: str) -> Path:
    return config.variants_path.with_name(f"variants.run-{sticker_id}.lock")


@contextmanager
def generation_lock(config, sticker_id: str):
    """1スタンプの生成の実行権。取れなければ GenerationBusyError（待ちません）。"""
    locks = ExitStack()
    try:
        locks.enter_context(file_lock(_generation_lock_path(config, sticker_id), wait=False))
    except LockBusyError as exc:
        raise GenerationBusyError(f"{sticker_id}: {GENERATION_BUSY_MESSAGE}") from exc
    with locks:
        yield


def _claim_generation(config, sticker_id: str) -> ExitStack:
    """実行権を取って返します（with で使うと、抜けるときに返します）。取れなければ GenerationBusyError。"""
    locks = ExitStack()
    locks.enter_context(generation_lock(config, sticker_id))
    return locks


def generation_running_elsewhere(config, sticker_id: str) -> bool:
    """ほかの処理（別のスレッド・プロセス）が、このスタンプの実行権を持っているか（計画・表示用）。

    実際に始めるかどうかは generation_lock の取得で決まります（ここは確認だけで、排他はしません）。
    """
    path = _generation_lock_path(config, sticker_id)
    if str(path) in _held_keys() or not path.exists():
        return False
    return not _lock_is_stale(path)


UNCHANGED = object()    # update() の mutate がこれを返したら、保存しません（変更なし）


def update(config, mutate, *, timeout: float = LOCK_TIMEOUT_SEC):
    """鍵を取り、**最新の記録を読み直してから** 変更を適用して保存します。

    古い状態をメモリに持ったまま保存すると、その間に入った他の更新
    （評価・判断・採用）が消えてしまうため、必ずこの入口を通します。

    Args:
        mutate: 最新の state を受け取って書き換える関数。戻り値はそのまま返します。
            UNCHANGED を返した場合は保存しません（repair の「変更なし」など）。
    """
    with state_lock(config, timeout):
        status, state = _read_state_file(config)
        if status == STATE_CORRUPT:
            raise StateCorruptError(corrupt_message(config))
        if state is None:
            state = _empty_state()
        state.setdefault("schema", SCHEMA)
        state.setdefault("defaults", {})
        result = mutate(state)
        if result is UNCHANGED:
            return result
        try:
            save(config, state)
        except PermissionError as exc:      # 再試行しても置き換えられなかった（元のファイルは無傷）
            raise StateWriteError(
                f"候補の記録を一時的に保存できませんでした（他の処理が使用中の可能性）: "
                f"{config.variants_path}\n  少し待ってからやり直してください（{exc}）") from exc
        return result


def update_sticker(config, sticker_id: str, mutate, *, timeout: float = LOCK_TIMEOUT_SEC):
    """1スタンプの記録を変更する update()。

    記録の無いスタンプでは、変更の中で generated/<id>.png を v001 としてコピーします。
    その画像は取り込み・採用がスタンプ単位の鍵を持って書き換えるため、コピーも同じ鍵を
    持って行う必要があります。鍵の順序は「スタンプの鍵 → 記録の鍵」（採用と同じ）です。
    普段は記録の鍵だけで済ませ、コピーが必要なのにスタンプの鍵を取れなかったときだけ、
    スタンプの鍵を先に取ってからやり直します（順序を逆にして待つと、互いに待ち合って止まるため）。
    """
    try:
        return update(config, mutate, timeout=timeout)
    except StickerLockNeeded:
        with adopt_lock(config, sticker_id, timeout):
            return update(config, mutate, timeout=timeout)


@contextmanager
def _sticker_lock_for_copy(config, sticker_id: str):
    """generated を v001 にコピーする間、スタンプの鍵を持ちます。

    すでに持っていればそのまま。持っていなければ待たずに1回だけ試し、他の処理
    （取り込み・採用）が持っていれば StickerLockNeeded にします（呼び出し側がやり直す）。
    """
    try:
        with file_lock(_adopt_lock_path(config, sticker_id), wait=False):
            yield
    except LockBusyError as exc:
        raise StickerLockNeeded(
            f"同じスタンプを他の処理（取り込み・採用）が使用中です: {sticker_id}") from exc


# ---------------------------------------------------------------------------
# 取得
# ---------------------------------------------------------------------------
def ensure_legacy_variant(config, sticker_id: str, record=None) -> StickerVariants | None:
    """記録が無いスタンプを、generated/<id>.png を指す仮想の v001 として扱います。

    **ファイルのコピー・移動はしません。variants.json も作りません。**
    generated/<id>.png が無ければ None を返します。
    候補0件の記録（record）があれば、その予約済みの番号は避けます（_legacy_slot を参照）。
    """
    src = config.dir_generated / f"{sticker_id}.png"
    if not src.exists():
        return None
    # 実体化したときと同じ番号で見せます（置き場に別の画像の v001 があれば、その次）
    slot, _reuse = _legacy_slot(config, sticker_id, src, record=record)
    vid = slot.stem
    v = Variant(
        variant_id=vid,
        file=relative_file(config, src),
        source=SOURCE_LEGACY,
        verdict=VERDICT_ADOPTED,
        created_at=None,
    )
    return StickerVariants(sticker_id=sticker_id, adopted=vid, next_seq=int(vid[1:]) + 1,
                           variants=[v], legacy=True)


def _legacy_slot(config, sticker_id: str, src: Path, src_sha: str | None = None, *,
                 record=None) -> tuple[Path, bool]:
    """legacy の画像を置く場所と、その場所の既存ファイルをそのまま使えるか。

    置き場に v001.png が無ければ v001（普段はここで終わり、ハッシュも計算しません）。
    ある場合は: 中身が同じ → そのまま使う / 読めない → 作り直す / 別の画像 → 次の番号。
    予約済みの番号は使いません: 記録の next_seq より前の番号、記録にある候補・実行の記録
    （initial_run / regen_run）の予約と完了の番号、書き込み途中の .png.part がある番号は飛ばします
    （ファイルが無いだけで空いているとは限らないため。課金済みの画像を原画で隠さないように）。
    """
    folder = variant_dir(config, sticker_id)
    seq, used = _legacy_consumed(record)
    while True:
        dest = folder / f"v{seq:03d}.png"
        if dest.stem in used or dest.with_suffix(".png.part").exists():
            seq += 1
            continue
        if not dest.exists():
            return dest, False
        src_sha = src_sha or sha1_file(src)
        if sha1_file(dest) == src_sha:
            return dest, True
        if not _is_readable_png(dest):
            return dest, False          # 半端なコピーなど。作り直します
        seq += 1                        # 別の候補の画像。消さずに次の番号へ


def _legacy_consumed(record) -> tuple[int, set[str]]:
    """legacy の番号を探し始める番号（記録の next_seq）と、使えない番号（候補・実行の予約と完了）。"""
    if not isinstance(record, dict):
        return 1, set()
    used = {str(v.get("variant_id")) for v in record.get("variants") or [] if isinstance(v, dict)}
    for key in ("initial_run", "regen_run"):
        run = record.get(key)
        if isinstance(run, dict):
            for field in ("reserved", "done"):
                if isinstance(run.get(field), list):
                    used.update(v for v in run[field] if isinstance(v, str))
    seq = record.get("next_seq")
    start = seq if isinstance(seq, int) and not isinstance(seq, bool) and seq >= 1 else 1
    return start, used


def _initial_run_open(record) -> bool:
    """止まった（または実行中の）初回生成の記録があるか。完了すると initial_run は消えます。"""
    return isinstance(record, dict) and isinstance(record.get("initial_run"), dict)


def get_sticker(config, sticker_id: str, data: dict | None = None) -> StickerVariants | None:
    """1スタンプ分の候補。記録があればそれを優先し、無ければ legacy として返します。"""
    state = load(config) if data is None else data
    record = (state.get("stickers") or {}).get(sticker_id)
    if not isinstance(record, dict):
        return ensure_legacy_variant(config, sticker_id)

    variants: list[Variant] = []
    for item in record.get("variants") or []:
        v = _variant_from_dict(item)
        if v is not None:
            variants.append(v)
    if not variants:
        if _initial_run_open(record):
            # 初回生成の途中（止まった実行の続きが先）。原画は候補として見せません
            return None
        # 記録はあるが候補が1件も読めない場合も、既存画像で動けるようにします。
        return ensure_legacy_variant(config, sticker_id, record)

    adopted = record.get("adopted")
    if adopted is not None and not any(v.variant_id == adopted for v in variants):
        adopted = None      # 実在しない候補を指していたら「未採用」として扱います
    next_seq = record.get("next_seq")
    if not isinstance(next_seq, int) or next_seq < len(variants) + 1:
        next_seq = len(variants) + 1
    return StickerVariants(
        sticker_id=sticker_id,
        adopted=adopted,
        adopted_at=record.get("adopted_at"),
        next_seq=next_seq,
        variants=variants,
        legacy=False,
        generated_mismatch=_generated_mismatch(config, sticker_id, record) if adopted else False,
        rollback_failed=record.get("rollback_failed")
        if isinstance(record.get("rollback_failed"), dict) else None,
    )


def _generated_mismatch(config, sticker_id: str, record: dict) -> bool:
    """採用時に記録した目印と、いまの generated/<id>.png を比べます。

    まずサイズ・更新日時だけを見て、違うときにだけ SHA1 を計算します
    （毎回すべてのスタンプでハッシュを取ると一覧表示が重くなるため）。
    """
    stamp = record.get("adopted_file")
    if not isinstance(stamp, dict):
        return False                      # 採用時の記録が無い（古いデータ）ときは判定しません
    path = config.dir_generated / f"{sticker_id}.png"
    try:
        stat = path.stat()
    except OSError:
        return True                       # 採用したはずの原画が無い
    if stat.st_size == stamp.get("size") and stat.st_mtime_ns == stamp.get("mtime_ns"):
        return False
    return sha1_file(path) != stamp.get("sha1")


def _variant_from_dict(item) -> Variant | None:
    if not isinstance(item, dict):
        return None
    vid = str(item.get("variant_id") or "").strip()
    file = str(item.get("file") or "").strip()
    if not vid or not file:
        return None
    verdict = str(item.get("verdict") or VERDICT_PENDING)
    rating = item.get("human_rating")
    known = {"variant_id", "file", "created_at", "source", "verdict", "human_rating",
             "note", "flags"}
    return Variant(
        variant_id=vid,
        file=file,
        source=str(item.get("source") or SOURCE_API),
        verdict=verdict if verdict in VERDICTS else VERDICT_PENDING,
        created_at=item.get("created_at"),
        human_rating=rating if isinstance(rating, int) else None,
        note=str(item.get("note") or ""),
        flags=[str(f) for f in (item.get("flags") or [])],
        extra={k: v for k, v in item.items() if k not in known},
    )


def list_variants(config, sticker_id: str, data: dict | None = None) -> list[Variant]:
    """1スタンプの候補一覧（記録が無ければ legacy の1件）。"""
    sticker = get_sticker(config, sticker_id, data)
    return list(sticker.variants) if sticker else []


def get_adopted(config, sticker_id: str, data: dict | None = None) -> Variant | None:
    """いま採用している候補。未採用・画像なしなら None。"""
    sticker = get_sticker(config, sticker_id, data)
    return sticker.adopted_variant() if sticker else None


def sha1_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def sha1_file(path: str | Path) -> str | None:
    """ファイルのSHA1。読めない場合は None。"""
    p = Path(path)
    try:
        h = hashlib.sha1()
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


# ---------------------------------------------------------------------------
# 候補の追加（Phase 1b: 生成のみ。採用は行いません）
# ---------------------------------------------------------------------------
def ensure_record(config, data: dict, sticker_id: str) -> dict:
    """variants.json 内の1スタンプ分の記録を用意します（無ければ作る）。

    記録が無く generated/<id>.png がある環境では、その画像を候補置き場へコピーして
    v001(legacy・採用中) として記録に残し、原画の目印（adopted_file）も付けます。

    記録はあるが候補が0件（variants=[]）の場合は、その記録をそのまま使います。
    番号の予約（next_seq）・実行中の状態・知らない項目を消さないためです
    （作り直すと、予約済みの番号を別の処理がもう一度使ってしまいます）。
    原画を取り込むときは、予約済みの番号を避けた番号に置きます（_legacy_slot を参照）。

    初回生成の途中（initial_run がある）の記録には、原画を取り込みません。止まった初回生成の
    続き（予約した番号・前回できていた画像）が先で、原画でその枠を埋めないためです。
    """
    stickers = data.setdefault("stickers", {})
    record = stickers.get(sticker_id)
    if isinstance(record, dict) and record.get("variants"):
        record.setdefault("next_seq", len(record["variants"]) + 1)
        return record

    legacy = None if _initial_run_open(record) else ensure_legacy_variant(config, sticker_id, record)
    if legacy is not None:
        # 記録に残す時点で、候補置き場へ実体をコピーします。
        # generated/<id>.png は採用のたびに中身が変わるため、そこを指したままだと
        # v001 が「いま採用中の画像」の別名になり、元の絵が追えなくなります。
        with _sticker_lock_for_copy(config, sticker_id):
            kept, stamp = _materialize_legacy(config, sticker_id, legacy.variants[0], record)
        seq = int(kept.variant_id[1:])
        fresh = {
            "adopted": kept.variant_id,
            "adopted_at": None,
            "next_seq": max(legacy.next_seq, seq + 1),
            "variants": [kept.to_dict()],
            # いまの generated/<id>.png の目印。これ以降の差し替えに気付けるようにします
            "adopted_file": stamp,
        }
    else:
        fresh = {"adopted": None, "adopted_at": None, "next_seq": 1, "variants": []}
    if not isinstance(record, dict):
        stickers[sticker_id] = fresh
        return fresh

    # 候補0件の既存の記録: 候補と採用の項目だけを整え、それ以外はそのまま残します
    reserved = record.get("next_seq")
    if legacy is not None:
        record.update({k: v for k, v in fresh.items() if k != "next_seq"})
    else:
        for key, value in fresh.items():
            record.setdefault(key, value)
        if not isinstance(record.get("variants"), list):
            record["variants"] = []
    valid = isinstance(reserved, int) and not isinstance(reserved, bool) and reserved >= 1
    record["next_seq"] = max(reserved, fresh["next_seq"]) if valid else fresh["next_seq"]
    return record


def _is_complete_png(path: Path) -> bool:
    """最後まで読み込める PNG か（書きかけのファイルを候補として保存しないため）。"""
    from PIL import Image

    try:
        with Image.open(path) as img:
            img.load()
        return True
    except Exception:  # noqa: BLE001 - 読めない理由は問いません
        return False


def _is_readable_png(path: Path) -> bool:
    from PIL import Image

    try:
        with Image.open(path) as img:
            img.verify()
        return True
    except Exception:  # noqa: BLE001 - 読めない理由は問いません
        return False


def _materialize_legacy(config, sticker_id: str, variant: Variant, record=None) -> tuple[Variant, dict]:
    """legacy の候補（generated/<id>.png）を候補置き場へコピーして、そちらを指させます。

    コピーは「一時ファイル → SHA1確認 → 置き換え」で行い、途中で失敗しても半端な
    vNNN.png を残しません。置き場所の決め方は _legacy_slot() を参照（record の予約を避けます）。
    Returns:
        (候補, generated の目印 {size, mtime_ns, sha1})
    """
    src = variant.path(config)
    if not _is_complete_png(src):
        raise VariantError(f"原画を読めないため、候補として保存できません（書き込み中の可能性）: {src}")
    stat = src.stat()
    src_sha = sha1_file(src)
    stamp = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha1": src_sha}
    dest, reuse = _legacy_slot(config, sticker_id, src, src_sha, record=record)
    if not reuse:
        _copy_verified(src, dest, src_sha)
    return _with_id_and_file(config, variant, dest), stamp


def _with_id_and_file(config, variant: Variant, path: Path) -> Variant:
    return dataclasses.replace(_with_file(config, variant, path), variant_id=path.stem)


def _with_file(config, variant: Variant, path: Path) -> Variant:
    return Variant(
        variant_id=variant.variant_id, file=relative_file(config, path),
        source=variant.source, verdict=variant.verdict, created_at=variant.created_at,
        human_rating=variant.human_rating, note=variant.note, flags=list(variant.flags),
        extra=dict(variant.extra),
    )


def allocate_variant(config, record: dict, sticker_id: str) -> tuple[str, Path]:
    """次の候補ID とファイルパスを確保します（next_seq を1つ進めます）。

    欠番は再利用しません。生成に失敗しても番号は消費したままにします
    （APIが課金されている可能性があるため、同じ番号を使い回さないほうが安全です）。
    """
    seq = int(record.get("next_seq", len(record.get("variants", [])) + 1))
    used = {str(v.get("variant_id")) for v in record.get("variants", []) if isinstance(v, dict)}
    folder = variant_dir(config, sticker_id)
    # 記録に無くてもファイルがあれば飛ばします（中断後の再実行で上書きしないため）。
    # 書き込み途中の一時ファイル（.png.part）も同じです（課金済みの画像の可能性があるため）
    while (f"v{seq:03d}" in used or (folder / f"v{seq:03d}.png").exists()
           or (folder / f"v{seq:03d}.png.part").exists()):
        seq += 1
    variant_id = f"v{seq:03d}"
    record["next_seq"] = seq + 1
    return variant_id, folder / f"{variant_id}.png"


def register_variant(config, record: dict, variant_id: str, path: Path, *, meta: dict) -> dict:
    """生成できた候補をメタデータ付きで記録します（PNGが実在する前提で呼びます）。

    同じ番号が既に記録にあれば、2件目は加えません（repair が先に取り込んだ場合など）。
    その候補が同じファイルを指していれば、既存の項目をそのまま使い、無い情報だけを補います
    （間に付いた判断・評価は上書きしません）。別のファイルを指していれば、矛盾なので例外にします。
    """
    file = relative_file(config, path)
    existing = next((v for v in record.get("variants") or []
                     if isinstance(v, dict) and v.get("variant_id") == variant_id), None)
    if existing is not None:
        if existing.get("file") != file:
            raise VariantError(f"同じ番号の候補が別のファイルを指しています: {variant_id}"
                               f"（記録: {existing.get('file')} / 今回: {file}）")
        for key, value in meta.items():
            existing.setdefault(key, value)
        return existing
    item = {
        "variant_id": variant_id,
        "file": relative_file(config, path),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source": SOURCE_API,
        "verdict": VERDICT_PENDING,     # 生成しただけでは採用も不採用もしません
        "human_rating": None,
        "note": "",
    }
    item.update(meta)
    record.setdefault("variants", []).append(item)
    return item


def generation_meta(config, generator, prompt: str, sticker_id: str) -> dict:
    """候補の生成条件。既存の設定・プロバイダから取れる値だけを入れます。"""
    reference = generator.reference_image()
    return {
        "provider": config.provider_name,
        "model": config.model,
        "model_params": {
            "size": str(config.get("generation.size", "1024x1024")),
            "quality": config.quality,
            "background": str(config.get("generation.background", "transparent")),
        },
        "prompt_sha1": sha1_text(prompt),
        "prompt_file": _prompt_file(config, sticker_id),
        # 既存コードにプロンプトの版という概念が無いため null。追跡は prompt_sha1 で行います。
        "prompt_version": None,
        "master_hash": sha1_file(reference) if reference else None,
        "cost_usd": generator.estimate_cost_usd(1),
    }


def _prompt_file(config, sticker_id: str) -> str | None:
    """既存の保存機構（prompts/generated/<id>.txt）が使われていればそのパス。"""
    if not bool(config.get("generation.save_prompt", True)):
        return None
    path = config.dir_generated_prompts / f"{sticker_id}.txt"
    return relative_file(config, path) if path.exists() else None


def generate_variants(config, entries, count: int, generator, *, on_event=None,
                      on_reserved=None, on_registered=None, should_stop=None) -> list[dict]:
    """各スタンプについて count 枚の候補を生成します（generated/ には触れません）。

    Args:
        generator: ImageGenerator。呼び出し側が dry_run などを設定して渡します。
        on_event: (sticker_id, variant_id, status, detail) を受け取る関数（進捗表示用）。

    Returns:
        1件ごとの結果 [{"id","variant_id","status","path","detail"}]。
        status は generated | dry-run | error。
    """
    results: list[dict] = []
    for entry in entries:
        _generate_for_entry(config, entry, count, generator, results, on_event,
                            on_registered, should_stop, on_reserved)
    return results


def _generate_for_entry(config, entry, count: int, generator, results: list[dict],
                        on_event, on_registered=None, should_stop=None,
                        on_reserved=None) -> None:
    """1スタンプ分。番号の確保と記録は、そのつど最新の状態に対して行います。

    生成には時間がかかるため、その間ずっと古い状態を持たないようにしています
    （持ったまま保存すると、途中で入った評価・判断・採用が消えます）。
    """
    prompt = generator.prompt_for(entry)
    for _ in range(max(int(count), 1)):
        if should_stop is not None and should_stop():
            return                  # 中止: 候補1件ごとに確かめます（API の呼び出し中は止めません）
        if getattr(generator, "dry_run", False):
            results.append({"id": entry.id, "variant_id": None, "status": "dry-run",
                            "path": None, "detail": ""})
            if on_event:
                on_event(entry.id, None, "dry-run", "")
            continue

        def reserve(state):     # noqa: B023 - ループごとに即時実行します
            record = ensure_record(config, state, entry.id)
            variant_id, path = allocate_variant(config, record, entry.id)
            if on_reserved is not None:
                # 番号の確保と同じ保存で、呼び出し側の記録（再生成の予約など）も更新します
                on_reserved(state, record, variant_id)
            return variant_id, path

        variant_id, path = update_sticker(config, entry.id, reserve)    # 番号は先に確定・保存

        result = generator.generate_one(entry, output_path=path)
        if result.status == "generated" and path.exists():
            meta = generation_meta(config, generator, prompt, entry.id)

            def register(state):    # noqa: B023 - 直後に実行します
                record = ensure_record(config, state, entry.id)
                register_variant(config, record, variant_id, path, meta=meta)
                if on_registered is not None:
                    # 登録と同じ保存で、呼び出し側の記録（再生成の進み具合など）も更新します
                    on_registered(state, record, variant_id)

            # PNG が出来てから記録します（記録だけ残る状態を作らない）
            update_sticker(config, entry.id, register)
            results.append({"id": entry.id, "variant_id": variant_id, "status": "generated",
                            "path": path, "detail": ""})
        else:
            # 失敗しても番号は戻しません（課金済みの可能性があるため）
            results.append({"id": entry.id, "variant_id": variant_id, "status": "error",
                            "path": None, "detail": result.detail or "生成に失敗しました"})
        if on_event:
            on_event(entry.id, variant_id, results[-1]["status"], results[-1]["detail"])


# ---------------------------------------------------------------------------
# 再生成（Phase 6）: regen の印が付いたスタンプだけに候補を作る
# ---------------------------------------------------------------------------
REGEN_DEFAULT_COUNT = 4        # 1スタンプあたりの既定の枚数
REGEN_MAX_COUNT = 8            # 1スタンプあたりの上限
REGEN_MAX_TOTAL = 100          # 1回の実行全体の上限（意図しない大量課金を防ぐ）


def _regen_now() -> str:
    """再生成の管理に使う時刻。印の付け直しと実行開始を取り違えないよう、マイクロ秒まで持ちます。"""
    return datetime.now().isoformat(timespec="microseconds")


def _regen_requested(record: dict, done_at: str | None = None) -> bool:
    """regen の印の中に、まだ生成していない指示があるか。

    regen_generated_at は「その時刻までに付いた regen の指示には、候補を作り終えた」ことを表します
    （実行を始めた時刻）。それより後に付けた・付け直した印（regen_marked_at が新しい）は未処理です。
    日時の無い以前のデータは、regen_generated_at が無ければ未処理とみなします。
    """
    marks = [v for v in record.get("variants") or []
             if isinstance(v, dict) and v.get("verdict") == VERDICT_REGEN]
    if not marks:
        return False
    done_at = done_at or record.get("regen_generated_at")
    if not done_at:
        return True
    return any(isinstance(v.get("regen_marked_at"), str) and v["regen_marked_at"] > done_at
               for v in marks)


def _regen_owner() -> dict:
    return {"pid": os.getpid(), "host": _this_host(), "started": _process_start(os.getpid())[1],
            "token": uuid.uuid4().hex}


# このプロセスで終わった実行（再生成・初回生成）の token。締めの保存に失敗すると、記録には
# このプロセスの owner が残ります。プロセスは動き続けるので、pid だけで判断すると「実行中」の
# ままになり、再起動するまで続きから再開できません。ここにある token の owner は止まったとみなします。
# 実行ごとに別の token（uuid4）なので、同じプロセスで実行中の別の実行には影響しません。
_ENDED_RUN_TOKENS: set[str] = set()
_ENDED_RUN_LOCK = threading.Lock()


def _mark_run_ended(token: str) -> None:
    """実行が候補を作り終えた（成功・失敗・中止のどれでも）。締めの前に呼びます。"""
    with _ENDED_RUN_LOCK:
        _ENDED_RUN_TOKENS.add(token)


def _forget_ended_run(token: str) -> None:
    """締めを保存できた（owner は消えたか外れた）ので、もう覚えておく必要はありません。"""
    with _ENDED_RUN_LOCK:
        _ENDED_RUN_TOKENS.discard(token)


def _regen_owner_alive(owner) -> bool:
    """再生成を実行中の処理が、まだ動いているか（落ちていれば、途中で止まった実行として続きから）。"""
    if not isinstance(owner, dict) or not isinstance(owner.get("pid"), int):
        return False
    token = owner.get("token")
    if isinstance(token, str):
        with _ENDED_RUN_LOCK:
            if token in _ENDED_RUN_TOKENS:
                return False               # このプロセスで終わった実行（締めを保存できなかった）
    if owner.get("host") not in (None, _this_host()):
        return True                        # 別のPCの処理は確かめられないので、動いているとみなす
    alive, started = _process_start(owner["pid"])
    return alive and (owner.get("started") is None or started is None or owner["started"] == started)


def _regen_pending_run(record: dict) -> dict | None:
    """途中で止まった（失敗・中止・異常終了した）実行。実行中（持ち主が動いている）なら None。"""
    run = record.get("regen_run")
    if not isinstance(run, dict) or not isinstance(run.get("count"), int):
        return None
    if _regen_owner_alive(run.get("owner")):
        return None
    return run


def _regen_resumable(record: dict, run: dict) -> bool:
    """途中の実行を続けてよいか。その実行が対象にした regen の印が、いまも残っているか。

    実行を始めた時刻（since）以前に付いた印（日時の無い以前の印を含む）が1つでも残っていれば、
    その実行の続きです。印がすべて外された・付け直された場合は、古い実行の記録（regen_run）だけを
    根拠に作ることはしません（付け直した新しい印は、新しい実行の対象になります）。
    """
    since = run.get("since")
    for item in record.get("variants") or []:
        if not isinstance(item, dict) or item.get("verdict") != VERDICT_REGEN:
            continue
        marked = item.get("regen_marked_at")
        if not isinstance(marked, str) or not isinstance(since, str) or marked <= since:
            return True
    return False


def _regen_recover(config, sticker_id: str, record: dict, run: dict, *, apply: bool) -> int:
    """前回の実行で予約した番号のうち、画像はできているのに記録されていないものを数える・戻す。

    API が画像を返した直後（記録する前）に落ちた場合の、課金済みの画像です。作り直さずに登録します。
    ただし、この実行で予約した番号のもので、最後まで読める PNG のものだけです（壊れた・書きかけの
    ファイルは使いません）。一時ファイル（.png.part）のままなら、正式な名前に置き換えてから登録します。
    apply=False なら数えるだけで、何も書きません。
    """
    return _recover_reserved(config, sticker_id, record, run, apply=apply, meta={"regen_recovered": True})


def _recover_reserved(config, sticker_id: str, record: dict, run: dict, *, apply: bool, meta: dict) -> int:
    """実行の記録（reserved / done）から、課金済みで未登録の画像を数える・登録する（再生成・初回生成で共通）。

    run の reserved のうち done に無い番号で、最後まで読める vNNN.png か vNNN.png.part があるものが対象です。
    apply=True なら .part を正式な名前に置き換え、未登録なら meta を付けて登録し、done に加えます。
    """
    done = run.get("done") or []
    known = {v.get("variant_id") for v in record.get("variants") or [] if isinstance(v, dict)}
    recovered = 0
    for variant_id in run.get("reserved") or []:
        if variant_id in done:
            continue
        path = variant_dir(config, sticker_id) / f"{variant_id}.png"
        part = path.with_suffix(".png.part")
        source = path if path.exists() else part if part.exists() else None
        if source is None or not _is_complete_png(source):
            continue
        recovered += 1
        if not apply:
            continue
        if source == part:
            _replace_with_retry(part, path)
        if variant_id not in known:
            register_variant(config, record, variant_id, path, meta=dict(meta))
        run.setdefault("done", []).append(variant_id)
    return recovered


def _regen_decide(config, sticker_id: str, record: dict, count: int, *, apply: bool) -> dict | None:
    """1スタンプの再生成を、続きから・新しく・しない、のどれにするかを決めます。

    apply=True なら、決めた結果を record に書きます（記録の鍵の中で呼びます）。
    apply=False なら何も書きません（dry-run・計画用）。

    Returns:
        {"busy": True} … 別の処理が実行中
        {"resume": True, "run": 実行, "remaining": n} … 途中の実行の続き
        {"new": True, "remaining": count} … 新しい実行
        None … 作るものは無い
    """
    run = record.get("regen_run")
    if isinstance(run, dict) and _regen_owner_alive(run.get("owner")):
        return {"busy": True}
    done_at = record.get("regen_generated_at")
    pending = _regen_pending_run(record)
    if pending is not None:
        work = pending if apply else copy.deepcopy(pending)
        recovered = _regen_recover(config, sticker_id, record, work, apply=apply)
        made = len(work.get("done") or []) + (0 if apply else recovered)
        if made >= work["count"]:
            # 全部できている（締めの前に止まった）: 作り直さずに完了として締めます
            done_at = work.get("since") or done_at
            if apply:
                record["regen_generated_at"] = done_at
                record.pop("regen_run", None)
        elif _regen_resumable(record, work):
            return {"resume": True, "run": work, "remaining": work["count"] - made}
        elif apply:
            record.pop("regen_run", None)       # 印を取り消された古い実行は、続けずに片付けます
    if _regen_requested(record, done_at):
        return {"new": True, "remaining": int(count)}
    return None


def regen_plan(config, sticker_ids, count: int) -> dict:
    """再生成の対象と枚数を計算します（読み取りだけ。何も書かず、プロバイダも作りません）。

    Returns:
        {"targets": [{"id", "count": 作る枚数, "resume": 途中からか}], "total": 合計枚数,
         "busy": [別の処理が実行中のスタンプ]}
    """
    state = load(config)
    stickers = state.get("stickers") or {}
    targets, busy = [], []
    for sid in sticker_ids:
        record = stickers.get(sid)
        if not isinstance(record, dict):
            continue
        decision = _regen_decide(config, sid, record, count, apply=False)
        if decision is None:
            continue
        if decision.get("busy"):
            busy.append(sid)
        elif decision["remaining"] > 0:
            if generation_running_elsewhere(config, sid):     # 別の処理（GUI / CLI）がこのスタンプを生成中
                busy.append(sid)
                continue
            targets.append({"id": sid, "count": decision["remaining"],
                            "resume": bool(decision.get("resume"))})
    return {"targets": targets, "total": sum(t["count"] for t in targets), "busy": busy}


def begin_regen(config, sticker_id: str, count: int) -> dict | None:
    """1スタンプの再生成を始めます（記録の鍵の中で、対象かどうかを確かめ直してから）。

    途中で止まった実行は、その対象だった regen の印が残っていれば続きから（残りだけ）。
    前回の記録前に落ちた課金済みの画像は、作り直さずに登録します。
    実行中の目印（owner）を書くので、同じスタンプを2つの処理が同時に生成することはありません。

    Returns:
        {"token", "since": 基準の時刻, "remaining": 作る枚数}。対象でない・実行中なら None。
    """
    def mutate(state):
        record = (state.get("stickers") or {}).get(sticker_id)
        if not isinstance(record, dict):
            return UNCHANGED
        before = copy.deepcopy(record)
        decision = _regen_decide(config, sticker_id, record, count, apply=True)
        if decision is None or decision.get("busy") or decision["remaining"] <= 0:
            # 締め・片付け・復元で記録が変わっていれば保存します（作るものは無い）
            return {"claim": None} if record != before else UNCHANGED
        if decision.get("resume"):
            run = decision["run"]
        else:
            run = {"since": _regen_now(), "count": int(count), "done": [], "reserved": []}
        run["owner"] = _regen_owner()
        record["regen_run"] = run
        return {"claim": {"token": run["owner"]["token"], "since": run["since"],
                          "remaining": decision["remaining"]}}

    result = update_sticker(config, sticker_id, mutate)
    return None if result is UNCHANGED else result["claim"]


def _regen_run_of(state: dict, sticker_id: str, token: str) -> tuple[dict, dict] | tuple[None, None]:
    record = (state.get("stickers") or {}).get(sticker_id)
    run = record.get("regen_run") if isinstance(record, dict) else None
    owner = run.get("owner") if isinstance(run, dict) else None
    if not isinstance(owner, dict) or owner.get("token") != token:
        return None, None
    return record, run


def regen_reserve_hook(sticker_id: str, token: str):
    """generate_variants の on_reserved に渡す関数。

    番号を確保するのと同じ保存で「この実行で予約した番号」を記録します。API が画像を返した
    直後・記録する前に落ちても、次回その番号の画像を見つけて、作り直さずに登録できるように。
    """
    def hook(state, record_for_sticker, variant_id):
        _record, run = _regen_run_of(state, sticker_id, token)
        if run is not None:
            run.setdefault("reserved", []).append(variant_id)
    return hook


def regen_register_hook(sticker_id: str, token: str):
    """generate_variants の on_registered に渡す関数。

    候補の登録と「この実行で作れた」記録を、同じ鍵の中の同じ保存で行います
    （別々に保存すると、その間で落ちたときに、作れた候補をもう一度作って課金してしまうため）。
    """
    def hook(state, record_for_sticker, variant_id):
        _record, run = _regen_run_of(state, sticker_id, token)
        done = run.setdefault("done", []) if run is not None else None
        if done is not None and variant_id not in done:     # 同じ番号を二度数えない
            done.append(variant_id)
    return hook


def run_regen(config, entry, count: int, generator, *, on_start=None, on_event=None,
              should_stop=None) -> dict:
    """1スタンプの再生成を最後まで行います（GUI のジョブが1スタンプずつ呼びます）。

    生成の実行権を取る → 始める → 候補を作る（番号の予約と登録は、それぞれ記録の鍵の中の1回の保存）
    → 締める → 実行権を返す。API を呼んでいる間は記録の鍵を持ちません（実行権だけを持ちます）。
    同じスタンプを別の処理（GUI / CLI）が生成中なら、始めません。

    Returns:
        {"claimed": 始めたか, "remaining": 作ろうとした枚数, "complete": 全部作れたか}
    """
    try:
        run_lock = _claim_generation(config, entry.id)
    except GenerationBusyError:
        return {"claimed": False, "remaining": 0, "complete": False}
    with run_lock:
        return _run_regen_locked(config, entry, count, generator, on_start=on_start, on_event=on_event,
                                 should_stop=should_stop)


def _run_regen_locked(config, entry, count: int, generator, *, on_start, on_event, should_stop) -> dict:
    claim = begin_regen(config, entry.id, count)
    if claim is None:
        return {"claimed": False, "remaining": 0, "complete": False}
    if on_start:
        on_start(claim["remaining"])
    try:
        generate_variants(
            config, [entry], claim["remaining"], generator, on_event=on_event,
            on_reserved=regen_reserve_hook(entry.id, claim["token"]),
            on_registered=regen_register_hook(entry.id, claim["token"]),
            should_stop=should_stop)
    finally:
        # 締めの保存に失敗しても、この実行を「実行中」のまま残さないため、先に終わったことを覚えます
        _mark_run_ended(claim["token"])
        complete = finish_regen(config, entry.id, claim["token"])
        _forget_ended_run(claim["token"])
    return {"claimed": True, "remaining": claim["remaining"], "complete": complete}


def finish_regen(config, sticker_id: str, token: str) -> bool:
    """1スタンプの再生成を終えます。全部作れたときだけ regen_generated_at を記録します。

    途中で失敗・中止した場合は、作れた分（done）を残して実行中の目印だけ外します
    （次回は残りの枚数だけを作ります）。regen の判断（verdict）そのものは変えません。

    Returns:
        全部作れたなら True。
    """
    def mutate(state):
        record, run = _regen_run_of(state, sticker_id, token)
        if run is None:
            return UNCHANGED
        if len(run.get("done") or []) >= run["count"]:
            record["regen_generated_at"] = run["since"]        # 開始時刻（それ以降の印は次回の対象）
            record.pop("regen_run", None)
            return True
        run["owner"] = None
        return False

    result = update_sticker(config, sticker_id, mutate)
    return result is True


# ---------------------------------------------------------------------------
# 初回候補生成（Phase 7）: 候補が1件も無いスタンプに、最初の候補を作る
# ---------------------------------------------------------------------------
# 実行の記録は initial_run（regen_run とは別です。regen の印・regen_generated_at とは関係しません）。
# 枚数の制限は再生成と同じにします。
INITIAL_DEFAULT_COUNT = REGEN_DEFAULT_COUNT
INITIAL_MAX_COUNT = REGEN_MAX_COUNT
INITIAL_MAX_TOTAL = REGEN_MAX_TOTAL

INITIAL_SKIP_CANDIDATES = "has_candidates"   # 候補が1件以上ある
INITIAL_SKIP_ORIGINAL = "has_original"       # 原画（generated/<id>.png）だけがある（仮想の v001 と混ぜない）
INITIAL_SKIP_RUNNING = "running"             # 別の処理が、このスタンプの初回生成を実行中


def initial_status(record) -> str | None:
    """一覧の表示用。"running"（実行中）/ "pending"（途中で止まった実行がある）/ None。"""
    run = record.get("initial_run") if isinstance(record, dict) else None
    if not isinstance(run, dict):
        return None
    return "running" if _regen_owner_alive(run.get("owner")) else "pending"


def _initial_decide(config, sticker_id: str, record, count: int, *, apply: bool) -> dict:
    """1スタンプの初回生成を、続きから・新しく・しない、のどれにするかを決めます。

    途中で止まった実行（initial_run の持ち主が動いていない）は、候補が既にあっても続きからです。
    前回の記録前に落ちた課金済みの画像は、作り直さずに登録します（apply=False なら数えるだけ）。

    Returns:
        {"skip": 理由} … 対象外
        {"resume": True, "run": 実行, "remaining": n, "recovered": n} … 続き（n=0 なら締めるだけ）
        {"new": True, "remaining": count, "recovered": 0} … 新しい実行
    """
    run = record.get("initial_run") if isinstance(record, dict) else None
    if isinstance(run, dict):
        if _regen_owner_alive(run.get("owner")):
            return {"skip": INITIAL_SKIP_RUNNING}
        work = run if apply else copy.deepcopy(run)
        recovered = _recover_reserved(config, sticker_id, record, work, apply=apply,
                                      meta={"initial_recovered": True})
        made = len(work.get("done") or []) + (0 if apply else recovered)
        planned = work.get("count")
        if not isinstance(planned, int) or isinstance(planned, bool):
            planned = made                      # 枚数の分からない記録は、作れた分で締めます
        return {"resume": True, "run": work, "remaining": max(planned - made, 0), "recovered": recovered}
    if isinstance(record, dict) and record.get("variants"):
        return {"skip": INITIAL_SKIP_CANDIDATES}
    if (config.dir_generated / f"{sticker_id}.png").exists():
        return {"skip": INITIAL_SKIP_ORIGINAL}
    return {"new": True, "remaining": int(count), "recovered": 0}


def initial_plan(config, sticker_ids, count: int) -> dict:
    """初回生成の対象と枚数を計算します（読み取りだけ。何も書かず、プロバイダも作りません）。

    Returns:
        {"targets": [{"id", "count": 作る枚数, "resume": 続きか, "recovered": 作らずに登録する枚数}],
         "skipped": [{"id", "reason"}], "busy": [実行中のスタンプ], "total": 合計枚数}
    """
    stickers = load(config).get("stickers") or {}
    targets, skipped, busy = [], [], []
    for sid in sticker_ids:
        decision = _initial_decide(config, sid, stickers.get(sid), count, apply=False)
        if "skip" not in decision and generation_running_elsewhere(config, sid):
            decision = {"skip": INITIAL_SKIP_RUNNING}      # 別の処理（GUI / CLI）がこのスタンプを生成中
        if "skip" in decision:
            skipped.append({"id": sid, "reason": decision["skip"]})
            if decision["skip"] == INITIAL_SKIP_RUNNING:
                busy.append(sid)
            continue
        targets.append({"id": sid, "count": decision["remaining"], "resume": bool(decision.get("resume")),
                        "recovered": decision["recovered"]})
    return {"targets": targets, "skipped": skipped, "busy": busy,
            "total": sum(t["count"] for t in targets)}


def begin_initial(config, sticker_id: str, count: int) -> dict | None:
    """1スタンプの初回生成を始めます（記録の鍵の中で、対象かどうかを確かめ直してから）。

    実行中の目印（owner）を書くので、同じスタンプを2つの処理が同時に初回生成することはありません。

    Returns:
        {"claim": {"token", "since", "remaining"} or None（作らずに締めた）, "recovered": 登録した枚数}。
        対象でない・実行中なら None。
    """
    def mutate(state):
        stickers = state.setdefault("stickers", {})
        record = stickers.get(sticker_id)
        decision = _initial_decide(config, sticker_id, record, count, apply=True)
        if "skip" in decision:
            return UNCHANGED
        if decision.get("resume"):
            run = decision["run"]
            if decision["remaining"] <= 0:
                # 全部できている（締めの前に止まった・残りを復元できた）: 作らずに完了として締めます
                record.pop("initial_run", None)
                return {"claim": None, "recovered": decision["recovered"]}
        else:
            record = ensure_record(config, state, sticker_id)
            run = {"since": _regen_now(), "count": int(count), "done": [], "reserved": []}
            record["initial_run"] = run
        run["owner"] = _regen_owner()
        return {"claim": {"token": run["owner"]["token"], "since": run["since"],
                          "remaining": decision["remaining"]},
                "recovered": decision["recovered"]}

    result = update_sticker(config, sticker_id, mutate)
    return None if result is UNCHANGED else result


def _initial_run_of(state: dict, sticker_id: str, token: str) -> tuple[dict, dict] | tuple[None, None]:
    record = (state.get("stickers") or {}).get(sticker_id)
    run = record.get("initial_run") if isinstance(record, dict) else None
    owner = run.get("owner") if isinstance(run, dict) else None
    if not isinstance(owner, dict) or owner.get("token") != token:
        return None, None
    return record, run


def initial_reserve_hook(sticker_id: str, token: str):
    """generate_variants の on_reserved: 番号の確保と同じ保存で initial_run.reserved に記録します。"""
    def hook(state, record_for_sticker, variant_id):
        _record, run = _initial_run_of(state, sticker_id, token)
        if run is not None:
            run.setdefault("reserved", []).append(variant_id)
    return hook


def initial_register_hook(sticker_id: str, token: str):
    """generate_variants の on_registered: 候補の登録と同じ保存で initial_run.done に記録します。"""
    def hook(state, record_for_sticker, variant_id):
        _record, run = _initial_run_of(state, sticker_id, token)
        done = run.setdefault("done", []) if run is not None else None
        if done is not None and variant_id not in done:     # 同じ番号を二度数えない
            done.append(variant_id)
    return hook


def run_initial(config, entry, count: int, generator, *, on_start=None, on_event=None,
                should_stop=None) -> dict:
    """1スタンプの初回生成を最後まで行います（GUI のジョブが1スタンプずつ呼びます）。

    生成の実行権を取る → 始める → 候補を作る（番号の予約と登録は、それぞれ記録の鍵の中の1回の保存）
    → 締める → 実行権を返す。API を呼んでいる間は記録の鍵を持ちません（実行権だけを持ちます）。
    同じスタンプを別の処理（GUI / CLI）が生成中なら、始めません。

    Returns:
        {"claimed": 候補を作り始めたか, "closed": 作らずに締めたか, "recovered": 作らずに登録した枚数,
         "remaining": 作ろうとした枚数, "complete": 全部そろったか}
    """
    try:
        run_lock = _claim_generation(config, entry.id)
    except GenerationBusyError:
        return {"claimed": False, "closed": False, "recovered": 0, "remaining": 0, "complete": False}
    with run_lock:
        return _run_initial_locked(config, entry, count, generator, on_start=on_start, on_event=on_event,
                                   should_stop=should_stop)


def _run_initial_locked(config, entry, count: int, generator, *, on_start, on_event, should_stop) -> dict:
    begun = begin_initial(config, entry.id, count)
    if begun is None:
        return {"claimed": False, "closed": False, "recovered": 0, "remaining": 0, "complete": False}
    claim = begun["claim"]
    if claim is None:
        return {"claimed": False, "closed": True, "recovered": begun["recovered"], "remaining": 0,
                "complete": True}
    if on_start:
        on_start(claim["remaining"], begun["recovered"])
    try:
        generate_variants(
            config, [entry], claim["remaining"], generator, on_event=on_event,
            on_reserved=initial_reserve_hook(entry.id, claim["token"]),
            on_registered=initial_register_hook(entry.id, claim["token"]),
            should_stop=should_stop)
    finally:
        # 締めの保存に失敗しても、この実行を「実行中」のまま残さないため、先に終わったことを覚えます
        _mark_run_ended(claim["token"])
        complete = finish_initial(config, entry.id, claim["token"])
        _forget_ended_run(claim["token"])
    return {"claimed": True, "closed": False, "recovered": begun["recovered"],
            "remaining": claim["remaining"], "complete": complete}


def finish_initial(config, sticker_id: str, token: str) -> bool:
    """1スタンプの初回生成を終えます。全部作れたら initial_run を消します（完了）。

    途中で失敗・中止した場合は、作れた分（done）と予約（reserved）を残して実行中の目印だけ外します
    （次回は残りの枚数だけを作ります）。

    Returns:
        全部作れたなら True。
    """
    def mutate(state):
        record, run = _initial_run_of(state, sticker_id, token)
        if run is None:
            return UNCHANGED
        if len(run.get("done") or []) >= run["count"]:
            record.pop("initial_run", None)
            return True
        run["owner"] = None
        return False

    result = update_sticker(config, sticker_id, mutate)
    return result is True


# ---------------------------------------------------------------------------
# 採用（Phase 1c）
# ---------------------------------------------------------------------------
class VariantError(Exception):
    """候補の操作ができない場合に送出されます（状態ファイルは更新されません）。"""


class StateCorruptError(VariantError):
    """variants.json が壊れていて、上書きすると記録を失う場合に送出されます。"""


class LockBusyError(VariantError):
    """鍵を他の処理が持っている（待たずに取ろうとして取れなかった場合も含む）。"""


class StickerLockNeeded(LockBusyError):
    """記録の無いスタンプの原画をコピーするのに、スタンプの鍵が必要（update_sticker がやり直す）。"""


class GenerationBusyError(LockBusyError):
    """同じスタンプを、別の処理（GUI または CLI）が生成中（生成の実行権を取れない）。"""


class StateBusyError(VariantError):
    """variants.json を一時的に読み書きできない場合（他の処理が使用中など）。壊れてはいません。"""


class StateReadError(StateBusyError):
    """variants.json を一時的に読めない場合に送出されます（壊れているとは限りません）。"""


class StateWriteError(StateBusyError):
    """variants.json を一時的に保存できない場合に送出されます（元のファイルはそのまま）。"""


class AdoptError(VariantError):
    """候補を採用できない場合に送出されます（状態ファイルは更新されません）。"""


def _find_item(config, data: dict, sticker_id: str, variant_id: str) -> tuple[dict, dict]:
    """(記録, 候補) を返します。見つからなければ保存せずに例外にします。"""
    sticker = get_sticker(config, sticker_id, data)
    if sticker is None or sticker.find(variant_id) is None:
        have = ", ".join(v.variant_id for v in sticker.variants) if sticker else "なし"
        raise VariantError(f"候補が見つかりません: {sticker_id}/{variant_id}（ある候補: {have}）")
    record = ensure_record(config, data, sticker_id)
    item = next((v for v in record.get("variants", [])
                 if isinstance(v, dict) and v.get("variant_id") == variant_id), None)
    if item is None:  # pragma: no cover - get_sticker と ensure_record は同じ元を見ています
        raise VariantError(f"候補が見つかりません: {sticker_id}/{variant_id}")
    return record, item


def set_verdict(config, sticker_id: str, variant_id: str, verdict: str) -> dict:
    """人の判断（pending / rejected / regen）を記録します。

    いま採用中かどうか（sticker.adopted）は変更しません。採用の切り替えは adopt() です。
    """
    if verdict not in SETTABLE_VERDICTS:
        raise VariantError(
            f"指定できない判断です: {verdict}"
            f"（指定できるのは {', '.join(SETTABLE_VERDICTS)}。"
            "採用は variants adopt を使ってください）"
        )
    def mutate(state):
        _record, item = _find_item(config, state, sticker_id, variant_id)
        item["verdict"] = verdict
        if verdict == VERDICT_REGEN:
            # 付けた・付け直した時刻。再生成が済んだかどうかの判定に使います（Phase 6）
            item["regen_marked_at"] = _regen_now()
        return item

    return update_sticker(config, sticker_id, mutate)


def set_rating(config, sticker_id: str, variant_id: str, rating: int | None) -> dict:
    """人の5段階評価を記録します（None で消します）。

    スコアリングには使いません。将来の重み決めのために残すだけです。
    """
    if rating is not None:
        if not isinstance(rating, int) or isinstance(rating, bool):
            raise VariantError(f"評価は整数で指定してください: {rating}")
        if not RATING_MIN <= rating <= RATING_MAX:
            raise VariantError(f"評価は{RATING_MIN}〜{RATING_MAX}で指定してください: {rating}")
    def mutate(state):
        _record, item = _find_item(config, state, sticker_id, variant_id)
        item["human_rating"] = rating
        return item

    return update_sticker(config, sticker_id, mutate)


# ---------------------------------------------------------------------------
# 採用の一時退避と巻き戻し
# ---------------------------------------------------------------------------
def adopt_backup_root(config) -> Path:
    """採用中だけ使う一時退避の置き場（成功すれば消します。巻き戻しに失敗したときだけ残ります）。"""
    return config.root / "output" / "archive" / "adopt-backup"


def _copy_verified(src: Path, dest: Path, expected_sha: str | None = None,
                   *, keep_times: bool = False) -> None:
    """src を dest へ「一時ファイルにコピー → fsync → SHA1確認 → 置き換え」で書きます。

    途中で失敗しても dest は元のまま（半端なファイルを dest に作らない）。
    keep_times=True なら更新日時も写します（元の画像に戻すとき用）。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copyfile(src, tmp)
        if keep_times:
            shutil.copystat(src, tmp)
        with open(tmp, "rb+") as f:
            os.fsync(f.fileno())
        want = expected_sha or sha1_file(src)
        if sha1_file(tmp) != want:
            raise OSError(f"コピーした画像の中身が一致しません: {src} → {dest}")
        _replace_with_retry(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _restore_file(saved: Path | None, dest: Path) -> None:
    """採用前の状態へ戻します（saved=None なら「元々無かった」ので消します）。"""
    if saved is None:
        _retry_io(lambda: dest.unlink(missing_ok=True))
    else:
        _copy_verified(saved, dest, keep_times=True)


def _sha_or_none(path: Path) -> str | None:
    return sha1_file(path) if path.exists() else None


@dataclass
class _AdoptionBackup:
    """採用を始める前の generated / final を、中身ごと控えておきます。

    「退避（archive）に成功したか」ではなく「採用前に原画があったか」で戻し方を決めるため、
    採用処理の最初に作ります。
    """

    folder: Path
    generated: Path | None          # 控えのパス（採用前に無ければ None）
    generated_sha: str | None
    final: Path | None
    final_sha: str | None

    @classmethod
    def capture(cls, config, sticker_id: str, dest: Path, final_path: Path) -> "_AdoptionBackup":
        folder = adopt_backup_root(config) / (
            f"{sticker_id}-{datetime.now():%Y%m%d_%H%M%S}-{uuid.uuid4().hex[:8]}")
        saved = {}
        try:
            for name, path in (("generated", dest), ("final", final_path)):
                if path.exists():
                    copy = folder / f"{name}.png"
                    _copy_verified(path, copy, keep_times=True)
                    saved[name] = (copy, sha1_file(copy))
                else:
                    saved[name] = (None, None)
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)   # まだ何も触っていないので控えも不要
            raise
        return cls(folder, *saved["generated"], *saved["final"])

    def restore(self, dest: Path, final_path: Path, archived) -> list[str]:
        """採用前の状態へ戻し、戻せなかったものを返します（空なら完全に戻せた）。"""
        problems: list[str] = []
        archived = Path(archived) if archived else None
        # 退避で動かした原画があれば、それ自体を元の場所へ戻します（退避に余計なものを残さない）
        if archived is not None and archived.exists() and self.generated is not None:
            try:
                if sha1_file(archived) == self.generated_sha:
                    _replace_with_retry(archived, dest)
            except OSError as exc:
                problems.append(f"退避した原画を戻せません: {archived} ({exc})")
        for label, saved, want, path in (("原画", self.generated, self.generated_sha, dest),
                                         ("完成画像", self.final, self.final_sha, final_path)):
            try:
                if _sha_or_none(path) != want:
                    _restore_file(saved, path)
                if _sha_or_none(path) != want:
                    raise OSError("戻したあとの中身が採用前と一致しません")
            except Exception as exc:  # noqa: BLE001 - 戻せなかった理由をすべて集めます
                problems.append(f"{label}を採用前に戻せません: {path} ({type(exc).__name__}: {exc})")
        if (archived is not None and archived.exists() and not problems
                and sha1_file(archived) == self.generated_sha):
            archived.unlink(missing_ok=True)    # 原画は控えから戻せたので、この退避は不要
        return problems

    def discard(self) -> None:
        shutil.rmtree(self.folder, ignore_errors=True)


def _record_rollback_failure(config, sticker_id: str, detail: str, backup: _AdoptionBackup) -> None:
    """巻き戻しの失敗を記録に残します（画面と CLI で気付けるように）。記録できなくても続けます。"""
    def mutate(state):
        record = ensure_record(config, state, sticker_id)
        record["rollback_failed"] = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "detail": detail,
            "backup": relative_file(config, backup.folder),
        }
    try:
        update(config, mutate)
    except Exception as exc:  # noqa: BLE001 - 元の失敗を隠さない
        print(f"ERROR: 巻き戻しの失敗を記録できませんでした: {exc}", file=sys.stderr)


def adopt(config, entry, variant_id: str, *, style=None) -> dict:
    """候補を「採用中の原画」にして、既存の合成・検証をそのまま通します。

    順序:
      候補とPNGを確認 → 採用前の generated / final を控える → 既存原画を退避
      → 候補を generated/<id>.png に置く（一時ファイル→確認→置き換え）
      → 既存の pipeline.render_final → 既存の validator → 最新の記録に採用状態を書く

    途中で失敗したら（Ctrl+C を含む）、generated / final / 退避 を採用前へ戻してから
    例外を送出します。variants.json は最後にしか書かないので、採用状態も前のままです。
    候補ファイル（variants/ 側）は移動も削除もしません。
    """
    if is_corrupt(config):          # 壊れた記録を上書きしないよう、何より先に止めます
        raise StateCorruptError(corrupt_message(config))

    with adopt_lock(config, entry.id):      # 同じスタンプの採用・取り込みを直列化します
        return _adopt_locked(config, entry, variant_id, style=style)


def _adopt_locked(config, entry, variant_id: str, *, style=None) -> dict:
    from . import image_processor as ip
    from . import pipeline
    from . import validator as vd
    from .importer import archive_existing
    from .text_renderer import TextStyle

    sticker = get_sticker(config, entry.id)
    if sticker is None:
        raise AdoptError(f"候補がありません: {entry.id}")
    variant = sticker.find(variant_id)
    if variant is None:
        have = ", ".join(v.variant_id for v in sticker.variants) or "なし"
        raise AdoptError(f"候補が見つかりません: {entry.id}/{variant_id}（ある候補: {have}）")

    src = variant.path(config)
    if not src.exists():
        raise AdoptError(f"候補の画像がありません: {src}")
    try:
        ip.load_rgba(src)          # 既存の読み込み処理で、壊れたPNGをここで弾きます
    except Exception as exc:       # noqa: BLE001 - 破損画像の例外は多岐にわたる
        raise AdoptError(f"候補の画像を読めません: {src} ({type(exc).__name__}: {exc})") from exc
    # 指紋は「採用を始めた時点の候補」から取ります（後から generated を読み直すと、
    # その間に別の処理が書いた画像を、採用した画像として記録してしまうため）
    src_sha = sha1_file(src)

    dest = config.dir_generated / f"{entry.id}.png"
    final_path = config.dir_final / f"{entry.id}.png"
    style = style or TextStyle.from_config(config)
    # legacy の候補（generated/<id>.png 自身）を採用する場合は、退避もコピーも不要です。
    same_file = src.resolve() == dest.resolve()

    backup = _AdoptionBackup.capture(config, entry.id, dest, final_path)
    archived = None
    try:
        if not same_file:
            archived = archive_existing(config, entry.id)      # 既存の退避処理を再利用
            _copy_verified(src, dest, src_sha)
        stat = dest.stat()
        stamp = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha1": src_sha}

        # ここから先は既存の正式な経路をそのまま使います（final を直接コピーしない）
        final_path, size_bytes, warnings = pipeline.render_final(config, entry, style)
        report = vd.validate_sticker(final_path, config)
        if not report.ok:
            raise AdoptError(
                "採用した画像がLINE仕様を満たしませんでした（採用状態は変更していません）:\n  "
                + "\n  ".join(i.message for i in report.errors)
            )

        def apply(state_now):
            record = ensure_record(config, state_now, entry.id)
            if not any(isinstance(v, dict) and v.get("variant_id") == variant_id
                       for v in record.get("variants", [])):
                raise AdoptError(f"候補が見つかりません: {entry.id}/{variant_id}")
            for item in record.get("variants", []):
                if isinstance(item, dict) and item.get("variant_id") == variant_id:
                    item["verdict"] = VERDICT_ADOPTED       # 採用した候補だけ変えます
            record["adopted"] = variant_id                  # ほかの候補の verdict は触りません
            record["adopted_at"] = datetime.now().isoformat(timespec="seconds")
            record["adopted_file"] = stamp                  # 別処理での差し替えを検出するため
            record.pop("rollback_failed", None)             # 採用し直せたので解消

        # 画像の処理が終わってから、最新の記録を読み直して採用状態だけを更新します
        update(config, apply)
    except BaseException as exc:
        # Ctrl+C も含め、触ったファイルを採用前へ戻してから、元の例外をそのまま送出します
        problems = backup.restore(dest, final_path, archived)
        if problems:
            detail = "採用の巻き戻しに失敗しました: " + " / ".join(problems)
            print(f"ERROR: {detail}\n  採用前の画像の控え: {backup.folder}", file=sys.stderr)
            exc.add_note(f"{detail}（採用前の画像の控え: {backup.folder}）")
            _record_rollback_failure(config, entry.id, detail, backup)
        else:
            backup.discard()
        raise
    backup.discard()

    return {
        "sticker_id": entry.id,
        "variant_id": variant_id,
        "generated": dest,
        "final": final_path,
        "size_bytes": size_bytes,
        "archived": archived,
        "warnings": list(warnings) + [i.message for i in report.warnings],
        "validation_ok": report.ok,
    }


def list_all(config, sticker_ids, data: dict | None = None) -> dict[str, StickerVariants]:
    """複数スタンプ分をまとめて取得します（variants.json の読み込みは1回だけ）。"""
    state = load(config) if data is None else data
    out: dict[str, StickerVariants] = {}
    for sid in sticker_ids:
        sticker = get_sticker(config, sid, state)
        if sticker is not None:
            out[sid] = sticker
    return out


# ---------------------------------------------------------------------------
# 修復（python -m src.variants repair）
# ---------------------------------------------------------------------------
_VARIANT_FILE = "v[0-9][0-9][0-9].png"


REPAIR_OK = "ok"                    # 記録は正常
REPAIR_CORRUPT = "corrupt"          # 壊れていたので退避して作り直した
REPAIR_MISSING = "missing"          # 記録ファイルが無い
REPAIR_TEMP_ONLY = "temp_only"      # 記録ファイルは無く、書き込み途中の一時ファイルだけが残っている


def _leftover_state_temps(config) -> list[str]:
    path = config.variants_path
    names = sorted(p.name for p in path.parent.glob(f".{path.name}.*.tmp"))
    if path.with_suffix(".json.tmp").exists():             # 以前の版の一時ファイル名
        names.append(path.with_suffix(".json.tmp").name)
    return names


def repair_state(config) -> dict:
    """候補の記録を修復します。表示と実際の変更が一致するよう、結果を分類して返します。

    - 正常: 記録に無い候補画像があれば追加します。何も無ければ **保存しません**。
    - 壊れている: 消さずに variants.json.corrupt-<日時>-<一意ID> へ退避し、候補フォルダの
      画像から作り直します（generated/<id>.png と中身が同じ候補を「採用中」にします）。
    - 記録ファイルが無い: 候補フォルダに画像があれば作り直し、無ければ何も作りません。
    - 一時ファイルだけがある: 中身が最新かどうか分からないので、自動では使いません（変更なし）。
    画像ファイルは消しも上書きもしません。

    Returns:
        {"kind": 上の分類, "changed": 保存したか, "quarantined": 退避先 or None,
         "stickers": {id: {"added": [...], "adopted": id|None, "rebuilt": bool}},
         "unreadable": [読めなかった画像], "temp_files": [一時ファイル名]}
    """
    report: dict = {"kind": None, "changed": False, "quarantined": None, "stickers": {},
                    "unreadable": [], "temp_files": []}
    root = config.dir_variants
    sticker_ids = sorted(p.name for p in root.iterdir() if p.is_dir()) if root.exists() else []
    with ExitStack() as locks:
        # 鍵の順序は「スタンプの鍵（ID順）→ 記録の鍵」。採用・取り込みと同じ順序です
        for sid in sticker_ids:
            locks.enter_context(adopt_lock(config, sid))
        locks.enter_context(state_lock(config))

        status = state_status(config)
        if status == STATE_MISSING and _leftover_state_temps(config):
            report["kind"] = REPAIR_TEMP_ONLY
            report["temp_files"] = _leftover_state_temps(config)
            return report
        if status == STATE_CORRUPT:
            report["kind"] = REPAIR_CORRUPT
            report["quarantined"] = str(quarantine_corrupt_state(config))
        else:
            report["kind"] = REPAIR_OK if status == STATE_OK else REPAIR_MISSING

        def mutate(state):
            stickers = state.setdefault("stickers", {})
            for sid in sticker_ids:
                record = stickers.get(sid)
                # 動いている実行（初回生成・再生成）が予約中の番号は、その実行が登録します。
                # 画像ができてから登録するまでの間に取り込むと、同じ番号が二重に登録されるため加えません
                active = _live_run_reservations(record)
                files = []
                for path in sorted((root / sid).glob(_VARIANT_FILE)):
                    if path.stem in active:
                        continue
                    if _is_readable_png(path):
                        files.append(path)
                    else:
                        report["unreadable"].append(relative_file(config, path))
                if isinstance(record, dict) and record.get("variants"):
                    added = _add_missing_variants(config, record, files)
                    if added:
                        report["stickers"][sid] = {"added": added, "adopted": record.get("adopted"),
                                                   "rebuilt": False}
                    continue
                if not files:
                    continue
                rebuilt = _rebuild_record(config, sid, files, record if isinstance(record, dict) else None)
                # 候補0件の既存の記録（途中で止まった初回生成など）は、置き換えずに候補と採用の
                # 項目だけを作り直します（実行の記録・予約・知らない項目を失わないため）
                record = _merge_rebuilt_record(record, rebuilt) if isinstance(record, dict) else rebuilt
                stickers[sid] = record
                report["stickers"][sid] = {
                    "added": [v["variant_id"] for v in record["variants"]],
                    "adopted": record.get("adopted"), "rebuilt": True,
                }
            return None if report["stickers"] else UNCHANGED

        report["changed"] = update(config, mutate) is not UNCHANGED
    return report


def _recovered_item(config, path: Path) -> dict:
    created = datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
    return {"variant_id": path.stem, "file": relative_file(config, path), "created_at": created,
            "source": SOURCE_RECOVERED, "verdict": VERDICT_PENDING, "human_rating": None,
            "note": "修復で候補フォルダから復元"}


def _add_missing_variants(config, record: dict, files: list[Path]) -> list[str]:
    known = {str(v.get("variant_id")) for v in record.get("variants", []) if isinstance(v, dict)}
    added = []
    for path in files:
        if path.stem not in known:
            record["variants"].append(_recovered_item(config, path))
            added.append(path.stem)
    if added:
        record["variants"].sort(key=lambda v: str(v.get("variant_id", "")) if isinstance(v, dict) else "")
        highest = max(int(v["variant_id"][1:]) for v in record["variants"]
                      if isinstance(v, dict) and str(v.get("variant_id", ""))[1:].isdigit())
        record["next_seq"] = max(int(record.get("next_seq") or 1), highest + 1)
    return added


def _rebuild_record(config, sticker_id: str, files: list[Path], existing=None) -> dict:
    """候補フォルダの画像から記録を作り直します。existing は候補0件の既存の記録（無ければ None）。

    原画は、通常の取り込みと同じ規則で扱います: 初回生成の途中（existing に initial_run がある）なら
    取り込まず、取り込むときは existing の予約済みの番号を避けます（ensure_record と同じ）。
    """
    items = [_recovered_item(config, path) for path in files]
    record = {"adopted": None, "adopted_at": None, "variants": items}
    generated = config.dir_generated / f"{sticker_id}.png"
    if generated.exists() and not _initial_run_open(existing):
        gen_sha = sha1_file(generated)
        match = next((item for item, path in zip(items, files) if sha1_file(path) == gen_sha), None)
        if match is None:
            legacy = ensure_legacy_variant(config, sticker_id, existing)
            with _sticker_lock_for_copy(config, sticker_id):     # repair_state が持っています
                kept, _stamp = _materialize_legacy(config, sticker_id, legacy.variants[0], existing)
            match = kept.to_dict()
            items.append(match)
            items.sort(key=lambda v: v["variant_id"])
        match["verdict"] = VERDICT_ADOPTED
        record["adopted"] = match["variant_id"]
        stat = generated.stat()
        record["adopted_file"] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha1": gen_sha}
    record["next_seq"] = max(int(v["variant_id"][1:]) for v in items) + 1
    return record


def _live_run_reservations(record) -> set[str]:
    """動いている実行（owner が生きている initial_run / regen_run）が予約し、まだ登録していない番号。

    止まった実行（owner が動いていない）の番号は含めません。その画像は repair が取り込み、
    再開したときに登録済みとして数えます（途中で落ちた実行の画像を失わないため）。
    """
    if not isinstance(record, dict):
        return set()
    active: set[str] = set()
    for key in ("initial_run", "regen_run"):
        run = record.get(key)
        if not isinstance(run, dict) or not _regen_owner_alive(run.get("owner")):
            continue
        done = run.get("done") if isinstance(run.get("done"), list) else []
        if isinstance(run.get("reserved"), list):
            active.update(v for v in run["reserved"] if isinstance(v, str) and v not in done)
    return active


_REBUILT_KEYS = ("adopted", "adopted_at", "adopted_file", "variants")   # repair が候補フォルダから作り直す項目
_RUN_KEYS = ("initial_run", "regen_run")                                  # 番号を予約する実行の記録


def _merge_rebuilt_record(existing: dict, rebuilt: dict) -> dict:
    """候補0件の既存の記録に、作り直した候補と採用の項目だけを反映します。

    実行の記録（initial_run / regen_run）・知らない項目はそのまま残します。
    next_seq は後退させません（既存の値・作り直した値・実行の記録で予約した番号の次、の最大）。
    予約済みの番号を空きに戻すと、別の処理が同じ番号をもう一度使ってしまうためです。
    """
    merged = dict(existing)
    for key in _REBUILT_KEYS:
        if key in rebuilt:
            merged[key] = rebuilt[key]
        else:
            merged.pop(key, None)            # 作り直した採用状態と食い違う古い目印は残しません
    seqs = [rebuilt["next_seq"]]
    kept = existing.get("next_seq")
    if isinstance(kept, int) and not isinstance(kept, bool):
        seqs.append(kept)
    for key in _RUN_KEYS:
        run = existing.get(key)
        if not isinstance(run, dict):
            continue
        numbers = [v for k in ("reserved", "done") if isinstance(run.get(k), list) for v in run[k]]
        for variant_id in numbers:
            if isinstance(variant_id, str) and variant_id[1:].isdigit():
                seqs.append(int(variant_id[1:]) + 1)
    merged["next_seq"] = max(seqs)
    return merged


def _main(argv: list[str] | None = None) -> int:
    """python -m src.variants repair [--config 設定ファイル]"""
    import argparse

    from .config import load_config

    parser = argparse.ArgumentParser(prog="python -m src.variants",
                                     description="候補の記録（variants.json）の保守")
    parser.add_argument("--config", help="設定ファイルのパス")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("repair", help="壊れた記録を退避し、候補フォルダの画像から記録を作り直す")
    args = parser.parse_args(argv)

    config = load_config(args.config, load_env=False)
    try:
        report = repair_state(config)
    except VariantError as exc:
        print(f"ERROR:\n  {exc}", file=sys.stderr)
        return 1

    kind = report["kind"]
    if kind == REPAIR_TEMP_ONLY:
        print(f"記録ファイルがありません: {config.variants_path}")
        print("  書き込み途中の一時ファイルが残っています（内容が最新かは分からないため、自動では使いません）:")
        for name in report["temp_files"]:
            print(f"    {name}")
        print("  内容を確認し、使う場合は variants.json に名前を変えてから、もう一度実行してください。"
              "使わない場合は削除してから実行してください。")
        return 1
    if kind == REPAIR_CORRUPT:
        print(f"壊れた記録を退避しました（削除していません）: {report['quarantined']}")
    elif kind == REPAIR_MISSING:
        print(f"記録ファイルがありません: {config.variants_path}")
    else:
        print("記録は正常です。")

    for sid, info in report["stickers"].items():
        if info["rebuilt"]:
            print(f"  {sid}: 候補フォルダの画像から記録を作り直しました {', '.join(info['added'])}"
                  f"（採用中: {info['adopted'] or '-'}）")
        else:
            print(f"  {sid}: 記録に無かった候補を追加しました {', '.join(info['added'])}"
                  f"（採用中は変えていません: {info['adopted'] or '-'}）")
    for path in report["unreadable"]:
        print(f"  WARNING: 読めない画像のため戻していません: {path}")

    if not report["changed"]:
        print("変更はありません（記録ファイルは書き換えていません）。" if kind != REPAIR_MISSING
              else "候補フォルダに画像も無いため、直すものはありません（記録ファイルは作っていません）。")
    elif any(info["rebuilt"] for info in report["stickers"].values()):
        print("作り直した候補の判断・評価は復元できないため、候補比較の画面で付け直してください。")
    return 0


if __name__ == "__main__":
    # 「python -m src.variants」で実行したときも、通常の import と同じモジュールの
    # 関数を使います（鍵の管理などを二重に持たないため）
    from src.variants import _main as _run

    sys.exit(_run())
