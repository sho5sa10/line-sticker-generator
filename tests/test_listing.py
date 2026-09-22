"""申請用のタイトル・説明文（src/listing.py）のテスト。"""

from __future__ import annotations

import re

import pytest

from src import character_profile as cp
from src import listing as L
from src.csv_loader import StickerEntry


def _entries(texts_cats):
    return [StickerEntry(id=f"{i + 1:03d}", text=t, action="a", expression="e", category=c)
            for i, (t, c) in enumerate(texts_cats)]


WORK = _entries([
    ("了解！", "basic"), ("了解です", "basic"), ("ありがとう！", "thanks"),
    ("すみません！", "apology"), ("確認しました", "work"), ("対応します", "work"),
    ("お疲れさまです", "greeting"), ("少々お待ちください", "work"), ("OKです！", "reply"),
])
CASUAL = _entries([
    ("おはよう！", "greeting"), ("おやすみ", "greeting"), ("やったー！", "joy"),
    ("ありがとう", "thanks"), ("ごめん！", "apology"), ("すごい！", "joy"),
])


def test_display_width_counts_fullwidth_as_two():
    assert L.display_width("abc") == 3
    assert L.display_width("ことり") == 6
    assert L.display_width("OK！") == 4


@pytest.mark.parametrize("preset", list(cp.PRESETS))
@pytest.mark.parametrize("entries", [WORK, CASUAL], ids=["work", "casual"])
def test_suggestions_pass_all_checks(preset, entries):
    cands = L.suggest(cp.PRESETS[preset], entries, creator="yourname", year=2026)
    assert len(cands) >= 2
    for c in cands:
        assert all(not issues for issues in L.check_listing(c).values()), (preset, c)
        assert c["title_en"].isascii() and c["desc_en"].isascii()
        assert c["copyright"] == "(C)2026 yourname"


def test_suggestion_uses_character_and_phrases():
    c = L.suggest({"kind": "動物", "animal": "ことり", "fur": "黄", "mood": ["コミカル"]}, WORK)[0]
    assert "ことり" in c["title_ja"] and "Bird" in c["title_en"]
    assert "「了解です」" in c["desc_ja"]      # 敬語が多いセットなので敬語の例
    assert "「了解！」" not in c["desc_ja"]    # 似たセリフは1つだけ
    assert "敬語" in c["title_ja"]
    assert "Little Yellow Bird" in c["title_en"] or "Little Bird" in c["title_en"]


def test_casual_phrases_get_everyday_theme():
    c = L.suggest(cp.PRESETS["ゆるうさぎ"], CASUAL)[0]
    assert "敬語" not in c["title_ja"]
    assert "friends" in c["desc_en"]


def test_without_profile_still_suggests():
    c = L.suggest({}, WORK)[0]
    assert "キャラクター" in c["title_ja"]


def test_japanese_creator_leaves_copyright_blank():
    c = L.suggest({}, WORK, creator="しょう")[0]
    assert c["creator"] == "しょう" and c["copyright"] == ""


@pytest.mark.parametrize(
    "key,text,expect",
    [
        ("title_en", "", "必須"),
        ("title_ja", "", None),
        ("title_en", "x" * 41, "長すぎ"),
        ("title_ja", "あ" * 21, "長すぎ"),
        ("title_ja", "あ" * 20, None),
        ("title_en", "Cute Cat \U0001F431", "絵文字"),
        ("desc_en", "かわいい cat", "半角"),
        ("copyright", "(C)2026 しょう", "半角"),
        ("title_en", "Cat for LINE", "LINE"),
        ("title_ja", "ラインで使えるねこ", "LINE"),
        ("title_ja", "オンラインで使えるねこ", None),
        ("desc_ja", "10月1日発売のスタンプ", "発売"),
        ("desc_en", "See https://example.com", "URL"),
    ],
)
def test_check_field(key, text, expect):
    issues = L.check_field(key, text)
    if expect is None:
        assert issues == []
    else:
        assert any(expect in i for i in issues), issues


def test_save_and_load(tmp_config):
    L.save_listing(tmp_config, {"creator": " name ", "title_en": "Cat"})
    got = L.load_listing(tmp_config)
    assert got["creator"] == "name" and got["title_en"] == "Cat" and got["desc_ja"] == ""


def test_volume_number_is_added_to_titles():
    c = L.suggest(cp.PRESETS["ゆるうさぎ"], CASUAL, volume=2)[0]
    assert c["title_ja"].endswith("2") and c["title_en"].endswith(" 2")
    assert not L.check_field("title_ja", c["title_ja"]) and not L.check_field("title_en", c["title_en"])


def test_mixed_set_shows_polite_and_casual_examples():
    mixed = _entries([
        ("了解です", "basic"), ("了解！", "basic"), ("助かります！", "thanks"),
        ("やったー！", "joy"), ("えっ！？", "surprise"), ("おはよう！", "greeting"),
        ("眠い…", "tired"), ("すごい！", "joy"),
    ])
    c = L.suggest({}, mixed)[0]
    assert "敬語もタメ口も" in c["desc_ja"]
    quoted = re.findall(r"「(.+?)」", c["desc_ja"])
    assert any(L._is_polite(q) for q in quoted) and any(not L._is_polite(q) for q in quoted)
    assert not ("了解です" in quoted and "了解！" in quoted)
