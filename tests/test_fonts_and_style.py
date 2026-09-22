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


# --- フォントを増やす（ダウンロードは差し替えて、実際には通信しない） -----
def _fake_http(font_path):
    """一覧のURLに対して、手元のフォントとライセンス文を返すダミー。"""
    data = open(font_path, "rb").read()

    def get(url, timeout):
        assert url.startswith(fontlib.GOOGLE_FONTS_BASE)  # 一覧のURL以外は取りに行かない
        return b"SIL OPEN FONT LICENSE" if url.endswith("OFL.txt") else data
    return get


def test_install_free_font(tmp_config, font_path):
    path = fontlib.install_free_font(tmp_config, "hachi-maru-pop", http_get=_fake_http(font_path))
    assert path.name == "HachiMaruPop-Regular.ttf"
    assert (path.parent / "HachiMaruPop-Regular-OFL.txt").read_bytes().startswith(b"SIL")
    info = fontlib.find_font(tmp_config, "hachi-maru-pop")
    assert info is not None
    assert info.label == "はちまるポップ（手書き）"
    assert "handwritten" in info.tags


def test_install_rejects_unknown_font(tmp_config):
    with pytest.raises(fontlib.FontInstallError, match="一覧にない"):
        fontlib.install_free_font(tmp_config, "../../evil", http_get=lambda u, t: b"")


def test_install_rejects_broken_download(tmp_config):
    with pytest.raises(fontlib.FontInstallError, match="フォントとして読み込めません"):
        fontlib.install_free_font(tmp_config, "yusei-magic", http_get=lambda u, t: b"<html>not a font</html>")
    # 壊れたファイルを残さない
    assert not list(fontlib.custom_font_dir(tmp_config).glob("*.ttf"))
    assert not list(fontlib.custom_font_dir(tmp_config).glob("*.part"))


def test_install_reports_network_error(tmp_config):
    def boom(url, timeout):
        raise OSError("network down")
    with pytest.raises(fontlib.FontInstallError, match="ダウンロードできません"):
        fontlib.install_free_font(tmp_config, "yomogi", http_get=boom)


def test_every_free_font_is_on_google_fonts_ofl():
    for f in fontlib.FREE_FONTS:
        assert f.url.startswith("https://raw.githubusercontent.com/google/fonts/main/ofl/")
        assert f.license_url.endswith("/OFL.txt")
        assert f.category in fontlib.FREE_FONT_CATEGORIES
    assert sum(1 for f in fontlib.FREE_FONTS if f.category == "手書き") >= 5


def test_suggestion_prefers_added_handwritten_font(tmp_config, font_path, tmp_path):
    import shutil

    from src import character_profile as cp

    fontlib.install_free_font(tmp_config, "hachi-maru-pop", http_get=_fake_http(font_path))
    cp.save_profile(tmp_config.character_profile_path, cp.PRESETS["ゆるうさぎ"])
    tmp_config.master_image_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(_bird(tmp_path), tmp_config.master_image_path)
    first = ss.suggest_styles(tmp_config)["suggestions"][0]
    assert first["font_id"] == "hachi-maru-pop"


def test_missing_chars(font_path):
    assert fontlib.missing_chars(font_path, ["了解！ありがとう"]) == []
    arial = fontlib._find_file("arial.ttf")
    if arial:
        assert set(fontlib.missing_chars(str(arial), ["了解！OK"])) == {"了", "解", "！"}


def test_pick_font_skips_font_without_kanji(tmp_config, font_path):
    # だるまドロップ（漢字なし）は「元気」系に合うフォントだが、提案には選ばない
    fontlib.install_free_font(tmp_config, "darumadrop-one", http_get=_fake_http(font_path))
    fonts = fontlib.available_fonts(tmp_config)
    assert any(f.id == "darumadrop-one" for f in fonts)
    picked = ss._pick_font(fonts, "impact")
    assert picked is not None and picked.id != "darumadrop-one"


def test_pick_font_skips_font_missing_sticker_chars(font_path):
    from src.fonts import FontInfo

    arial = fontlib._find_file("arial.ttf")
    if not arial:
        pytest.skip("arial.ttf がありません")
    no_jp = FontInfo("a", "A", str(arial), 0, "", ("gentle",), "")
    jp = FontInfo("b", "B", font_path, 0, "", ("gentle",), "")
    assert ss._pick_font([no_jp, jp], "gentle", ["了解！"]).id == "b"


# --- プロジェクトのフォルダを移動しても動く ---------------------------------
def test_portable_path_is_relative_inside_project(tmp_config):
    inside = tmp_config.root / "fonts" / "A.ttf"
    assert fontlib.portable_path(tmp_config, str(inside)) == "fonts/A.ttf"
    outside = r"C:\Windows\Fonts\meiryo.ttc"
    assert fontlib.portable_path(tmp_config, outside) == outside


def test_old_absolute_font_path_falls_back_to_fonts_folder(tmp_config, font_path):
    import shutil

    from src.text_renderer import resolve_font_path

    (tmp_config.root / "fonts").mkdir(exist_ok=True)
    shutil.copy(font_path, tmp_config.root / "fonts" / "Moved.ttf")
    tmp_config.raw["font"]["path"] = r"D:\old-place\project\fonts\Moved.ttf"
    assert resolve_font_path(tmp_config) == tmp_config.root / "fonts" / "Moved.ttf"
