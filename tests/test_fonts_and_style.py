"""改行・フォント・おまかせ提案のテスト。APIは呼びません。"""

from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from src import fonts as fontlib
from src import style_suggest as ss
from src.text_renderer import TextStyle, fit_text, split_phrases


# --- 言葉の区切りで改行 ---------------------------------------------------
@pytest.fixture
def style(font_path) -> TextStyle:
    return TextStyle(font_path=font_path, size=60, min_size=26, stroke_width=7, max_lines=3)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("ありがとうございます", ["ありがとう", "ございます"]),
        ("お手数おかけします", ["お手数", "おかけします"]),
        ("よろしくお願いします", ["よろしく", "お願いします"]),
        ("少々お待ちください", ["少々", "お待ちください"]),
        ("了解！", ["了解！"]),
    ],
)
def test_breaks_at_word_boundaries(style, text, expected):
    _, lines, _ = fit_text(text, style, 350, 120)
    assert lines == expected


def test_manual_line_break_is_respected(style):
    _, lines, _ = fit_text("ありがとう\nございます", style, 350, 120)
    assert lines == ["ありがとう", "ございます"]
    # 自分で入れた改行は「最大行数」より優先
    style.max_lines = 1
    _, lines, _ = fit_text("ありがとう\nございます", style, 350, 120)
    assert lines == ["ありがとう", "ございます"]


def test_no_mid_word_break_when_avoidable(style):
    _, lines, _ = fit_text("ちょっと何言ってるかわからない", style, 350, 120)
    assert lines == ["ちょっと", "何言ってるかわからない"]


def test_punctuation_never_starts_a_line():
    for ph in split_phrases("今日は疲れた…！？"):
        assert ph[0] not in "…！？"


def test_falls_back_to_character_wrap_without_budoux(style, monkeypatch):
    from src import text_renderer as tr

    monkeypatch.setattr(tr, "_phrase_parser", lambda: None)
    _, lines, _ = fit_text("ありがとうございます", style, 350, 120)
    assert "".join(lines) == "ありがとうございます"


def test_variable_font_weight_is_applied():
    path = fontlib._find_file("NotoSansJP-VF.ttf")
    if not path:
        pytest.skip("Noto Sans JP が無い環境")
    from src.text_renderer import load_font

    def ink(variation):
        font = load_font(TextStyle(font_path=str(path), variation=variation), 60)
        img = Image.new("L", (400, 100), 0)
        ImageDraw.Draw(img).text((0, 0), "ありがとう", font=font, fill=255)
        return sum(1 for v in img.getdata() if v > 128)

    assert ink("Black") > ink("Regular") * 1.3


# --- フォント一覧 --------------------------------------------------------
def test_available_fonts_include_standard(real_config):
    ids = [f.id for f in fontlib.available_fonts(real_config)]
    if not ids:
        pytest.skip("日本語フォントが無い環境")
    assert "biz-ud-gothic" in ids


def test_custom_fonts_folder(tmp_config, font_path):
    folder = fontlib.custom_font_dir(tmp_config)
    folder.mkdir()
    import shutil

    shutil.copy(font_path, folder / "MyFont.ttc")
    (folder / "readme.txt").write_text("not a font", encoding="utf-8")
    custom = [f for f in fontlib.available_fonts(tmp_config) if "custom" in f.tags]
    assert [f.label for f in custom] == ["MyFont（追加したフォント）"]
    assert fontlib.find_font(tmp_config, "custom:MyFont.ttc") is not None
    assert fontlib.find_font(tmp_config, "custom:../../secret.ttf") is None


# --- おまかせ提案 --------------------------------------------------------
def _bird(tmp_path, body=(255, 215, 67)):
    """黄色い体・焦げ茶の線・オレンジのほっぺ・水色のとさか のダミーキャラ。"""
    img = Image.new("RGBA", (400, 400), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((60, 60, 340, 340), fill=body, outline=(37, 28, 24), width=18)
    d.ellipse((110, 200, 160, 240), fill=(244, 149, 38))
    d.ellipse((240, 200, 290, 240), fill=(244, 149, 38))
    d.polygon([(180, 20), (220, 20), (200, 70)], fill=(1, 192, 187))
    d.rectangle((150, 10, 250, 40), fill=(1, 192, 187))
    p = tmp_path / "bird.png"
    img.save(p)
    return p


def test_analyze_character_colors(tmp_path):
    with Image.open(_bird(tmp_path)) as im:
        pal = ss.analyze_character(im)
    assert ss.color_name(pal["body"]) == "黄"
    assert ss.color_name(pal["outline"]) in ("黒", "焦げ茶")
    names = {ss.color_name(c) for c in pal["accents"]}
    assert "オレンジ" in names and "水色" in names


def test_white_character_uses_white_body(tmp_path):
    with Image.open(_bird(tmp_path, body=(250, 250, 250))) as im:
        pal = ss.analyze_character(im)
    assert ss.color_name(pal["body"]) == "白"


def test_suggestions_are_readable(tmp_config, tmp_path):
    import shutil

    tmp_config.master_image_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(_bird(tmp_path), tmp_config.master_image_path)
    r = ss.suggest_styles(tmp_config)
    names = [s["name"] for s in r["suggestions"]]
    assert "キャラの体の色" in names and "白文字（定番）" in names
    for sg in r["suggestions"]:
        fill = tuple(int(sg["fill"][i:i + 2], 16) for i in (1, 3, 5))
        stroke = tuple(int(sg["stroke_fill"][i:i + 2], 16) for i in (1, 3, 5))
        assert ss.contrast_ratio(fill, stroke) >= 3, sg  # 文字と縁取りがはっきり分かれる
        assert sg["reason"]


@pytest.mark.parametrize(
    "moods,kind,tag",
    [
        (["コミカル", "元気"], "動物", "impact"),
        (["上品"], "人", "elegant"),
        (["かわいい", "ゆるい"], "人", "gentle"),
        (["クール"], "人", "cool"),
        ([], "動物", "gentle"),
        ([], "人", "standard"),
    ],
)
def test_mood_font_tag(moods, kind, tag):
    assert ss.mood_font_tag({"mood": moods, "kind": kind})[0] == tag
