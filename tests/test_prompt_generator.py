"""プロンプト生成のテスト。APIは呼びません。"""

from __future__ import annotations

from src.csv_loader import StickerEntry, load_stickers
from src.prompt_generator import (
    FALLBACK_MASTER_PROMPT,
    build_prompt,
    build_prompts,
    load_master_prompt,
    save_prompt,
)

COMMON_LINES = [
    "Japanese cute chibi office worker mascot.",
    "Consistent character design.",
    "Male Japanese office worker in his 30s.",
    "Short black hair.",
    "White dress shirt.",
    "Simple dark necktie.",
    "Slightly rounded body.",
    "2.5-head-tall chibi proportions.",
    "Thick clean outline.",
    "Simple flat illustration.",
    "Friendly and humorous.",
    "Highly readable at small size.",
    "Full body.",
    "Transparent background.",
    "No scenery.",
    "No frame.",
    "No watermark.",
]


def _entry():
    return StickerEntry(
        id="001", text="了解！", action="片手で敬礼する", expression="自信のある笑顔",
        category="basic",
    )


def test_master_prompt_file_matches_spec(real_config):
    master = load_master_prompt(real_config.master_prompt_path)
    for line in COMMON_LINES:
        assert line in master, f"マスタープロンプトに不足: {line}"


def test_fallback_used_when_file_missing(tmp_path):
    assert load_master_prompt(tmp_path / "nope.txt") == FALLBACK_MASTER_PROMPT


def test_prompt_contains_common_block(real_config):
    prompt = build_prompt(_entry(), load_master_prompt(real_config.master_prompt_path))
    for line in COMMON_LINES:
        assert line in prompt


def test_prompt_contains_pose_and_expression():
    prompt = build_prompt(_entry())
    assert "片手で敬礼する" in prompt
    assert "自信のある笑顔" in prompt


def test_prompt_forbids_text_rendering():
    prompt = build_prompt(_entry())
    lowered = prompt.lower()
    assert "no text" in lowered
    assert "no japanese characters" in lowered
    assert "no speech balloons" in lowered
    # セリフ本体はプロンプトに含めない（AIに日本語を描かせないため）
    assert "了解！" not in prompt


def test_prompt_demands_transparency_and_consistency():
    prompt = build_prompt(_entry())
    assert "transparent" in prompt.lower()
    assert "CHARACTER CONSISTENCY" in prompt


def test_all_100_prompts_are_unique(real_config):
    entries = load_stickers(real_config.csv_path)
    prompts = build_prompts(entries, load_master_prompt(real_config.master_prompt_path))
    assert len(prompts) == len(entries)
    # ポーズ・表情が違うのでプロンプトも全て異なるはず
    assert len(set(prompts.values())) == len(entries)
    # セリフ本文はどのプロンプトにも含まれない
    for entry in entries:
        assert entry.text not in prompts[entry.id]


# --- 日本語のマスタープロンプト -----------------------------------------
FIXED_LINES = [
    "Consistent character design.",
    "Thick clean outline.",
    "Simple flat illustration.",
    "Highly readable at small size.",
    "Full body.",
    "Transparent background.",
    "No scenery.",
    "No frame.",
    "No watermark.",
]


def test_japanese_master_takes_priority(tmp_path):
    en = tmp_path / "en.txt"
    en.write_text("English character.", encoding="utf-8")
    ja = tmp_path / "ja.txt"
    ja.write_text("茶色のボブヘアの女性。", encoding="utf-8")

    master = load_master_prompt(en, ja)
    assert "茶色のボブヘアの女性。" in master
    assert "English character." not in master
    # スタンプとして使うための技術的な指示は必ず付く
    for line in FIXED_LINES:
        assert line in master


def test_empty_japanese_falls_back_to_english(tmp_path):
    en = tmp_path / "en.txt"
    en.write_text("English character.", encoding="utf-8")
    ja = tmp_path / "ja.txt"
    ja.write_text("   \n", encoding="utf-8")
    assert load_master_prompt(en, ja) == "English character."
    assert load_master_prompt(en, tmp_path / "missing.txt") == "English character."


def test_default_japanese_master_matches_english_character():
    """初期値の日本語説明は、英語版と同じキャラクターを日本語で書いたものです。

    実際の prompts/character_master_ja.txt はユーザーが自由に書き換えるので、
    その中身ではなく、組み込みの初期値（DEFAULT_CHARACTER_JA）を確認します。
    """
    from src.prompt_generator import DEFAULT_CHARACTER_JA

    for word in ("30代", "日本人男性", "会社員", "頭身", "黒髪", "ワイシャツ", "ネクタイ"):
        assert word in DEFAULT_CHARACTER_JA


def test_project_master_prompt_always_has_fixed_lines(real_config):
    """ユーザーがどんなキャラを書いても、技術的な指示は必ず付くこと。"""
    from src.prompt_generator import master_prompt_from_config

    master = master_prompt_from_config(real_config)
    for line in FIXED_LINES:
        assert line in master


def test_japanese_master_used_in_every_sticker_prompt(real_config, tmp_path):
    ja = tmp_path / "ja.txt"
    ja.write_text("茶色のボブヘアの女性。\n黄色いパーカー。", encoding="utf-8")
    master = load_master_prompt(tmp_path / "none.txt", ja)

    entries = load_stickers(real_config.csv_path)
    prompts = build_prompts(entries, master)
    assert len(set(prompts.values())) == len(entries)
    for entry in entries:
        p = prompts[entry.id]
        assert "茶色のボブヘアの女性。" in p
        assert "no japanese characters" in p.lower()  # 文字を描かせない指示は残る
        assert entry.text not in p                    # セリフ本文は送らない


def test_consistency_rule_has_no_character_specific_features():
    """固定の指示に特定キャラの特徴を書くと、キャラを変えたときに矛盾します。"""
    from src.prompt_generator import CONSISTENCY_RULE, NO_TEXT_RULE

    for fixed in (CONSISTENCY_RULE, NO_TEXT_RULE):
        lowered = fixed.lower()
        for word in ("black hair", "dress shirt", "necktie", "2.5-head", "office worker", "male"):
            assert word not in lowered, word


def test_different_character_has_no_contradiction(tmp_path):
    """日本語で別キャラを書いたとき、元のキャラの特徴が紛れ込まないこと。"""
    ja = tmp_path / "ja.txt"
    ja.write_text("20代の女性。\n茶色のボブヘア。\n黄色いパーカー。", encoding="utf-8")
    prompt = build_prompt(_entry(), load_master_prompt(tmp_path / "none.txt", ja)).lower()
    for word in ("black hair", "dress shirt", "necktie", "office worker"):
        assert word not in prompt, word


def test_save_prompt(tmp_path):
    p = save_prompt("hello", tmp_path / "generated", "001")
    assert p.read_text(encoding="utf-8") == "hello"
    assert p.name == "001.txt"
