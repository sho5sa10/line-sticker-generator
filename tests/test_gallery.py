"""gallery.html 生成のテスト。APIは呼びません。"""

from __future__ import annotations

from src import image_processor as ip
from src.csv_loader import StickerEntry
from src.gallery import build_gallery
from tests.conftest import make_character


def _entries():
    return [
        StickerEntry(id="001", text="了解！", action="敬礼", expression="笑顔", category="basic"),
        StickerEntry(id="002", text="<script>", action="a", expression="e", category="basic"),
        StickerEntry(id="003", text="未生成", action="a", expression="e", category="basic"),
    ]


def test_gallery_lists_all_entries(tmp_config):
    entries = _entries()
    sticker = ip.compose_sticker(
        make_character(), None, tmp_config.sticker_size, tmp_config.margin
    ).image
    ip.save_png(sticker, tmp_config.dir_final / "001.png")
    ip.save_png(make_character(), tmp_config.dir_generated / "002.png")

    path = build_gallery(tmp_config, entries)
    html = path.read_text(encoding="utf-8")

    assert path.exists()
    assert "final/001.png" in html
    assert "generated/002.png" in html
    assert "未生成" in html
    assert "了解！" in html
    # 拡大表示の仕組みが含まれること
    assert 'id="lightbox"' in html
    assert "data-src=" in html


def test_gallery_escapes_html(tmp_config):
    ip.save_png(make_character(), tmp_config.dir_generated / "002.png")
    path = build_gallery(tmp_config, _entries())
    html = path.read_text(encoding="utf-8")
    assert "<script>" not in html.split("<script>\n  const box")[0].replace(
        "&lt;script&gt;", ""
    )
    assert "&lt;script&gt;" in html


def test_gallery_counts(tmp_config):
    ip.save_png(make_character(), tmp_config.dir_generated / "001.png")
    path = build_gallery(tmp_config, _entries())
    html = path.read_text(encoding="utf-8")
    assert "全 3 件" in html
    assert "画像あり 1 件" in html
    assert "未生成 2 件" in html
