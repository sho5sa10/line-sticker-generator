"""日本語セリフを透過PNGとして描画します。

画像生成AIには文字を描かせず、ここで Pillow を使って合成します。
LINEの小さい表示でも読めるよう、太字フォント + 太い縁取りを使用します。
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from functools import lru_cache
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
    # 太さを変えられるフォント（Noto Sans JP など）の太さ名。例: "Black"
    variation: str = ""

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
            variation=str(config.get("font.variation", "") or ""),
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
            # プロジェクトのフォルダを移動すると、前の場所の絶対パスが残っていることがあります。
            # 同じ名前のフォントが fonts/ にあれば、それを使います。
            moved = config.root / "fonts" / Path(explicit.replace("\\", "/")).name
            if moved.exists():
                return moved
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
        font = ImageFont.truetype(style.font_path, size=size, index=style.font_index)
    except OSError as exc:
        raise FontNotFoundError(f"フォントを読み込めません: {style.font_path} ({exc})") from exc
    if style.variation:
        try:
            font.set_variation_by_name(style.variation)
        except (OSError, ValueError):
            pass  # 太さを変えられないフォントなら、そのままの太さで描きます
    return font


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


@lru_cache(maxsize=1)
def _phrase_parser():
    """日本語の言葉の区切りを判定するパーサ（BudouX）。無ければ None。"""
    try:
        import budoux
    except ImportError:
        return None
    return budoux.load_default_japanese_parser()


def split_phrases(text: str) -> list[str]:
    """「よろしくお願いします」→「よろしく」「お願いします」のように言葉の区切りで分けます。

    BudouX が使えない環境では1文字ずつに分けます（従来どおりの改行になります）。
    """
    parser = _phrase_parser()
    phrases = parser.parse(text) if parser else list(text)
    # 行頭に来てはいけない文字（「！」「…」など）で始まる区切りは、前の区切りにつなげます。
    merged: list[str] = []
    for ph in phrases:
        if merged and ph and ph[0] in NO_LINE_START:
            merged[-1] += ph
        else:
            merged.append(ph)
    return merged or [text]


def _balanced_lines(
    phrases: list[str],
    n_lines: int,
    font: ImageFont.FreeTypeFont,
    max_width: int,
    stroke_width: int,
) -> list[str] | None:
    """区切りの単位で n_lines 行に分け、行の長さがいちばんそろう分け方を返します。

    どの分け方でも max_width を超える行ができるなら None。
    """
    if n_lines == 1:
        line = "".join(phrases)
        return [line] if _measure(line, font, stroke_width)[0] <= max_width else None
    if n_lines > len(phrases):
        return None
    best: tuple[tuple[int, int], list[str]] | None = None
    for cuts in itertools.combinations(range(1, len(phrases)), n_lines - 1):
        bounds = (0, *cuts, len(phrases))
        lines = ["".join(phrases[a:b]) for a, b in zip(bounds, bounds[1:])]
        widths = [_measure(ln, font, stroke_width)[0] for ln in lines]
        if max(widths) > max_width:
            continue
        # いちばん長い行が短いほど、次に行の長さの差が小さいほど良い
        score = (max(widths), max(widths) - min(widths))
        if best is None or score < best[0]:
            best = (score, lines)
    return best[1] if best else None


def layout_lines(
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
    stroke_width: int,
    max_lines: int,
    *,
    allow_mid_word: bool = True,
) -> list[str] | None:
    """セリフの改行位置を決めます。

    - セリフの中に改行があれば、その位置で改行します（自分で決めた改行を優先）。
    - 無ければ、言葉の区切りで、できるだけ少ない行数・そろった長さに分けます。
    - 1つの言葉が長すぎて収まらないときだけ、文字の途中で改行します。
    """
    if "\n" in text:
        lines: list[str] = []
        for paragraph in text.split("\n"):
            lines += wrap_text(paragraph, font, max_width, stroke_width) if paragraph else [""]
        return lines

    phrases = split_phrases(text)
    for n in range(1, max(max_lines, 1) + 1):
        lines = _balanced_lines(phrases, n, font, max_width, stroke_width)
        if lines:
            return lines
    if not allow_mid_word:
        return None
    return wrap_text(text, font, max_width, stroke_width)


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
    # 自分で改行を入れた場合は、その行数を「最大行数」より優先します。
    max_lines = max(style.max_lines, text.count("\n") + 1)

    # 1回目は言葉の途中で改行しない分け方だけを試し、どの大きさでも収まらなかったときだけ
    # 2回目で文字の途中での改行を許します（「何言って／るか」のような切れ方を避けるため）。
    for allow_mid_word in (False, True):
        size = max(style.size, style.min_size)
        while size >= style.min_size:
            font = load_font(style, size)
            lines = layout_lines(text, font, max_width, style.stroke_width, max_lines,
                                 allow_mid_word=allow_mid_word)
            if lines is None:
                size -= 2
                continue
            ascent, descent = font.getmetrics()
            line_height = int((ascent + descent + style.stroke_width * 2) * style.line_spacing)
            total_h = line_height * len(lines)
            widest = max((_measure(ln, font, style.stroke_width)[0] for ln in lines), default=0)

            if len(lines) <= max_lines and total_h <= max_height and widest <= max_width:
                return font, lines, line_height

            best = (font, lines, line_height)
            size -= 2

    # min_size でも収まらない場合は最小サイズの結果を返します（呼び出し側で縮小されます）。
    font = load_font(style, style.min_size)
    lines = layout_lines(text, font, max_width, style.stroke_width, max_lines)
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
    sw = style.stroke_width

    # 各行の「縁取りを含めた実際のインクの範囲」。縁取りは文字の外側へ広がり、
    # 字形によっては送り幅の外にもはみ出すので、送り幅ではなくこの範囲で配置します。
    # （以前は送り幅で配置していたため、左端の縁取りが数px欠けていました）
    boxes = [font.getbbox(ln, stroke_width=sw, anchor="la") if ln else (0, 0, 0, 0)
             for ln in lines]
    widths = [b[2] - b[0] for b in boxes]
    block_w = max(max(widths, default=1), 1)

    # どの方向にも欠けないよう余白を十分に取って描き、最後に描いた範囲で切り抜きます。
    pad = sw * 2 + style.size // 2
    img = Image.new("RGBA", (block_w + pad * 2, line_height * len(lines) + pad * 2), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    for i, (line, box, w) in enumerate(zip(lines, boxes, widths)):
        if not line:
            continue
        x = pad + (block_w - w) // 2 - box[0]  # インクの範囲で中央揃え
        y = pad + i * line_height + sw
        draw.text(
            (x, y),
            line,
            font=font,
            fill=style.fill,
            stroke_width=sw,
            stroke_fill=style.stroke_fill,
            anchor="la",
        )

    bbox = img.getbbox()
    return img.crop(bbox) if bbox else Image.new("RGBA", (1, 1), (0, 0, 0, 0))
