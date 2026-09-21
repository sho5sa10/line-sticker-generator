"""生成された原画を LINE 規格の透過PNGへ加工します。

重要:
  - 元画像 (output/generated/*.png) は絶対に破壊しません。常にコピーを加工します。
  - キャラクターは上側、セリフは下側の帯に配置するため、文字が顔を隠しません。
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

ALPHA_THRESHOLD = 8  # これ以下のアルファは「透明」とみなします


class ImageProcessingError(Exception):
    """画像加工に失敗した場合に送出されます。"""


@dataclass
class ComposeResult:
    """合成結果と、その過程で出た警告。"""

    image: Image.Image
    warnings: list[str]


# ----------------------------------------------------------------------
# 基本ユーティリティ
# ----------------------------------------------------------------------
def load_rgba(path: str | Path) -> Image.Image:
    """PNGを RGBA として読み込みます（元ファイルは変更しません）。"""
    p = Path(path)
    if not p.exists():
        raise ImageProcessingError(f"画像が見つかりません: {p}")
    try:
        with Image.open(p) as im:
            return im.convert("RGBA")
    except OSError as exc:
        raise ImageProcessingError(f"画像を読み込めません（破損の可能性）: {p} ({exc})") from exc


def has_transparency(img: Image.Image) -> bool:
    """実際に透明ピクセルを含むかどうか。"""
    if img.mode != "RGBA":
        return False
    alpha = img.getchannel("A")
    return alpha.getextrema()[0] <= ALPHA_THRESHOLD


def make_background_transparent(img: Image.Image, tolerance: int = 24) -> Image.Image:
    """背景が不透明な場合に、四隅の色を背景色とみなして透過させます。

    API が透過PNGを返さなかった場合のフォールバックです。
    """
    if has_transparency(img):
        return img.copy()

    rgba = img.convert("RGBA")
    w, h = rgba.size
    corners = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]
    samples = [rgba.getpixel(c)[:3] for c in corners]
    # 四隅が概ね同色でなければ背景判定できないので、そのまま返します。
    base = samples[0]
    if any(max(abs(a - b) for a, b in zip(base, s)) > tolerance for s in samples[1:]):
        return rgba

    pixels = rgba.load()
    for y in range(h):
        for x in range(w):
            r, g, b, a = pixels[x, y]
            if max(abs(r - base[0]), abs(g - base[1]), abs(b - base[2])) <= tolerance:
                pixels[x, y] = (r, g, b, 0)
    return rgba


def is_blank(img: Image.Image) -> bool:
    """不透明なピクセルが1つも無い（＝中身が空の）画像かどうか。"""
    if img.mode != "RGBA":
        return False
    return img.getchannel("A").getextrema()[1] <= ALPHA_THRESHOLD


def trim_transparent(img: Image.Image) -> Image.Image:
    """透明な余白を切り落とします。中身が空なら元画像をそのまま返します。"""
    if img.mode != "RGBA":
        return img.copy()
    alpha = img.getchannel("A")
    bbox = alpha.point(lambda v: 255 if v > ALPHA_THRESHOLD else 0).getbbox()
    if not bbox:
        return img.copy()
    return img.crop(bbox)


def fit_within(img: Image.Image, max_w: int, max_h: int) -> Image.Image:
    """アスペクト比を保ったまま指定枠に収まるよう拡大/縮小します。"""
    if img.width <= 0 or img.height <= 0 or max_w <= 0 or max_h <= 0:
        raise ImageProcessingError("リサイズ先のサイズが不正です")
    scale = min(max_w / img.width, max_h / img.height)
    new_w = max(1, int(round(img.width * scale)))
    new_h = max(1, int(round(img.height * scale)))
    return img.resize((new_w, new_h), Image.LANCZOS)


def to_even(value: int) -> int:
    """LINE仕様「縦横は偶数」を満たすよう偶数に丸めます。"""
    return value if value % 2 == 0 else value - 1


# ----------------------------------------------------------------------
# スタンプ合成
# ----------------------------------------------------------------------
def compose_sticker(
    character: Image.Image,
    text_image: Image.Image | None,
    canvas_size: tuple[int, int],
    margin: int,
    gap: int = 4,
    text_position: str = "bottom",
) -> ComposeResult:
    """キャラクターとセリフを1枚の透過スタンプに合成します。

    キャラクターとテキストは互いに重ならない領域に配置されるため、
    セリフが顔や重要なポーズを隠すことはありません。
    """
    canvas_w, canvas_h = canvas_size
    warnings: list[str] = []
    canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))

    if not is_blank(character):
        char = trim_transparent(character)
    else:
        raise ImageProcessingError("キャラクター画像が空です（全ピクセルが透明）")
    if char.width <= 1 or char.height <= 1:
        raise ImageProcessingError("キャラクター画像が小さすぎます")

    inner_w = canvas_w - margin * 2
    inner_h = canvas_h - margin * 2
    if inner_w <= 0 or inner_h <= 0:
        raise ImageProcessingError("余白が大きすぎて描画領域がありません")

    text_h = 0
    text = None
    if text_image is not None and text_image.width > 1 and text_image.height > 1:
        text = text_image
        if text.width > inner_w:
            text = fit_within(text, inner_w, text.height)
            warnings.append("テキストが幅に収まらないため縮小しました")
        text_h = text.height + gap

    char_area_h = inner_h - text_h
    if char_area_h < 20:
        raise ImageProcessingError("テキスト帯が大きすぎてキャラクター領域が残りません")

    char_fitted = fit_within(char, inner_w, char_area_h)

    if text_position == "top":
        text_y = margin
        char_y = margin + text_h + (char_area_h - char_fitted.height) // 2
    else:
        char_y = margin + (char_area_h - char_fitted.height) // 2
        text_y = canvas_h - margin - (text.height if text else 0)

    char_x = (canvas_w - char_fitted.width) // 2
    canvas.alpha_composite(char_fitted, (char_x, char_y))

    if text is not None:
        text_x = (canvas_w - text.width) // 2
        canvas.alpha_composite(text, (text_x, max(text_y, 0)))

    return ComposeResult(image=canvas, warnings=warnings)


def fit_on_canvas(
    character: Image.Image,
    canvas_size: tuple[int, int],
    margin: int,
) -> Image.Image:
    """キャラクターのみを指定キャンバスの中央に配置します（main/tab用）。"""
    canvas_w, canvas_h = canvas_size
    canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    char = trim_transparent(character)
    fitted = fit_within(char, canvas_w - margin * 2, canvas_h - margin * 2)
    canvas.alpha_composite(
        fitted,
        ((canvas_w - fitted.width) // 2, (canvas_h - fitted.height) // 2),
    )
    return canvas


# ----------------------------------------------------------------------
# 保存
# ----------------------------------------------------------------------
def _png_bytes(img: Image.Image, *, optimize: bool = True) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=optimize)
    return buf.getvalue()


def save_png(
    img: Image.Image,
    path: str | Path,
    max_bytes: int | None = None,
) -> tuple[Path, int, list[str]]:
    """透過PNGとして保存します。容量超過時は自動的に減色して収めます。

    Returns:
        (保存先, バイト数, 警告リスト)
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []

    rgba = img if img.mode == "RGBA" else img.convert("RGBA")
    data = _png_bytes(rgba)

    if max_bytes is not None and len(data) > max_bytes:
        for colors in (256, 128, 64, 32):
            quantized = rgba.quantize(colors=colors, method=Image.FASTOCTREE).convert("RGBA")
            candidate = _png_bytes(quantized)
            if len(candidate) <= max_bytes:
                data = candidate
                warnings.append(f"容量超過のため{colors}色に減色しました")
                break
        else:
            warnings.append(
                f"WARNING: file size {len(data)} bytes exceeds limit {max_bytes} bytes"
            )

    p.write_bytes(data)
    return p, len(data), warnings
