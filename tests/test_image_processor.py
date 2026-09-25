"""画像加工（サイズ・透過・余白・容量）のテスト。APIは呼びません。"""

from __future__ import annotations

import pytest
from PIL import Image

from src import image_processor as ip
from tests.conftest import make_character


def test_trim_transparent_removes_padding():
    img = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    img.paste((255, 0, 0, 255), (50, 60, 90, 120))
    trimmed = ip.trim_transparent(img)
    assert trimmed.size == (40, 60)


def test_trim_keeps_original_untouched():
    img = make_character((100, 100))
    before = img.tobytes()
    ip.trim_transparent(img)
    assert img.tobytes() == before  # 元画像は破壊されない


def test_fit_within_preserves_aspect_ratio():
    img = Image.new("RGBA", (400, 200), (255, 0, 0, 255))
    fitted = ip.fit_within(img, 100, 100)
    assert fitted.size == (100, 50)


def test_has_transparency():
    assert ip.has_transparency(Image.new("RGBA", (10, 10), (0, 0, 0, 0)))
    assert not ip.has_transparency(Image.new("RGBA", (10, 10), (1, 2, 3, 255)))


def test_make_background_transparent_on_opaque_image():
    img = Image.new("RGBA", (60, 60), (255, 255, 255, 255))
    img.paste((10, 20, 30, 255), (20, 20, 40, 40))
    out = ip.make_background_transparent(img)
    assert ip.has_transparency(out)
    assert out.getpixel((0, 0))[3] == 0      # 背景は透過
    assert out.getpixel((30, 30))[3] == 255  # 中身は残る


def test_to_even():
    assert ip.to_even(370) == 370
    assert ip.to_even(371) == 370


# --- 合成 ---------------------------------------------------------------
def test_compose_sticker_size_and_margin(tmp_config):
    canvas_w, canvas_h = tmp_config.sticker_size
    margin = tmp_config.margin
    result = ip.compose_sticker(make_character(), None, (canvas_w, canvas_h), margin)

    assert result.image.size == (canvas_w, canvas_h)
    assert result.image.mode == "RGBA"

    bbox = result.image.getchannel("A").point(lambda v: 255 if v > 8 else 0).getbbox()
    left, top, right, bottom = bbox
    assert left >= margin
    assert top >= margin
    assert canvas_w - right >= margin
    assert canvas_h - bottom >= margin


def test_compose_sticker_rejects_blank_character(tmp_config):
    blank = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    with pytest.raises(ip.ImageProcessingError, match="空"):
        ip.compose_sticker(blank, None, tmp_config.sticker_size, tmp_config.margin)


def test_fit_on_canvas_exact_size():
    out = ip.fit_on_canvas(make_character(), (240, 240), 10)
    assert out.size == (240, 240)
    bbox = out.getchannel("A").point(lambda v: 255 if v > 8 else 0).getbbox()
    assert bbox is not None
    assert min(bbox[0], bbox[1], 240 - bbox[2], 240 - bbox[3]) >= 10


# --- 保存 / 容量 --------------------------------------------------------
def test_save_png_creates_transparent_file(tmp_path):
    out = tmp_path / "sub" / "a.png"
    path, size_bytes, warnings = ip.save_png(make_character((370, 320)), out)
    assert path.exists()
    assert size_bytes > 0
    assert warnings == []
    with Image.open(path) as im:
        assert im.format == "PNG"
        assert im.mode == "RGBA"


def test_save_png_reduces_when_over_limit(tmp_path):
    # ノイズ画像は圧縮が効かないため、わざと小さい上限を課して減色を発動させる
    import random

    random.seed(0)
    img = Image.new("RGBA", (370, 320))
    img.putdata([
        (random.randrange(256), random.randrange(256), random.randrange(256), 255)
        for _ in range(370 * 320)
    ])
    limit = 40 * 1024
    path, size_bytes, warnings = ip.save_png(img, tmp_path / "big.png", limit)
    assert path.exists()
    assert warnings, "容量超過時には警告か減色が記録されるはず"
    if not any("exceeds limit" in w for w in warnings):
        assert size_bytes <= limit


def test_load_rgba_missing_file(tmp_path):
    with pytest.raises(ip.ImageProcessingError):
        ip.load_rgba(tmp_path / "nope.png")


# --- 体が画像の端で切れていないか ---------------------------------------------
def test_cropped_sides_detects_body_cut_off_at_bottom():
    from PIL import Image, ImageDraw

    from src.image_processor import cropped_sides

    ok = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    ImageDraw.Draw(ok).ellipse((40, 30, 160, 170), fill=(255, 200, 0, 255))
    assert cropped_sides(ok) == []

    cut = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    ImageDraw.Draw(cut).rectangle((50, 20, 150, 199), fill=(255, 200, 0, 255))  # 下の端まで体がある
    assert cropped_sides(cut) == ["bottom"]

    opaque = Image.new("RGBA", (200, 200), (255, 255, 255, 255))  # 透過なし＝判定しない
    assert cropped_sides(opaque) == []
