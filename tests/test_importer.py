"""手持ち画像の取り込みのテスト。APIは呼びません。"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from src import importer
from src.csv_loader import StickerEntry
from src.text_renderer import TextStyle
from tests.conftest import make_character


def _png(img: Image.Image, fmt: str = "PNG") -> bytes:
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def _entry(sid="001", text="了解！"):
    return StickerEntry(id=sid, text=text, action="敬礼", expression="笑顔", category="basic")


@pytest.mark.parametrize(
    "name,expected",
    [
        ("001.png", 1),
        ("12.jpg", 12),
        ("sticker_035.png", 35),
        ("100_final.webp", 100),
        ("no-number.png", None),
        ("01_02.png", None),  # 数字が2か所あると曖昧なので取り込まない
    ],
)
def test_id_from_filename(name, expected):
    assert importer.id_from_filename(name) == expected


def test_load_rejects_non_image():
    with pytest.raises(importer.ImportError_):
        importer.load_image_bytes(b"hello")


def test_load_rejects_empty():
    with pytest.raises(importer.ImportError_):
        importer.load_image_bytes(b"")


def test_load_rejects_blank():
    with pytest.raises(importer.ImportError_):
        importer.load_image_bytes(_png(Image.new("RGBA", (50, 50), (0, 0, 0, 0))))


def test_load_makes_opaque_background_transparent():
    img = Image.new("RGB", (200, 200), (255, 255, 255))
    img.paste((200, 30, 30), (60, 60, 140, 140))
    rgba = importer.load_image_bytes(_png(img, "JPEG"))
    assert rgba.mode == "RGBA"
    assert rgba.getpixel((0, 0))[3] == 0


def test_load_downscales_huge_images():
    rgba = importer.load_image_bytes(_png(make_character((3000, 2000))))
    assert max(rgba.size) == importer.MAX_SIDE


def test_import_creates_raw_and_final(tmp_config):
    style = TextStyle.from_config(tmp_config)
    r = importer.import_image(tmp_config, _entry(), _png(make_character()), style)
    assert r.ok, r.issues
    assert r.archived is None
    assert (tmp_config.dir_generated / "001.png").exists()
    assert (tmp_config.dir_final / "001.png").exists()
    with Image.open(tmp_config.dir_final / "001.png") as im:
        assert im.size == tuple(tmp_config.sticker_size)


def test_import_archives_instead_of_overwriting(tmp_config):
    style = TextStyle.from_config(tmp_config)
    importer.import_image(tmp_config, _entry(), _png(make_character()), style)
    first = (tmp_config.dir_generated / "001.png").read_bytes()

    r = importer.import_image(
        tmp_config, _entry(), _png(make_character(color=(20, 180, 60, 255))), style
    )
    assert r.archived
    archived = importer.archive_dir(tmp_config) / r.archived
    assert archived.read_bytes() == first  # 前の画像がそのまま残っている
    # 退避先は generated/ の外（main/tab の自動選択などに混ざらない）
    assert archived.parent != tmp_config.dir_generated


def test_import_failure_keeps_existing_image(tmp_config):
    style = TextStyle.from_config(tmp_config)
    importer.import_image(tmp_config, _entry(), _png(make_character()), style)
    before = (tmp_config.dir_generated / "001.png").read_bytes()

    r = importer.import_image(tmp_config, _entry(), b"broken", style)
    assert not r.ok
    assert (tmp_config.dir_generated / "001.png").read_bytes() == before


def test_import_logs_without_api(tmp_config):
    from src.logger import RunLogger, StateStore

    style = TextStyle.from_config(tmp_config)
    logger = RunLogger(tmp_config.log_path, echo=False)
    state = StateStore(tmp_config.state_path)
    importer.import_image(tmp_config, _entry("007"), _png(make_character()), style, logger, state)

    log = tmp_config.log_path.read_text(encoding="utf-8")
    assert "007 IMPORTED - API was not called" in log
    assert "007 API REQUEST" not in log
    assert state.status("007") == "imported"
