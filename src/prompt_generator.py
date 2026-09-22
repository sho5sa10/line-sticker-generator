"""CSVの1行から画像生成プロンプトを組み立てます。

方針:
  - 画像生成AIには「キャラクター + ポーズ + 表情」だけを描かせます。
  - 日本語テキストは画像生成AIに描かせません（誤字・文字化けの原因になるため）。
    セリフは後処理で Pillow により合成します (text_renderer.py)。
"""

from __future__ import annotations

from pathlib import Path

from .csv_loader import StickerEntry

# マスタープロンプトが読めない場合のフォールバック（仕様書と同一内容）
FALLBACK_MASTER_PROMPT = """Japanese cute chibi office worker mascot.
Consistent character design.
Male Japanese office worker in his 30s.
Short black hair.
White dress shirt.
Simple dark necktie.
Slightly rounded body.
2.5-head-tall chibi proportions.
Thick clean outline.
Simple flat illustration.
Friendly and humorous.
Highly readable at small size.
Full body.
Transparent background.
No scenery.
No frame.
No watermark."""

# カテゴリ -> 演出のヒント（英語）
CATEGORY_HINTS: dict[str, str] = {
    "basic": "calm, cooperative and dependable",
    "thanks": "grateful and warm-hearted",
    "apology": "apologetic and humble",
    "request": "polite and asking for a favor",
    "work": "busy and businesslike",
    "move": "in motion, going somewhere",
    "surprise": "strongly surprised, exaggerated comedic reaction",
    "joy": "delighted and celebrating",
    "think": "thinking it over",
    "trouble": "troubled and struggling",
    "tired": "exhausted and drained",
    "life": "relaxed everyday mood",
    "greeting": "friendly everyday greeting",
    "care": "caring about the other person",
    "reply": "checking a smartphone message",
    "misc": "lighthearted and comedic",
}

# 「文字を描かせない」ための強い指示。全プロンプト共通。
NO_TEXT_RULE = """STRICT RULES:
Draw the character only. Absolutely no text, no letters, no Japanese characters,
no kanji, no kana, no numbers, no speech balloons, no captions, no logo, no signature.
Fully transparent background (alpha channel), no background color, no shadow on the ground.
Single character, centered, full body visible, with generous empty margin on all four sides.
The WHOLE character, from the top of the head (including ears or hair) down to the bottom of the feet,
must be inside the image. Keep the character at about 70% of the image height,
with clear empty space above the head and below the feet.
Do not crop any part of the body, not even the feet."""

# キャラクター固有の特徴（髪型・服装など）はここに書きません。
# 書くとマスタープロンプトでキャラクターを変えたときに矛盾するためです。
CONSISTENCY_RULE = """CHARACTER CONSISTENCY (highest priority):
Keep the character design exactly identical every time, exactly as described above
and as shown in the reference image:
same face, same hairstyle and hair color, same eye shape, same outfit and outfit colors,
same accessories, same body shape and head-to-body proportions,
same outline weight, same color palette.
Only the pose and the facial expression change."""


# 日本語でキャラクターを書く場合に、自動で付け足す技術的な指示。
# 背景透過・全身・枠なしなどはスタンプとして使えるかどうかに直結するため、
# 画像生成AIが最も安定して従う英語のまま固定します。
FIXED_STYLE_PROMPT = """Cute chibi mascot character for LINE messenger stickers.
Consistent character design.
Thick clean outline.
Simple flat illustration.
Highly readable at small size.
Full body.
Transparent background.
No scenery.
No frame.
No watermark."""

# 日本語マスタープロンプトの初期値（英語版と同じキャラクターを日本語で書いたもの）
DEFAULT_CHARACTER_JA = """30代くらいの日本人男性の会社員。
2〜2.5頭身のちびキャラ。
短い黒髪。
白いワイシャツ。
シンプルな濃い色のネクタイ。
少し丸みのある体型。
親しみやすく、コミカルな雰囲気。
配色はシンプルにする。"""


def _read(path: str | Path | None) -> str:
    if not path:
        return ""
    p = Path(path)
    return p.read_text(encoding="utf-8").strip() if p.exists() else ""


def compose_japanese_master(character_ja: str) -> str:
    """日本語のキャラクター説明に、固定の技術指示（英語）を付け足します。"""
    return (
        "CHARACTER DESCRIPTION (written in Japanese; follow every detail exactly):\n"
        f"{character_ja.strip()}\n\n{FIXED_STYLE_PROMPT}"
    )


def load_master_prompt(path: str | Path | None, ja_path: str | Path | None = None) -> str:
    """キャラクター・マスタープロンプトを読み込みます。

    日本語版（ja_path）に中身があればそれを優先し、固定の技術指示を付け足します。
    無ければ英語版（path）、それも無ければ組み込みのフォールバックを使います。
    """
    character_ja = _read(ja_path)
    if character_ja:
        return compose_japanese_master(character_ja)
    return _read(path) or FALLBACK_MASTER_PROMPT


def master_prompt_from_config(config) -> str:
    """設定に従ってマスタープロンプトを読み込みます（日本語版を優先）。"""
    return load_master_prompt(config.master_prompt_path, config.master_prompt_ja_path)


def build_prompt(entry: StickerEntry, master_prompt: str | None = None) -> str:
    """1件分の画像生成プロンプトを生成します。"""
    master = (master_prompt or FALLBACK_MASTER_PROMPT).strip()
    hint = CATEGORY_HINTS.get(entry.category, CATEGORY_HINTS["misc"])

    action = entry.action or "standing naturally"
    expression = entry.expression or "friendly smile"

    # 日本語の指示は gpt-image-1 が解釈できるためそのまま渡し、
    # 英語の補足で意図を補強します（"描かせる文字" ではなく "指示" です）。
    specific = (
        f"POSE / ACTION (Japanese instruction): {action}\n"
        f"FACIAL EXPRESSION (Japanese instruction): {expression}\n"
        f"OVERALL MOOD: {hint}.\n"
        f"The gesture should read clearly even at 100x100 pixels."
    )

    return "\n\n".join([master, specific, CONSISTENCY_RULE, NO_TEXT_RULE])


def build_prompts(entries, master_prompt: str | None = None) -> dict[str, str]:
    """複数件分のプロンプトを {id: prompt} で返します。"""
    return {e.id: build_prompt(e, master_prompt) for e in entries}


def save_prompt(prompt: str, out_dir: str | Path, sticker_id: str) -> Path:
    """生成したプロンプトを prompts/generated/<id>.txt に保存します。"""
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{sticker_id}.txt"
    p.write_text(prompt, encoding="utf-8")
    return p
