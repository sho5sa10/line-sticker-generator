"""原画 + セリフ → LINE規格の完成PNG。

CLI (main.py) と Web GUI (webapp.py) の両方から使われる共通処理です。
原画 output/generated/*.png は読み取るだけで、決して変更しません。
"""

from __future__ import annotations

from pathlib import Path

from . import image_processor as ip
from .csv_loader import StickerEntry
from .text_renderer import TextStyle, render_text_image


def render_final(
    config,
    entry: StickerEntry,
    style: TextStyle,
    *,
    output_path: Path | None = None,
) -> tuple[Path, int, list[str]]:
    """1件分の完成画像を書き出します。

    Args:
        output_path: 指定するとそこへ保存します（プレビュー用の一時出力など）。

    Returns:
        (保存先, バイト数, 警告リスト)
    """
    return _render(config, entry, style, output_path=output_path)


def compose_final_image(config, entry: StickerEntry, style: TextStyle):
    """保存せずに合成結果の Image を返します（ライブプレビュー用）。"""
    canvas_w, canvas_h = config.sticker_size
    margin = config.margin
    character = ip.make_background_transparent(
        ip.load_rgba(config.dir_generated / f"{entry.id}.png")
    )
    text_img = render_text_image(
        entry.text,
        style,
        canvas_w - margin * 2,
        int((canvas_h - margin * 2) * float(config.get("font.band_ratio", 0.40))),
    )
    return ip.compose_sticker(
        character,
        text_img,
        (canvas_w, canvas_h),
        margin,
        gap=int(config.get("font.gap", 4)),
        text_position=str(config.get("font.position", "bottom")),
    )


def _render(config, entry, style, *, output_path=None):
    result = compose_final_image(config, entry, style)
    out = output_path or (config.dir_final / f"{entry.id}.png")
    path, size_bytes, save_warnings = ip.save_png(
        result.image, out, config.max_file_size_bytes
    )
    return path, size_bytes, result.warnings + save_warnings
