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
    assert len(prompts) == 100
    # ポーズ・表情が違うのでプロンプトも全て異なるはず
    assert len(set(prompts.values())) == 100
    # セリフ本文はどのプロンプトにも含まれない
    for entry in entries:
        assert entry.text not in prompts[entry.id]


def test_save_prompt(tmp_path):
    p = save_prompt("hello", tmp_path / "generated", "001")
    assert p.read_text(encoding="utf-8") == "hello"
    assert p.name == "001.txt"
