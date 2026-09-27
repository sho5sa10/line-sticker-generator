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

import hashlib
import json
import os
import shutil
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

SCHEMA = 1

# 候補の出どころ
SOURCE_API = "api"          # 画像生成APIで作った
SOURCE_IMPORT = "import"    # 手持ち画像を取り込んだ
SOURCE_LEGACY = "legacy"    # variants.json 導入前からある generated/<id>.png

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


def state_status(config) -> str:
    """variants.json の状態。"missing" / "ok" / "corrupt"。

    「ファイルが無い」と「壊れている」を区別します。壊れているときに空の状態で
    上書きすると、採用状態や人の判断がすべて消えてしまうためです。
    """
    path = config.variants_path
    if not path.exists():
        return STATE_MISSING
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return STATE_CORRUPT
    if not isinstance(data, dict) or not isinstance(data.get("stickers"), dict):
        return STATE_CORRUPT
    return STATE_OK


def is_corrupt(config) -> bool:
    return state_status(config) == STATE_CORRUPT


def corrupt_message(config) -> str:
    return (f"候補の記録が壊れているため保存できません: {config.variants_path}\n"
            "  中身を直すか、`python -m src.main variants repair` で退避してください"
            "（退避すると、候補の採用状態・判断・評価は失われます）。")


def quarantine_corrupt_state(config) -> Path | None:
    """壊れた variants.json を variants.json.corrupt-<日時> へ退避します（削除しません）。"""
    path = config.variants_path
    if state_status(config) != STATE_CORRUPT:
        return None
    dest = path.with_name(f"{path.name}.corrupt-{datetime.now():%Y%m%d_%H%M%S}")
    path.replace(dest)
    return dest


def load(config) -> dict:
    """variants.json を読みます。

    無い場合・壊れている場合は空の状態を返します（例外は投げません）。
    壊れている場合だけ、既存の StateStore と同じ方式で警告を出します。
    ただし、この状態のまま save() すると記録を失うため、保存側で拒否します。
    """
    path = config.variants_path
    status = state_status(config)
    if status == STATE_MISSING:
        return _empty_state()
    if status == STATE_CORRUPT:
        print(f"WARNING: 候補の記録を読めないため、generated/ の画像だけを使います: {path}",
              file=sys.stderr)
        return _empty_state()
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("schema", SCHEMA)
    data.setdefault("defaults", {})
    return data


def save(config, data: dict, *, force: bool = False) -> Path:
    """variants.json を原子的に書き換えます（.tmp に書いてから置換）。

    壊れたファイルがある状態では、既存の記録を消さないために拒否します
    （force=True は、退避したあとの書き込みなど、明示的に上書きしたい場合だけ）。
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
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def _empty_state() -> dict:
    return {"schema": SCHEMA, "defaults": {}, "stickers": {}}


# ---------------------------------------------------------------------------
# 排他制御（同時に書き込んでも、あとから来た更新で前の更新が消えないように）
# ---------------------------------------------------------------------------
LOCK_TIMEOUT_SEC = 20.0
LOCK_STALE_SEC = 120.0                # 強制終了などで残ったロックを無効とみなす時間
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _thread_lock(key: str) -> threading.RLock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


@contextmanager
def file_lock(lock_path: Path, timeout: float = LOCK_TIMEOUT_SEC):
    """指定のロックファイルで、プロセス内（GUIの複数リクエスト）とプロセス間を排他します。"""
    lock_path = Path(lock_path)
    with _thread_lock(str(lock_path)):
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
            lock_path.unlink(missing_ok=True)


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
        if is_corrupt(config):
            raise StateCorruptError(corrupt_message(config))
        state = load(config)
        result = mutate(state)
        save(config, state)
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
    v = Variant(
        variant_id="v001",
        file=relative_file(config, src),
        source=SOURCE_LEGACY,
        verdict=VERDICT_ADOPTED,
        created_at=None,
    )
    return StickerVariants(sticker_id=sticker_id, adopted="v001", next_seq=2,
                           variants=[v], legacy=True)


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

    記録が無く generated/<id>.png がある環境では、その画像を v001(legacy・採用中)
    として記録に残します。**ファイルはコピーせず、generated/ のパスを指したままです。**
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
        items = []
        for v in legacy.variants:
            kept = _materialize_legacy(config, sticker_id, v)
            items.append(kept.to_dict())
        record = {
            "adopted": legacy.adopted,
            "adopted_at": None,
            "next_seq": legacy.next_seq,
            "variants": items,
        }
    else:
        record = {"adopted": None, "adopted_at": None, "next_seq": 1, "variants": []}
    stickers[sticker_id] = record
    return record


def _materialize_legacy(config, sticker_id: str, variant: Variant) -> Variant:
    """legacy の候補（generated/<id>.png）を候補置き場へコピーして、そちらを指させます。"""
    src = variant.path(config)
    dest = variant_dir(config, sticker_id) / f"{variant.variant_id}.png"
    if not src.exists() or dest.exists():
        return variant if not dest.exists() else _with_file(config, variant, dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    return _with_file(config, variant, dest)


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


def generate_variants(config, entries, count: int, generator, *, data: dict | None = None,
                      on_event=None) -> list[dict]:
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
        _generate_for_entry(config, entry, count, generator, results, on_event, data)
    return results


def _generate_for_entry(config, entry, count: int, generator, results: list[dict],
                        on_event, data: dict | None) -> None:
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

        if data is not None:
            variant_id, path = reserve(data)
        else:
            variant_id, path = update(config, reserve)      # 番号は先に確定・保存

        result = generator.generate_one(entry, output_path=path)
        if result.status == "generated" and path.exists():
            meta = generation_meta(config, generator, prompt, entry.id)

            def register(state):    # noqa: B023 - 直後に実行します
                record = ensure_record(config, state, entry.id)
                register_variant(config, record, variant_id, path, meta=meta)

            # PNG が出来てから記録します（記録だけ残る状態を作らない）
            if data is not None:
                register(data)
            else:
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


def set_verdict(config, sticker_id: str, variant_id: str, verdict: str,
                *, data: dict | None = None) -> dict:
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

    if data is not None:            # 呼び出し側が状態を持っている場合はその場で変更
        return mutate(data)
    return update(config, mutate)


def set_rating(config, sticker_id: str, variant_id: str, rating: int | None,
               *, data: dict | None = None) -> dict:
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

    if data is not None:
        return mutate(data)
    return update(config, mutate)


def _rollback_adoption(config, entry, style, dest: Path, archived, final_path: Path,
                       had_final: bool) -> None:
    """採用に失敗したとき、原画と完成画像を元の状態に戻します（できる範囲で）。"""
    from . import pipeline

    try:
        if archived is not None and Path(archived).exists():
            Path(archived).replace(dest)         # 退避した原画を戻す
        else:
            dest.unlink(missing_ok=True)         # 元々無かったので作りかけを消す
        if dest.exists():
            pipeline.render_final(config, entry, style)     # 完成画像も元の原画から作り直す
        elif not had_final:
            final_path.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001 - 巻き戻しの失敗で元の原因を隠さない
        print(f"WARNING: 採用の巻き戻しに失敗しました: {type(exc).__name__}: {exc}",
              file=sys.stderr)


def adopt(config, entry, variant_id: str, *, style=None, data: dict | None = None) -> dict:
    """候補を「採用中の原画」にして、既存の合成・検証をそのまま通します。

    順序:
      記録を読む → 候補とPNGを確認 → 既存原画を退避（既存の archive_existing）
      → 候補を generated/<id>.png にコピー → 既存の pipeline.render_final
      → 既存の validator → 最後に variants.json を更新（原子的に保存）

    途中で失敗した場合、variants.json は更新しません（adopted は前のまま）。
    候補ファイル（variants/ 側）は移動も削除もしません。
    """
    from . import image_processor as ip
    from . import pipeline
    from . import validator as vd
    from .importer import archive_existing
    from .text_renderer import TextStyle

    if is_corrupt(config):          # 壊れた記録を上書きしないよう、何より先に止めます
        raise StateCorruptError(corrupt_message(config))

    with adopt_lock(config, entry.id):      # 同じスタンプの採用どうしだけを直列化します
        return _adopt_locked(config, entry, variant_id, style=style, data=data)


def _adopt_locked(config, entry, variant_id: str, *, style=None, data=None) -> dict:
    from . import image_processor as ip
    from . import pipeline
    from . import validator as vd
    from .importer import archive_existing
    from .text_renderer import TextStyle

    state = load(config) if data is None else data
    sticker = get_sticker(config, entry.id, state)
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

    dest = config.dir_generated / f"{entry.id}.png"
    final_path = config.dir_final / f"{entry.id}.png"
    had_final = final_path.exists()
    archived = None
    style = style or TextStyle.from_config(config)

    # legacy の候補（generated/<id>.png 自身）を採用する場合は、退避もコピーも不要です。
    same_file = src.resolve() == dest.resolve()
    try:
        if not same_file:
            archived = archive_existing(config, entry.id)      # 既存の退避処理を再利用
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)

        # ここから先は既存の正式な経路をそのまま使います（final を直接コピーしない）
        final_path, size_bytes, warnings = pipeline.render_final(config, entry, style)
        report = vd.validate_sticker(final_path, config)
        if not report.ok:
            raise AdoptError(
                "採用した画像がLINE仕様を満たしませんでした（採用状態は変更していません）:\n  "
                + "\n  ".join(i.message for i in report.errors)
            )
    except Exception:
        # 失敗したら、触ったファイルを元に戻します（記録と画像が食い違わないように）
        if not same_file:
            _rollback_adoption(config, entry, style, dest, archived, final_path, had_final)
        raise

    stamp = file_stamp(dest)

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

    # 画像の処理が終わってから、最新の記録を読み直して採用状態だけを更新します
    if data is not None:
        apply(data)
        save(config, data)
    else:
        update(config, apply)

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
