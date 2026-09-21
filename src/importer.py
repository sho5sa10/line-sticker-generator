"""手持ちの画像をスタンプの原画として取り込みます（APIを使わない＝無料）。

外部ツールで作った画像や自分で描いた絵を output/generated/<id>.png に置き、
あとは通常どおり 文字合成 → LINE規格化 → 検証 を行います。

既存の原画を置き換えるときは、上書きせず output/archive/replaced/ へ退避します。
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from PIL import Image

from . import image_processor as ip
from . import pipeline
from . import validator as vd
from .csv_loader import StickerEntry

# 取り込み時に長辺をこのサイズまで縮小します（巨大画像で処理が重くならないように）。
MAX_SIDE = 2048

_ID_PATTERN = re.compile(r"^\D*(\d{1,4})\D*$")


class ImportError_(Exception):
    """取り込みに失敗した場合に送出されます（組み込みの ImportError と区別）。"""


@dataclass
class ImportResult:
    """1枚分の取り込み結果。"""

    sticker_id: str
    ok: bool
    message: str = ""
    archived: str | None = None
    issues: list[str] = field(default_factory=list)
    size_kb: float = 0.0


def archive_dir(config) -> Path:
    return config.root / "output" / "archive" / "replaced"


def archive_existing(config, sticker_id: str) -> Path | None:
    """既存の原画を退避します。原画が無ければ何もしません。

    output/generated/ の中には置かないので、main/tab の自動選択などに混ざりません。
    """
    src = config.dir_generated / f"{sticker_id}.png"
    if not src.exists():
        return None
    dest_dir = archive_dir(config)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    dest = dest_dir / f"{sticker_id}_{stamp}.png"
    src.replace(dest)
    return dest


def id_from_filename(name: str) -> int | None:
    """"001.png" や "sticker_12.jpg" からIDの数字を取り出します。"""
    m = _ID_PATTERN.match(Path(name).stem)
    return int(m.group(1)) if m else None


def load_image_bytes(data: bytes) -> Image.Image:
    """画像データを検証して RGBA に変換します。背景が不透明なら透過を試みます。"""
    if not data:
        raise ImportError_("ファイルが空です")
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            rgba = im.convert("RGBA")
    except Exception as exc:  # noqa: BLE001 - 画像でないものは種類を問わず拒否
        raise ImportError_("画像として読み込めませんでした") from exc

    if max(rgba.size) > MAX_SIDE:
        scale = MAX_SIDE / max(rgba.size)
        rgba = rgba.resize(
            (max(1, round(rgba.width * scale)), max(1, round(rgba.height * scale))),
            Image.LANCZOS,
        )
    if not ip.has_transparency(rgba):
        rgba = ip.make_background_transparent(rgba)
    if ip.is_blank(rgba):
        raise ImportError_("画像の中身が空です（全ピクセルが透明）")
    return rgba


def import_image(config, entry: StickerEntry, data: bytes, style, logger=None, state=None) -> ImportResult:
    """1枚取り込み、そのまま完成画像まで作って検証します。"""
    try:
        rgba = load_image_bytes(data)
    except ImportError_ as exc:
        if logger:
            logger.error(entry.id, f"IMPORT FAILED: {exc}")
        return ImportResult(entry.id, False, str(exc))

    config.dir_generated.mkdir(parents=True, exist_ok=True)
    archived = archive_existing(config, entry.id)
    rgba.save(config.dir_generated / f"{entry.id}.png", format="PNG")
    if logger:
        logger.event(entry.id, "IMPORTED", "API was not called")

    try:
        path, size_bytes, warnings = pipeline.render_final(config, entry, style)
    except Exception as exc:  # noqa: BLE001 - 取り込み自体は成功しているので理由を返す
        if logger:
            logger.error(entry.id, f"{type(exc).__name__}: {exc}")
        return ImportResult(entry.id, False, f"文字合成に失敗しました: {exc}",
                            archived=archived.name if archived else None)

    report = vd.validate_sticker(path, config)
    issues = warnings + [i.message for i in report.issues]
    if logger:
        logger.event(entry.id, "TEXT RENDERED", f"{size_bytes / 1024:.0f}KB")
        logger.event(entry.id, "VALIDATION PASS" if report.ok else "VALIDATION FAIL")
    if state:
        state.set(entry.id, "imported" if report.ok else "validation_failed",
                  "" if report.ok else report.errors[0].message)

    return ImportResult(
        entry.id,
        report.ok,
        "取り込みました" if report.ok else "取り込みましたが検証エラーがあります",
        archived=archived.name if archived else None,
        issues=issues,
        size_kb=round(size_bytes / 1024, 1),
    )
