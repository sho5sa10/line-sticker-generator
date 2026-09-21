"""LINE仕様バリデーションのテスト。APIは呼びません。"""

from __future__ import annotations

from PIL import Image

from src import image_processor as ip
from src import validator as vd
from tests.conftest import make_character


def _save(img: Image.Image, path) -> str:
    img.save(path, format="PNG")
    return str(path)


def _valid_sticker(tmp_config):
    return ip.compose_sticker(
        make_character(), None, tmp_config.sticker_size, tmp_config.margin
    ).image


def test_valid_sticker_passes(tmp_config, tmp_path):
    p = tmp_path / "ok.png"
    ip.save_png(_valid_sticker(tmp_config), p, tmp_config.max_file_size_bytes)
    report = vd.validate_sticker(p, tmp_config)
    assert report.ok, [str(i) for i in report.issues]
    assert report.warnings == []


def test_oversized_image_is_error(tmp_config, tmp_path):
    p = tmp_path / "big.png"
    _save(make_character((800, 700)), p)
    report = vd.validate_sticker(p, tmp_config)
    assert any(i.code == "dimensions" for i in report.errors)


def test_odd_dimensions_is_error(tmp_config, tmp_path):
    p = tmp_path / "odd.png"
    _save(make_character((301, 201)), p)
    report = vd.validate_sticker(p, tmp_config)
    assert any(i.code == "odd_dimensions" for i in report.errors)


def test_no_transparency_is_error(tmp_config, tmp_path):
    p = tmp_path / "opaque.png"
    _save(Image.new("RGB", (200, 200), (10, 20, 30)), p)
    report = vd.validate_sticker(p, tmp_config)
    assert any(i.code == "no_transparency" for i in report.errors)


def test_blank_image_is_error(tmp_config, tmp_path):
    p = tmp_path / "blank.png"
    _save(Image.new("RGBA", (200, 200), (0, 0, 0, 0)), p)
    report = vd.validate_sticker(p, tmp_config)
    assert any(i.code == "blank" for i in report.errors)


def test_edge_contact_is_error(tmp_config, tmp_path):
    """コンテンツが画像端に接触している場合。"""
    w, h = tmp_config.sticker_size
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    img.paste((255, 0, 0, 255), (0, 0, w, h // 2))  # 左上端に接触
    p = tmp_path / "edge.png"
    _save(img, p)
    report = vd.validate_sticker(p, tmp_config)
    assert any(i.code == "edge_contact" for i in report.errors)


def test_insufficient_margin_is_warning(tmp_config, tmp_path):
    """端には触れていないが余白が10px未満の場合は WARNING。"""
    w, h = tmp_config.sticker_size
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    img.paste((255, 0, 0, 255), (3, 3, w - 3, h - 3))
    p = tmp_path / "narrow.png"
    _save(img, p)
    report = vd.validate_sticker(p, tmp_config)
    assert report.ok  # エラーではない
    assert any(i.code == "insufficient_margin" for i in report.warnings)
    assert "WARNING: insufficient margin" in report.warnings[0].message


def test_file_size_limit(tmp_config, tmp_path):
    import copy

    from src.config import Config

    raw = copy.deepcopy(tmp_config.raw)
    raw["sticker"]["max_file_size_mb"] = 0.0005  # 約512バイト
    strict = Config(raw=raw, root=tmp_config.root, path=tmp_config.path)

    p = tmp_path / "size.png"
    ip.save_png(_valid_sticker(tmp_config), p)
    report = vd.validate_sticker(p, strict)
    assert any(i.code == "file_size" for i in report.errors)


def test_corrupt_file_is_error(tmp_config, tmp_path):
    p = tmp_path / "corrupt.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"garbage" * 10)
    report = vd.validate_sticker(p, tmp_config)
    assert any(i.code in ("corrupt", "format") for i in report.errors)


def test_missing_file_is_error(tmp_config, tmp_path):
    report = vd.validate_sticker(tmp_path / "nope.png", tmp_config)
    assert any(i.code == "missing" for i in report.errors)


def test_main_and_tab_exact_size(tmp_config, tmp_path):
    main_png = tmp_path / "main.png"
    ip.save_png(ip.fit_on_canvas(make_character(), tmp_config.main_size, 10), main_png)
    assert vd.validate_main(main_png, tmp_config).ok

    tab_png = tmp_path / "tab.png"
    ip.save_png(ip.fit_on_canvas(make_character(), tmp_config.tab_size, 4), tab_png)
    assert vd.validate_tab(tab_png, tmp_config).ok

    wrong = tmp_path / "wrong.png"
    ip.save_png(ip.fit_on_canvas(make_character(), (200, 200), 10), wrong)
    assert not vd.validate_main(wrong, tmp_config).ok


def test_measure_margins():
    img = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    img.paste((255, 0, 0, 255), (12, 8, 90, 70))
    assert vd.measure_margins(img) == (12, 8, 10, 30)
