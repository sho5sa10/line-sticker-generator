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
