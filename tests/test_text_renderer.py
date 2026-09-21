"""日本語テキスト描画のテスト。APIは呼びません。"""

from __future__ import annotations

import pytest
from PIL import Image

from src.text_renderer import (
    FontNotFoundError,
    TextStyle,
    load_font,
    render_text_image,
    resolve_font_path,
    wrap_text,
)


@pytest.fixture
def style(font_path) -> TextStyle:
    return TextStyle(font_path=font_path, size=60, min_size=20, stroke_width=6)


def test_resolve_font_path_autodetect(real_config):
    p = resolve_font_path(real_config)
    assert p.exists()


def test_resolve_font_path_missing_explicit(real_config):
    import copy

    from src.config import Config

    raw = copy.deepcopy(real_config.raw)
    raw["font"]["path"] = "no/such/font.ttf"
    cfg = Config(raw=raw, root=real_config.root, path=real_config.path)
    with pytest.raises(FontNotFoundError):
        resolve_font_path(cfg)


def test_render_short_text(style):
    img = render_text_image("了解！", style, 350, 120)
    assert img.mode == "RGBA"
    assert img.width > 0 and img.height > 0
    assert img.getchannel("A").getextrema()[1] == 255  # 不透明な文字が存在する


def test_render_empty_text_returns_blank(style):
    img = render_text_image("", style, 350, 120)
    assert img.size == (1, 1)
    assert img.getchannel("A").getextrema()[1] == 0


def test_long_text_is_wrapped(style):
    font = load_font(style, 40)
    lines = wrap_text("ちょっと何言ってるかわからない", font, 200, style.stroke_width)
    assert len(lines) >= 2
    assert "".join(lines) == "ちょっと何言ってるかわからない"


def test_wrap_respects_max_width(style):
    font = load_font(style, 40)
    max_width = 200
    for line in wrap_text("ちょっと何言ってるかわからない", font, max_width, 0):
        assert font.getlength(line) <= max_width + font.size  # 禁則によるぶら下げ分を許容


def test_kinsoku_no_line_start(style):
    font = load_font(style, 40)
    # 「。」が行頭に来ないこと
    lines = wrap_text("あああああああああ。あああ", font, int(font.getlength("あああ")), 0)
    assert all(not ln.startswith("。") for ln in lines)


def test_render_fits_within_box(style):
    max_w, max_h = 350, 120
    img = render_text_image("ちょっと何言ってるかわからない", style, max_w, max_h)
    assert img.width <= max_w
    assert img.height <= max_h + style.size  # 縮小しきれない場合の許容


def test_explicit_newline_is_respected(style):
    font = load_font(style, 40)
    lines = wrap_text("あ\nい", font, 1000, 0)
    assert lines == ["あ", "い"]


def test_text_is_composited_onto_sticker(tmp_config, style, dummy_character):
    from src import image_processor as ip

    text_img = render_text_image("了解！", style, 350, 120)
    result = ip.compose_sticker(
        dummy_character, text_img, tmp_config.sticker_size, tmp_config.margin
    )
    assert result.image.size == tmp_config.sticker_size

    # 文字がキャンバス内に収まっていること
    bbox = result.image.getchannel("A").point(lambda v: 255 if v > 8 else 0).getbbox()
    assert bbox[0] >= 0 and bbox[1] >= 0
    assert bbox[2] <= tmp_config.sticker_size[0]
    assert bbox[3] <= tmp_config.sticker_size[1]


def test_style_from_config(real_config):
    s = TextStyle.from_config(real_config)
    assert s.stroke_width >= 1
    assert s.size >= s.min_size
    assert Image  # Pillow が使えること


# --- 縁取りが欠けないこと -------------------------------------------------
def _reference_ink_size(text, style, max_w, max_h):
    """十分に大きいキャンバスに描いたときの、縁取りを含むインクの大きさ（正解）。"""
    from PIL import ImageDraw

    from src.text_renderer import fit_text

    font, lines, lh = fit_text(text, style, max_w, max_h)
    pad = 200
    img = Image.new("RGBA", (2000, lh * len(lines) + pad * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for i, ln in enumerate(lines):
        d.text((pad, pad + i * lh), ln, font=font, fill="#fff",
               stroke_width=style.stroke_width, stroke_fill="#000", anchor="la")
    widest = max(
        (lambda b: b[2] - b[0])(img.crop((0, pad + i * lh - style.stroke_width - 60,
                                          2000, pad + (i + 1) * lh + 60)).getbbox() or (0, 0, 0, 0))
        for i in range(len(lines))
    )
    return widest, lines


def test_stroke_is_never_clipped_for_any_bundled_text(real_config, font_path):
    """同梱の100件すべてで、縁取りが画像の外に切れていないこと。

    以前は送り幅で配置していたため、左端の縁取りが2〜4px欠けていました。
    """
    from src.csv_loader import load_stickers

    style = TextStyle(font_path=font_path, size=60, min_size=26, stroke_width=7)
    w, h = real_config.sticker_size
    m = real_config.margin
    max_w, max_h = w - m * 2, int((h - m * 2) * 0.40)
    clipped = []
    for e in load_stickers(real_config.csv_path):
        img = render_text_image(e.text, style, max_w, max_h)
        expected_w, _ = _reference_ink_size(e.text, style, max_w, max_h)
        if img.width < expected_w:
            clipped.append((e.id, e.text, expected_w - img.width))
    assert clipped == [], clipped


def test_thick_stroke_is_not_clipped(font_path):
    style = TextStyle(font_path=font_path, size=60, min_size=20, stroke_width=16)
    img = render_text_image("了解！", style, 350, 120)
    expected_w, _ = _reference_ink_size("了解！", style, 350, 120)
    assert img.width >= expected_w
    # 上下も欠けない: 縁取りの上端・下端の行にインクがあり、それより外は無い
    a = img.getchannel("A")
    assert a.crop((0, 0, img.width, 1)).getextrema()[1] > 0
    assert a.crop((0, img.height - 1, img.width, img.height)).getextrema()[1] > 0
