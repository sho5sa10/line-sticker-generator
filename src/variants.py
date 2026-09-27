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
from contextlib import contextmanager
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
        text = _retry_io(lambda: _read_file_bytes(path)).decode("utf-8")
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
LOCK_STALE_SEC = 120.0                # 強制終了などで残ったロックを無効とみなす時間
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


_HELD = threading.local()            # このスレッドがいま持っている鍵（入れ子で取れるように）


def _thread_lock(key: str) -> threading.RLock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


@contextmanager
def file_lock(lock_path: Path, timeout: float = LOCK_TIMEOUT_SEC):
    """指定のロックファイルで、プロセス内（GUIの複数リクエスト）とプロセス間を排他します。

    同じスレッドがすでに持っている鍵は、そのまま入れ子で使えます
    （ロックファイルを二重に作ろうとして自分自身を待ち続けないように）。
    """
    lock_path = Path(lock_path)
    key = str(lock_path)
    held = getattr(_HELD, "keys", None)
    if held is None:
        held = _HELD.keys = set()
    if key in held:
        yield
        return
    with _thread_lock(key):
        held.add(key)
        try:
            with _exclusive_file(lock_path, timeout):
                yield
        finally:
            held.discard(key)


@contextmanager
def _exclusive_file(lock_path: Path, timeout: float):
    """ロックファイルを O_EXCL で作ってプロセス間を排他します。"""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    fd = None
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:    # 古いロックは、異常終了の置き土産とみなして外します
                if time.time() - lock_path.stat().st_mtime > LOCK_STALE_SEC:
                    lock_path.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
        except PermissionError:
            pass    # Windows: 削除処理中のロックファイルは一時的に開けません
        if time.monotonic() > deadline:
            raise VariantError(
                f"候補の記録が他の処理で使用中です（{lock_path}）。"
                "しばらく待ってからやり直してください。") from None
        time.sleep(0.05)
    try:
        os.write(fd, str(os.getpid()).encode())
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


def adopt_lock(config, sticker_id: str, timeout: float = LOCK_TIMEOUT_SEC):
    """1スタンプの採用処理の鍵。

    採用は generated/<id>.png と final/<id>.png を書き換えるため、同じスタンプの採用が
    並行すると「記録は A、画像は B」という食い違いが起きます。状態ファイルの鍵とは別に、
    スタンプ単位で採用だけを直列化します（別のスタンプの採用や、評価・判断は止めません）。
    """
    return file_lock(config.variants_path.with_name(f"variants.adopt-{sticker_id}.lock"), timeout)


def update(config, mutate, *, timeout: float = LOCK_TIMEOUT_SEC):
    """鍵を取り、**最新の記録を読み直してから** 変更を適用して保存します。

    古い状態をメモリに持ったまま保存すると、その間に入った他の更新
    （評価・判断・採用）が消えてしまうため、必ずこの入口を通します。

    Args:
        mutate: 最新の state を受け取って書き換える関数。戻り値はそのまま返します。
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
        try:
            save(config, state)
        except PermissionError as exc:      # 再試行しても置き換えられなかった（元のファイルは無傷）
            raise StateWriteError(
                f"候補の記録を一時的に保存できませんでした（他の処理が使用中の可能性）: "
                f"{config.variants_path}\n  少し待ってからやり直してください（{exc}）") from exc
        return result


# ---------------------------------------------------------------------------
# 取得
# ---------------------------------------------------------------------------
def ensure_legacy_variant(config, sticker_id: str) -> StickerVariants | None:
    """記録が無いスタンプを、generated/<id>.png を指す仮想の v001 として扱います。

    **ファイルのコピー・移動はしません。variants.json も作りません。**
    generated/<id>.png が無ければ None を返します。
    """
    src = config.dir_generated / f"{sticker_id}.png"
    if not src.exists():
        return None
    # 実体化したときと同じ番号で見せます（置き場に別の画像の v001 があれば、その次）
    slot, _reuse = _legacy_slot(config, sticker_id, src)
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


def _legacy_slot(config, sticker_id: str, src: Path, src_sha: str | None = None) -> tuple[Path, bool]:
    """legacy の画像を置く場所と、その場所の既存ファイルをそのまま使えるか。

    置き場に v001.png が無ければ v001（普段はここで終わり、ハッシュも計算しません）。
    ある場合は: 中身が同じ → そのまま使う / 読めない → 作り直す / 別の画像 → 次の番号。
    """
    folder = variant_dir(config, sticker_id)
    seq = 1
    while True:
        dest = folder / f"v{seq:03d}.png"
        if not dest.exists():
            return dest, False
        src_sha = src_sha or sha1_file(src)
        if sha1_file(dest) == src_sha:
            return dest, True
        if not _is_readable_png(dest):
            return dest, False          # 半端なコピーなど。作り直します
        seq += 1                        # 別の候補の画像。消さずに次の番号へ


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
        # 記録はあるが候補が1件も読めない場合も、既存画像で動けるようにします。
        return ensure_legacy_variant(config, sticker_id)

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


def file_stamp(path: str | Path) -> dict | None:
    """ファイルの目印（サイズ・更新日時・SHA1）。差し替えの検出に使います。"""
    p = Path(path)
    try:
        stat = p.stat()
    except OSError:
        return None
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha1": sha1_file(p)}


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
    """
    stickers = data.setdefault("stickers", {})
    record = stickers.get(sticker_id)
    if isinstance(record, dict) and record.get("variants"):
        record.setdefault("next_seq", len(record["variants"]) + 1)
        return record

    legacy = ensure_legacy_variant(config, sticker_id)
    if legacy is not None:
        # 記録に残す時点で、候補置き場へ実体をコピーします。
        # generated/<id>.png は採用のたびに中身が変わるため、そこを指したままだと
        # v001 が「いま採用中の画像」の別名になり、元の絵が追えなくなります。
        kept, stamp = _materialize_legacy(config, sticker_id, legacy.variants[0])
        seq = int(kept.variant_id[1:])
        record = {
            "adopted": kept.variant_id,
            "adopted_at": None,
            "next_seq": max(legacy.next_seq, seq + 1),
            "variants": [kept.to_dict()],
            # いまの generated/<id>.png の目印。これ以降の差し替えに気付けるようにします
            "adopted_file": stamp,
        }
    else:
        record = {"adopted": None, "adopted_at": None, "next_seq": 1, "variants": []}
    stickers[sticker_id] = record
    return record


def _is_readable_png(path: Path) -> bool:
    from PIL import Image

    try:
        with Image.open(path) as img:
            img.verify()
        return True
    except Exception:  # noqa: BLE001 - 読めない理由は問いません
        return False


def _materialize_legacy(config, sticker_id: str, variant: Variant) -> tuple[Variant, dict]:
    """legacy の候補（generated/<id>.png）を候補置き場へコピーして、そちらを指させます。

    コピーは「一時ファイル → SHA1確認 → 置き換え」で行い、途中で失敗しても半端な
    vNNN.png を残しません。置き場所の決め方は _legacy_slot() を参照。
    Returns:
        (候補, generated の目印 {size, mtime_ns, sha1})
    """
    src = variant.path(config)
    stat = src.stat()
    src_sha = sha1_file(src)
    stamp = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha1": src_sha}
    dest, reuse = _legacy_slot(config, sticker_id, src, src_sha)
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
    # 記録に無くてもファイルがあれば飛ばします（中断後の再実行で上書きしないため）
    while f"v{seq:03d}" in used or (folder / f"v{seq:03d}.png").exists():
        seq += 1
    variant_id = f"v{seq:03d}"
    record["next_seq"] = seq + 1
    return variant_id, folder / f"{variant_id}.png"


def register_variant(config, record: dict, variant_id: str, path: Path, *, meta: dict) -> dict:
    """生成できた候補をメタデータ付きで記録します（PNGが実在する前提で呼びます）。"""
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


def generate_variants(config, entries, count: int, generator, *, on_event=None) -> list[dict]:
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
        _generate_for_entry(config, entry, count, generator, results, on_event)
    return results


def _generate_for_entry(config, entry, count: int, generator, results: list[dict],
                        on_event) -> None:
    """1スタンプ分。番号の確保と記録は、そのつど最新の状態に対して行います。

    生成には時間がかかるため、その間ずっと古い状態を持たないようにしています
    （持ったまま保存すると、途中で入った評価・判断・採用が消えます）。
    """
    prompt = generator.prompt_for(entry)
    for _ in range(max(int(count), 1)):
        if getattr(generator, "dry_run", False):
            results.append({"id": entry.id, "variant_id": None, "status": "dry-run",
                            "path": None, "detail": ""})
            if on_event:
                on_event(entry.id, None, "dry-run", "")
            continue

        def reserve(state):     # noqa: B023 - ループごとに即時実行します
            record = ensure_record(config, state, entry.id)
            return allocate_variant(config, record, entry.id)

        variant_id, path = update(config, reserve)      # 番号は先に確定・保存

        result = generator.generate_one(entry, output_path=path)
        if result.status == "generated" and path.exists():
            meta = generation_meta(config, generator, prompt, entry.id)

            def register(state):    # noqa: B023 - 直後に実行します
                record = ensure_record(config, state, entry.id)
                register_variant(config, record, variant_id, path, meta=meta)

            # PNG が出来てから記録します（記録だけ残る状態を作らない）
            update(config, register)
            results.append({"id": entry.id, "variant_id": variant_id, "status": "generated",
                            "path": path, "detail": ""})
        else:
            # 失敗しても番号は戻しません（課金済みの可能性があるため）
            results.append({"id": entry.id, "variant_id": variant_id, "status": "error",
                            "path": None, "detail": result.detail or "生成に失敗しました"})
        if on_event:
            on_event(entry.id, variant_id, results[-1]["status"], results[-1]["detail"])


# ---------------------------------------------------------------------------
# 採用（Phase 1c）
# ---------------------------------------------------------------------------
class VariantError(Exception):
    """候補の操作ができない場合に送出されます（状態ファイルは更新されません）。"""


class StateCorruptError(VariantError):
    """variants.json が壊れていて、上書きすると記録を失う場合に送出されます。"""


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
        return item

    return update(config, mutate)


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

    return update(config, mutate)


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


def repair_state(config) -> dict:
    """候補の記録を修復します。

    1. variants.json が壊れていれば、消さずに variants.json.corrupt-<日時>-<一意ID> へ退避
    2. 候補フォルダ（output/variants/<id>/vNNN.png）を調べ、記録に無い画像を候補として戻す
       - 記録が無いスタンプは作り直し、generated/<id>.png と中身が同じ候補を「採用中」にします
         （同じものが無ければ generated を legacy 候補として保存して採用中にします）
       - 記録があるスタンプは、採用状態・判断・評価を変えず、足りない候補だけ追加します
    画像ファイルは消しも上書きもしません。

    Returns:
        {"quarantined": 退避先 or None, "stickers": {id: {"added": [...], "adopted": id|None}},
         "unreadable": [読めなかった画像]}
    """
    report: dict = {"quarantined": None, "stickers": {}, "unreadable": []}
    with state_lock(config):
        moved = quarantine_corrupt_state(config)
        report["quarantined"] = str(moved) if moved else None

        def mutate(state):
            stickers = state.setdefault("stickers", {})
            root = config.dir_variants
            folders = sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []
            for folder in folders:
                sid = folder.name
                files = []
                for path in sorted(folder.glob(_VARIANT_FILE)):
                    if _is_readable_png(path):
                        files.append(path)
                    else:
                        report["unreadable"].append(relative_file(config, path))
                record = stickers.get(sid)
                if isinstance(record, dict) and record.get("variants"):
                    added = _add_missing_variants(config, record, files)
                    if added:
                        report["stickers"][sid] = {"added": added, "adopted": record.get("adopted")}
                    continue
                if not files:
                    continue
                record = _rebuild_record(config, sid, files)
                stickers[sid] = record
                report["stickers"][sid] = {
                    "added": [v["variant_id"] for v in record["variants"]],
                    "adopted": record.get("adopted"),
                }

        update(config, mutate)
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


def _rebuild_record(config, sticker_id: str, files: list[Path]) -> dict:
    items = [_recovered_item(config, path) for path in files]
    record = {"adopted": None, "adopted_at": None, "variants": items}
    generated = config.dir_generated / f"{sticker_id}.png"
    if generated.exists():
        gen_sha = sha1_file(generated)
        match = next((item for item, path in zip(items, files) if sha1_file(path) == gen_sha), None)
        if match is None:
            legacy = ensure_legacy_variant(config, sticker_id)
            kept, _stamp = _materialize_legacy(config, sticker_id, legacy.variants[0])
            match = kept.to_dict()
            items.append(match)
            items.sort(key=lambda v: v["variant_id"])
        match["verdict"] = VERDICT_ADOPTED
        record["adopted"] = match["variant_id"]
        stat = generated.stat()
        record["adopted_file"] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha1": gen_sha}
    record["next_seq"] = max(int(v["variant_id"][1:]) for v in items) + 1
    return record


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
    if report["quarantined"]:
        print(f"壊れた記録を退避しました（削除していません）: {report['quarantined']}")
    else:
        print("記録は壊れていませんでした（書き換えずに、足りない候補だけ確認しました）。")
    for sid, info in report["stickers"].items():
        print(f"  {sid}: 候補を戻しました {', '.join(info['added'])}"
              f"（採用中: {info['adopted'] or '-'}）")
    if not report["stickers"]:
        print("  戻す候補はありませんでした。")
    for path in report["unreadable"]:
        print(f"  WARNING: 読めない画像のため戻していません: {path}")
    print("判断・評価は復元できないため、候補比較の画面で付け直してください。")
    return 0


if __name__ == "__main__":
    # 「python -m src.variants」で実行したときも、通常の import と同じモジュールの
    # 関数を使います（鍵の管理などを二重に持たないため）
    from src.variants import _main as _run

    sys.exit(_run())
