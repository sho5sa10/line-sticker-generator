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
import shutil
import sys
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
def load(config) -> dict:
    """variants.json を読みます。

    無い場合・壊れている場合は空の状態を返します（例外は投げません）。
    壊れている場合だけ、既存の StateStore と同じ方式で警告を出します。
    """
    path = config.variants_path
    if not path.exists():
        return _empty_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print(f"WARNING: 候補の記録を読めないため、generated/ の画像だけを使います: {path}",
              file=sys.stderr)
        return _empty_state()
    if not isinstance(data, dict) or not isinstance(data.get("stickers"), dict):
        print(f"WARNING: 候補の記録の形式が不正です。generated/ の画像だけを使います: {path}",
              file=sys.stderr)
        return _empty_state()
    data.setdefault("schema", SCHEMA)
    data.setdefault("defaults", {})
    return data


def save(config, data: dict) -> Path:
    """variants.json を原子的に書き換えます（.tmp に書いてから置換）。

    Phase 1a では呼び出し箇所はありません（採用・生成は未実装）。
    """
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
    )


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
        record = {
            "adopted": legacy.adopted,
            "adopted_at": None,
            "next_seq": legacy.next_seq,
            "variants": [v.to_dict() for v in legacy.variants],
        }
    else:
        record = {"adopted": None, "adopted_at": None, "next_seq": 1, "variants": []}
    stickers[sticker_id] = record
    return record


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
    state = load(config) if data is None else data
    results: list[dict] = []
    for entry in entries:
        try:
            _generate_for_entry(config, state, entry, count, generator, results, on_event)
        finally:
            # 途中で例外が出ても、そこまでの記録（と消費した番号）は残します
            if not getattr(generator, "dry_run", False):
                save(config, state)
    return results


def _generate_for_entry(config, state: dict, entry, count: int, generator,
                        results: list[dict], on_event) -> None:
    record = ensure_record(config, state, entry.id)
    prompt = generator.prompt_for(entry)
    for _ in range(max(int(count), 1)):
        if getattr(generator, "dry_run", False):
            results.append({"id": entry.id, "variant_id": None, "status": "dry-run",
                            "path": None, "detail": ""})
            if on_event:
                on_event(entry.id, None, "dry-run", "")
            continue

        variant_id, path = allocate_variant(config, record, entry.id)
        result = generator.generate_one(entry, output_path=path)
        if result.status == "generated" and path.exists():
            # PNG が出来てから記録します（記録だけ残る状態を作らない）
            register_variant(config, record, variant_id, path,
                             meta=generation_meta(config, generator, prompt, entry.id))
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
    state = load(config) if data is None else data
    _record, item = _find_item(config, state, sticker_id, variant_id)
    item["verdict"] = verdict
    save(config, state)
    return item


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
    state = load(config) if data is None else data
    _record, item = _find_item(config, state, sticker_id, variant_id)
    item["human_rating"] = rating
    save(config, state)
    return item


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
    archived = None
    # legacy の候補（generated/<id>.png 自身）を採用する場合は、退避もコピーも不要です。
    if src.resolve() != dest.resolve():
        archived = archive_existing(config, entry.id)      # 既存の退避処理を再利用
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)

    # ここから先は既存の正式な経路をそのまま使います（final を直接コピーしない）
    style = style or TextStyle.from_config(config)
    final_path, size_bytes, warnings = pipeline.render_final(config, entry, style)
    report = vd.validate_sticker(final_path, config)
    if not report.ok:
        raise AdoptError(
            "採用した画像がLINE仕様を満たしませんでした（採用状態は変更していません）:\n  "
            + "\n  ".join(i.message for i in report.errors)
        )

    # すべて成功してから記録を更新します
    record = ensure_record(config, state, entry.id)
    for item in record.get("variants", []):
        if isinstance(item, dict) and item.get("variant_id") == variant_id:
            item["verdict"] = VERDICT_ADOPTED       # 採用した候補だけ変えます
    record["adopted"] = variant_id                  # ほかの候補の verdict は触りません
    record["adopted_at"] = datetime.now().isoformat(timespec="seconds")
    save(config, state)

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
