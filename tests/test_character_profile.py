"""かんたん入力（選択肢 → 日本語の説明文）のテスト。"""

from __future__ import annotations

import pytest

from src import character_profile as cp
from src.prompt_generator import DEFAULT_CHARACTER_JA


def test_office_worker_preset_reproduces_bundled_text():
    """「会社員（男性）」は同梱の説明文と一字一句同じになる（既存キャラが変わらない）。"""
    assert cp.compose(cp.PRESETS["会社員（男性）"]) == DEFAULT_CHARACTER_JA


@pytest.mark.parametrize("name", list(cp.PRESETS))
def test_every_preset_composes_clean_text(name):
    text = cp.compose(cp.PRESETS[name])
    lines = text.splitlines()
    assert len(lines) >= 4
    assert all(line.endswith("。") for line in lines), lines
    assert "None" not in text
    assert "。。" not in text


def test_presets_only_use_defined_options():
    """ひな形が選択肢に無い値を使っていると、画面で選択状態を再現できません。"""
    for name, preset in cp.PRESETS.items():
        assert cp.normalize(preset) == preset, name


def test_animal_hides_human_only_fields():
    profile = {"kind": "動物", "animal": "ねこ", "gender": "男性", "hair": "ボブ", "job": "会社員"}
    norm = cp.normalize(profile)
    assert norm == {"kind": "動物", "animal": "ねこ"}
    text = cp.compose(profile)
    assert text.startswith("ねこのキャラクター。")
    assert "ボブ" not in text and "会社員" not in text


def test_human_hides_animal_field():
    assert "animal" not in cp.normalize({"kind": "人", "animal": "ねこ"})


def test_unknown_values_are_dropped():
    norm = cp.normalize({"kind": "宇宙人", "heads": "10頭身", "mood": ["元気", "<script>"]})
    # kind と heads は不正値なので消え、mood は正しい値「元気」だけが残る
    assert norm == {"mood": ["元気"]}


def test_multi_select_order_is_stable():
    a = cp.compose({"kind": "人", "mood": ["コミカル", "親しみやすい"]})
    b = cp.compose({"kind": "人", "mood": ["親しみやすい", "コミカル"]})
    assert a == b
    assert "親しみやすく、コミカルな雰囲気。" in a


@pytest.mark.parametrize(
    "profile,expected",
    [
        ({"kind": "人（日本人）", "gender": "女性", "age": "20代"}, "20代くらいの日本人女性。"),
        ({"kind": "人（日本人）", "age": "30代", "job": "会社員"}, "30代くらいの日本人の会社員。"),
        ({"kind": "人", "gender": "男性", "age": "子ども"}, "男の子。"),
        ({"kind": "人（日本人）", "gender": "女性", "age": "シニア"}, "日本人のおばあさん。"),
        ({"kind": "人"}, "人のキャラクター。"),
        ({"kind": "動物"}, "動物のキャラクター。"),
    ],
)
def test_person_line(profile, expected):
    assert cp.compose(profile).splitlines()[0] == expected


@pytest.mark.parametrize(
    "hair,color,expected",
    [
        ("短髪", "黒", "短い黒髪。"),
        ("短髪", "茶", "短い茶色の髪。"),
        ("短髪", None, "短い髪。"),
        ("ボブ", "茶", "茶色のボブヘア。"),
        ("ロング", None, "ロングヘア。"),
        (None, "黒", "黒髪。"),
        (None, "金", "金色の髪。"),
    ],
)
def test_hair_line(hair, color, expected):
    profile = {"kind": "人"}
    if hair:
        profile["hair"] = hair
    if color:
        profile["hair_color"] = color
    assert expected in cp.compose(profile).splitlines()


def test_outfit_lines():
    assert "白いワイシャツ。\nシンプルな濃い色のネクタイ。" in cp.compose(
        {"kind": "人", "outfit": "ワイシャツとネクタイ", "outfit_color": "白"})
    assert "黄色いパーカー。" in cp.compose({"kind": "人", "outfit": "パーカー", "outfit_color": "黄"})
    # 「白い白衣」のような重複を避ける
    assert "白衣。" in cp.compose({"kind": "人", "outfit": "白衣", "outfit_color": "白"}).splitlines()


def test_extra_text_is_appended_and_limited():
    text = cp.compose({"kind": "人", "extra": "左目の下にほくろ。\n\n  いつも笑顔。 "})
    assert text.splitlines()[-2:] == ["左目の下にほくろ。", "いつも笑顔。"]
    long = cp.normalize({"extra": "あ" * 2000})
    assert len(long["extra"]) == cp.EXTRA_MAX_CHARS


def test_save_and_load_profile(tmp_path):
    path = tmp_path / "profile.json"
    cp.save_profile(path, {**cp.PRESETS["ねこ"], "bogus": "x"})
    assert cp.load_profile(path) == cp.PRESETS["ねこ"]
    # 日本語がそのまま読める形で保存される
    assert "ねこ" in path.read_text(encoding="utf-8")


def test_load_profile_missing_or_broken(tmp_path):
    assert cp.load_profile(tmp_path / "none.json") is None
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert cp.load_profile(broken) is None


def test_infer_profile_from_text():
    assert cp.infer_profile(DEFAULT_CHARACTER_JA) == cp.PRESETS["会社員（男性）"]
    assert cp.infer_profile("自由に書いた説明。") is None
