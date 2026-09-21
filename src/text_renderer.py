"""日本語セリフを透過PNGとして描画します。

画像生成AIには文字を描かせず、ここで Pillow を使って合成します。
LINEの小さい表示でも読めるよう、太字フォント + 太い縁取りを使用します。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# 行頭に来てはいけない文字（禁則処理）
NO_LINE_START = set("、。，．・：；？！ゝゞーぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮ»）」』】〉》”’,.!?:;)]}〕")
# 行末に来てはいけない文字
NO_LINE_END = set("（「『【〈《“‘«([{〔")


class FontNotFoundError(Exception):
    """日本語フォントが見つからない場合に送出されます。"""


@dataclass
class TextStyle:
    """テキスト描画スタイル。すべて設定ファイルから変更できます。"""

    font_path: str
    font_index: int = 0
    size: int = 60
    min_size: int = 26
    stroke_width: int = 7
    fill: str = "#FFFFFF"
    stroke_fill: str = "#000000"
    max_lines: int = 3
    line_spacing: float = 1.06

    @classmethod
    def from_config(cls, config) -> "TextStyle":
        path = resolve_font_path(config)
        return cls(
            font_path=str(path),
            font_index=int(config.get("font.index", 0)),
            size=int(config.get("font.size", 60)),
            min_size=int(config.get("font.min_size", 26)),
            stroke_width=int(config.get("font.stroke_width", 7)),
            fill=str(config.get("font.fill", "#FFFFFF")),
            stroke_fill=str(config.get("font.stroke_fill", "#000000")),
            max_lines=int(config.get("font.max_lines", 3)),
            line_spacing=float(config.get("font.line_spacing", 1.06)),
        )


def resolve_font_path(config) -> Path:
    """フォントパスを解決します。

    font.path が指定されていればそれを使い、空なら font.candidates から
    実在する最初のフォントを自動検出します。パスはハードコードしません。
    """
    explicit = str(config.get("font.path", "") or "").strip()
    if explicit:
        p = Path(explicit)
        if not p.is_absolute():
            p = config.root / p
        if not p.exists():
            raise FontNotFoundError(
                f"設定された日本語フォントが見つかりません: {p}\n"
                "config/sticker_config.yaml の font.path を修正してください。"
            )
        return p

    for cand in config.get("font.candidates", []) or []:
        p = Path(str(cand))
        if p.exists():
            return p

    raise FontNotFoundError(
        "日本語フォントが見つかりませんでした。\n"
        "config/sticker_config.yaml の font.path に日本語フォント(.ttf/.ttc)の"
        "絶対パスを設定してください。"
    )


def load_font(style: TextStyle, size: int) -> ImageFont.FreeTypeFont:
    """指定サイズでフォントを読み込みます。"""
    try:
        return ImageFont.truetype(style.font_path, size=size, index=style.font_index)
    except OSError as exc:
        raise FontNotFoundError(f"フォントを読み込めません: {style.font_path} ({exc})") from exc


def _measure(text: str, font: ImageFont.FreeTypeFont, stroke_width: int) -> tuple[int, int]:
    """(幅, 高さ) をピクセルで返します。幅は縁取り込みの送り幅です。"""
    if not text:
        return 0, 0
    width = int(math.ceil(font.getlength(text))) + stroke_width * 2
    _, top, _, bottom = font.getbbox(text, stroke_width=stroke_width)
    return width, bottom - top


def wrap_text(
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
    stroke_width: int = 0,
) -> list[str]:
    """日本語テキストを max_width に収まるよう自動改行します。

    簡易的な禁則処理（行頭禁則・行末禁則）に対応します。
    明示的な改行 "\\n" は尊重されます。
    """
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")
            continue
        current = ""
        for ch in paragraph:
            candidate = current + ch
            width, _ = _measure(candidate, font, stroke_width)
            if width <= max_width or not current:
                current = candidate
                continue
            # 行頭禁則: この文字は次の行の先頭に置けないので、ぶら下げる
            if ch in NO_LINE_START:
                current = candidate
                continue
            # 行末禁則: 直前の文字が行末に置けないなら一緒に送る
            if current and current[-1] in NO_LINE_END:
                lines.append(current[:-1])
                current = current[-1] + ch
                continue
            lines.append(current)
            current = ch
        if current:
            lines.append(current)
    return lines or [""]


def fit_text(
    text: str,
    style: TextStyle,
    max_width: int,
    max_height: int,
) -> tuple[ImageFont.FreeTypeFont, list[str], int]:
    """枠に収まる最大のフォントサイズと改行結果を求めます。

    Returns:
        (font, lines, line_height)
    """
    size = max(style.size, style.min_size)
    best: tuple[ImageFont.FreeTypeFont, list[str], int] | None = None

    while size >= style.min_size:
        font = load_font(style, size)
        lines = wrap_text(text, font, max_width, style.stroke_width)
        ascent, descent = font.getmetrics()
        line_height = int((ascent + descent + style.stroke_width * 2) * style.line_spacing)
        total_h = line_height * len(lines)
        widest = max((_measure(ln, font, style.stroke_width)[0] for ln in lines), default=0)

        if len(lines) <= style.max_lines and total_h <= max_height and widest <= max_width:
            return font, lines, line_height

        best = (font, lines, line_height)
        size -= 2

    # min_size でも収まらない場合は最小サイズの結果を返します（呼び出し側で警告）。
    font = load_font(style, style.min_size)
    lines = wrap_text(text, font, max_width, style.stroke_width)
    ascent, descent = font.getmetrics()
    line_height = int((ascent + descent + style.stroke_width * 2) * style.line_spacing)
    return best or (font, lines, line_height)


def render_text_image(
    text: str,
    style: TextStyle,
    max_width: int,
    max_height: int,
) -> Image.Image:
    """セリフを描画した透過RGBA画像を返します（余分な余白なし）。

    text が空の場合は 1x1 の完全透過画像を返します。
    """
    if not text.strip():
        return Image.new("RGBA", (1, 1), (0, 0, 0, 0))

    font, lines, line_height = fit_text(text, style, max_width, max_height)
    widths = [_measure(ln, font, style.stroke_width)[0] for ln in lines]
    width = max(max(widths, default=1), 1)
    height = max(line_height * len(lines), 1)

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    for i, line in enumerate(lines):
        x = (width - widths[i]) // 2  # 中央揃え
        y = i * line_height + style.stroke_width
        draw.text(
            (x, y),
            line,
            font=font,
            fill=style.fill,
            stroke_width=style.stroke_width,
            stroke_fill=style.stroke_fill,
            anchor="la",
        )

    return img.crop(img.getbbox() or (0, 0, width, height))
