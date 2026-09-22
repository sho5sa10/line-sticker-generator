"""LINE Creators Market に登録する「タイトル・説明文」などを作ります。

キャラクターの特徴（かんたん入力）とセリフ一覧から、APIを使わずに（無料で）案を組み立てます。
作った案はそのまま使うこともできますし、画面で自由に書き換えることもできます。

LINE 公式の制作ガイドライン（2026-09-22 確認）
  https://creator.line.me/ja/guideline/sticker/
- タイトル 40文字以内 / 説明文 160文字以内 / クリエイター名 50文字以内
- コピーライト 50文字以内（英数字のみ）
- 全角文字は2文字として数える。絵文字は使えない
- 英語での登録は必須（日本語などは「言語を追加」で登録）
  https://help2.line.me/creators/web/categoryId/20002326/3/pc?lang=ja
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import date
from pathlib import Path

from . import character_profile as cprof

# 各項目の上限（全角は2文字として数えた値）
LIMITS = {
    "creator": 50,
    "copyright": 50,
    "title_en": 40,
    "desc_en": 160,
    "title_ja": 40,
    "desc_ja": 160,
}
FIELD_LABELS = {
    "creator": "クリエイター名",
    "copyright": "コピーライト",
    "title_en": "タイトル（英語・必須）",
    "desc_en": "説明文（英語・必須）",
    "title_ja": "タイトル（日本語）",
    "desc_ja": "説明文（日本語）",
}
# 半角英数字・記号だけで書く項目
ASCII_FIELDS = ("copyright", "title_en", "desc_en")
REQUIRED = ("creator", "copyright", "title_en", "desc_en")


# ---------------------------------------------------------------------------
# 文字数と禁止事項のチェック
# ---------------------------------------------------------------------------
def display_width(text: str) -> int:
    """LINE の数え方の文字数（全角は2、半角は1）。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F", "A") else 1 for ch in text)


def _has_emoji(text: str) -> bool:
    for ch in text:
        cp = ord(ch)
        if (0x1F000 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF or 0xFE00 <= cp <= 0xFE0F
                or cp == 0x200D or 0x1F1E6 <= cp <= 0x1F1FF):
            return True
    return False


_URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
_LINE_RE = re.compile(r"(?<![A-Za-z])LINE(?![A-Za-z])|ＬＩＮＥ|(?<![ァ-ヶー])ライン", re.IGNORECASE)
_RELEASE_RE = re.compile(r"発売|販売開始|リリース|\d+\s*月\s*\d+\s*日|on sale|release", re.IGNORECASE)


def check_field(key: str, text: str) -> list[str]:
    """1項目ぶんの問題点（日本語の文）を返します。問題がなければ空。"""
    text = text or ""
    issues: list[str] = []
    if not text.strip():
        if key in REQUIRED:
            issues.append("必須の項目です")
        return issues
    width, limit = display_width(text), LIMITS[key]
    if width > limit:
        issues.append(f"長すぎます（{width}/{limit}。全角は2文字として数えます）")
    if _has_emoji(text):
        issues.append("絵文字は使えません")
    if key in ASCII_FIELDS and not text.isascii():
        issues.append("半角の英数字・記号だけで書いてください（全角文字は使えません）")
    if key != "creator" and _URL_RE.search(text):
        issues.append("URLは入れられません")
    if key in ("title_en", "desc_en", "title_ja", "desc_ja"):
        if _LINE_RE.search(text):
            issues.append("「LINE」という言葉は入れないでください")
        if _RELEASE_RE.search(text):
            issues.append("発売日などの告知は入れられません")
    return issues


def check_listing(listing: dict) -> dict[str, list[str]]:
    return {key: check_field(key, str(listing.get(key, "") or "")) for key in LIMITS}


def _fit(text: str, limit: int) -> str:
    """上限を超えたら、文の区切り（。/ .）で切り詰めます。"""
    if display_width(text) <= limit:
        return text
    parts = re.split(r"(?<=[。．.!！])\s*", text)
    out = ""
    for part in parts:
        cand = (out + (" " if out and part.isascii() and out.isascii() else "") + part)
        if display_width(cand) > limit:
            break
        out = cand
    if out:
        return out
    while display_width(text) > limit:
        text = text[:-1]
    return text


# ---------------------------------------------------------------------------
# キャラクターの呼び名
# ---------------------------------------------------------------------------
_SUBJECT_EN = {
    "ねこ": "Cat", "いぬ": "Dog", "トイプードル": "Toy Poodle", "うさぎ": "Bunny",
    "くま": "Bear", "パンダ": "Panda", "ハムスター": "Hamster", "モモンガ": "Flying Squirrel",
    "ペンギン": "Penguin", "ことり": "Little Bird", "ハシビロコウ": "Shoebill",
    "白くて丸い生き物": "Round White Creature", "もちもちの生き物": "Mochi Creature",
    "おばけ": "Ghost", "小さな妖精": "Little Fairy",
    "パン": "Bread", "プリン": "Pudding", "おにぎり": "Rice Ball", "おもち": "Mochi",
    "たまご": "Egg", "きのこ": "Mushroom",
    "会社員": "Office Worker", "学生": "Student", "店員": "Shop Clerk", "エンジニア": "Engineer",
    "先生": "Teacher", "医師": "Doctor", "看護師": "Nurse", "主婦": "Homemaker", "主夫": "Homemaker",
}
_COLOR_JA = {"白": "白い", "クリーム": "クリーム色の", "茶": "茶色い", "グレー": "グレーの",
             "黒": "黒い", "三毛": "三毛の", "ピンク": "ピンクの", "黄": "黄色い"}
_COLOR_EN = {"白": "White", "クリーム": "Cream", "茶": "Brown", "グレー": "Gray", "黒": "Black",
             "三毛": "Calico", "ピンク": "Pink", "黄": "Yellow"}
_MOOD_JA = {"親しみやすい": "親しみやすい", "やさしい": "やさしい", "コミカル": "おちゃめな",
            "かわいい": "かわいい", "上品": "上品な", "クール": "クールな", "シュール": "シュールな",
            "ゆるい": "ゆるい", "元気": "元気な", "まじめ": "まじめな"}
_MOOD_EN = {"親しみやすい": "Friendly", "やさしい": "Gentle", "コミカル": "Funny",
            "かわいい": "Cute", "上品": "Elegant", "クール": "Cool", "シュール": "Quirky",
            "ゆるい": "Laid-back", "元気": "Cheerful", "まじめ": "Earnest"}


def _subject(profile: dict) -> tuple[str, str]:
    """キャラの呼び名（日本語, 英語）。"""
    kind = profile.get("kind", "")
    if kind == "動物" and profile.get("animal"):
        ja = profile["animal"]
    elif kind == "ふしぎな生き物" and profile.get("creature"):
        ja = profile["creature"]
    elif kind == "食べ物" and profile.get("food"):
        ja = profile["food"]
    elif kind in cprof.HUMAN_KINDS:
        job, gender = profile.get("job", ""), profile.get("gender", "")
        if job:
            ja = f"{job}の{gender}" if gender else job
            en = _SUBJECT_EN.get(job, "Person")
            return ja, en
        ja = {"男性": "男の子", "女性": "女の子"}.get(gender, "ひと")
        return ja, {"男性": "Guy", "女性": "Girl"}.get(gender, "Person")
    else:
        return "キャラクター", "Buddy"
    return ja, _SUBJECT_EN.get(ja, "Buddy")


# ---------------------------------------------------------------------------
# セリフの傾向
# ---------------------------------------------------------------------------
# (見出しの言葉, 英語, 探す言葉)
_PHRASE_EN = [
    ("了解", "Got it"), ("OK", "OK"), ("おけ", "OK"), ("ありがとう", "Thanks"),
    ("お疲れ", "Good work"), ("おつ", "Good work"), ("おはよう", "Good morning"),
    ("おやすみ", "Good night"), ("ごめん", "Sorry"), ("すみません", "Sorry"),
    ("よろしく", "Thanks in advance"), ("お願い", "Please"), ("すごい", "Awesome"),
    ("やった", "Yay"), ("大丈夫", "No problem"), ("ただいま", "I'm home"),
]


_POLITE_RE = re.compile(r"(です|ます|ました|ません|ください|ございます)[！!？?…。]*$")


def _is_polite(text: str) -> bool:
    return bool(_POLITE_RE.search(text))


def _themes(texts: list[str], categories: list[str]) -> dict:
    n = max(len(texts), 1)
    polite = sum(1 for t in texts if _is_polite(t)) / n
    work = sum(1 for c in categories if c in ("work", "request", "reply", "move")) / n
    return {
        "polite": polite >= 0.5,
        "mixed": 0.2 <= polite < 0.5,
        "work": work >= 0.2,
    }


def _core(text: str) -> str:
    """語尾の記号をのぞいた本体（「了解！」→「了解」）。"""
    return re.sub(r"[！!？?…。〜ー]+$", "", text)


def _samples(texts: list[str], categories: list[str], count: int = 3, max_len: int = 7,
             prefer: str = "") -> list[str]:
    """説明文に載せるセリフの例。短く、種類（カテゴリ）がばらけるように選びます。

    prefer="polite" なら敬語、"casual" ならくだけたセリフ、"mixed" なら両方を交互に優先します。
    """
    pairs = list(zip(texts, categories))
    if prefer in ("polite", "casual"):
        want = prefer == "polite"
        pairs.sort(key=lambda p: _is_polite(p[0]) != want)  # 安定ソートなので元の順も保ちます
    elif prefer == "mixed":
        pol = [p for p in pairs if _is_polite(p[0])]
        cas = [p for p in pairs if not _is_polite(p[0])]
        pairs = [p for pair in zip(pol, cas) for p in pair] + pol[len(cas):] + cas[len(pol):]
    texts, categories = [p[0] for p in pairs], [p[1] for p in pairs]
    picked: list[str] = []
    cores: list[str] = []
    used_cats: set[str] = set()
    for rnd in (0, 1):  # 1周目はカテゴリごとに1つ、2周目は足りない分を補う
        for t, cat in zip(texts, categories):
            core = re.sub(r"[！!？?…。〜ー]+$", "", t)
            if not core or len(t) > max_len or t in picked:
                continue
            if any(core.startswith(c) or c.startswith(core) for c in cores):
                continue
            if rnd == 0 and cat in used_cats:
                continue
            picked.append(t)
            cores.append(core)
            used_cats.add(cat)
            if len(picked) >= count:
                return picked
    return picked


def _english_samples(texts: list[str], count: int = 3) -> list[str]:
    out: list[str] = []
    joined = " ".join(texts)
    for key, en in _PHRASE_EN:
        if key in joined and en not in out:
            out.append(en)
        if len(out) >= count:
            break
    return out


def _join_en(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


# ---------------------------------------------------------------------------
# 案を作る
# ---------------------------------------------------------------------------
def _first_fit(options: list[str], limit: int) -> str:
    """上限に収まる最初の案（どれも収まらなければ最後の案を切り詰め）。"""
    for opt in options:
        opt = _tidy(opt)
        if opt and display_width(opt) <= limit:
            return opt
    return _fit(_tidy(options[-1]), limit)


def _quote_en(items: list[str]) -> str:
    return _join_en([f'"{s}"' for s in items])


def suggest(profile: dict, entries, *, creator: str = "", year: int | None = None,
            volume: int = 1) -> list[dict]:
    """タイトル・説明文の案を3つ返します（どれも文字数の上限内）。

    volume が2以上なら、タイトルの最後に番号を付けます（同じキャラで複数セットを出すとき用）。
    """
    texts = [e.text.replace("\n", "") for e in entries]
    cats = [getattr(e, "category", "") for e in entries]
    th = _themes(texts, cats)
    subj_ja, subj_en = _subject(profile)
    moods = [m for m in profile.get("mood", []) if m in _MOOD_JA]
    mood_ja = _MOOD_JA[moods[0]] if moods else ""
    mood_en = _MOOD_EN[moods[0]] if moods else ""
    mood2_en = _MOOD_EN[moods[1]] if len(moods) > 1 else mood_en
    fur = profile.get("fur", "") if profile.get("kind") in cprof.FUR_KINDS else ""
    color_ja, color_en = _COLOR_JA.get(fur, ""), _COLOR_EN.get(fur, "")

    if th["polite"] and th["work"]:
        prefer = "polite"
        theme_ja, short_ja = "仕事で使える敬語", "敬語"
        theme_en, short_en = "Polite Work Phrases", "Polite Phrases"
        use_ja, use_en = "職場や目上の人とのやりとり", "chats with coworkers and your boss"
    elif th["polite"]:
        prefer = "polite"
        theme_ja, short_ja = "ていねいな", "ていねい"
        theme_en, short_en = "Polite Phrases", "Polite Phrases"
        use_ja, use_en = "きちんと返したいとき", "polite everyday chats"
    elif th["mixed"]:
        prefer = "mixed"
        theme_ja, short_ja = "敬語もタメ口も使える", "毎日使える"
        theme_en, short_en = "Polite and Casual Phrases", "Everyday Phrases"
        use_ja, use_en = "仕事でもプライベートでも", "both work and private chats"
    elif th["work"]:
        prefer = "casual"
        theme_ja, short_ja = "仕事で使える", "お仕事"
        theme_en, short_en = "Work Replies", "Work Replies"
        use_ja, use_en = "仕事の連絡", "quick replies at work"
    else:
        prefer = "casual"
        theme_ja, short_ja = "毎日使える", "毎日"
        theme_en, short_en = "Everyday Phrases", "Everyday"
        use_ja, use_en = "家族や友だちとの会話", "chats with family and friends"

    # 英語の形容詞の順番: 気分 → 大きさ(Little) → 色 → 種類
    if subj_en.startswith("Little ") and color_en:
        noun_en = f"Little {color_en} {subj_en[len('Little '):]}"
    else:
        noun_en = " ".join(w for w in (color_en, subj_en) if w)
    who_en = " ".join(w for w in (mood_en, noun_en) if w)
    who_en_l = who_en.lower()
    a_en = "an" if who_en_l[:1] in "aeiou" else "a"
    who_ja = f"{mood_ja}{color_ja}{subj_ja}"

    if prefer == "mixed":  # 敬語とくだけたセリフを両方見せます
        picks = _samples(texts, cats, count=2, prefer="polite")
        cores = [_core(t) for t in picks]
        casual = [t for t in _samples(texts, cats, count=6, prefer="casual")
                  if not _is_polite(t)
                  and not any(_core(t).startswith(c[:2]) or c.startswith(_core(t)[:2]) for c in cores)]
        picks = picks[:1] + casual[:1] + picks[1:2] if casual else picks
    else:
        picks = _samples(texts, cats, prefer=prefer)
    sample_ja = "".join(f"「{t}」" for t in picks)
    ni_douzo = "どうぞ" if use_ja.endswith("も") else "にどうぞ"
    de = "" if use_ja.endswith(("も", "とき")) else "で"
    en_samples = _english_samples(texts)
    like_en = f" like {_quote_en(en_samples)}" if en_samples else ""

    creator = creator.strip()
    copyright_ = f"(C){year or date.today().year} {creator}" if creator and creator.isascii() else ""

    kinds = [
        {
            "title_ja": [f"{who_ja}の{short_ja}スタンプ", f"{mood_ja}{subj_ja}の{short_ja}スタンプ",
                         f"{subj_ja}の{short_ja}スタンプ"],
            "title_en": [f"{who_en}: {theme_en}", f"{who_en}: {short_en}",
                         f"{noun_en}: {short_en}", f"{subj_en} Stickers"],
            "desc_ja": [f"{who_ja}が、{sample_ja}など{theme_ja}ひとことでお返事します。{use_ja}{ni_douzo}。",
                        f"{who_ja}が、{sample_ja}など{theme_ja}ひとことでお返事します。"],
            "desc_en": [f"{a_en.capitalize()} {who_en_l} replies with {theme_en.lower()}{like_en}. "
                        f"Great for {use_en}.",
                        f"{a_en.capitalize()} {who_en_l} replies with {theme_en.lower()}{like_en}."],
        },
        {
            "title_ja": [f"{subj_ja}の{theme_ja}ひとこと", f"{subj_ja}の{short_ja}ひとこと"],
            "title_en": [f"{subj_en} {theme_en}", f"{subj_en} {short_en}"],
            "desc_ja": [f"{sample_ja}など、{theme_ja}ひとことを{who_ja}がお届け。すぐ返したいときに便利です。",
                        f"{sample_ja}など、{theme_ja}ひとことを{who_ja}がお届け。"],
            "desc_en": [f"Reply in one tap with phrases{like_en} from this {who_en_l}. "
                        f"Perfect for {use_en}.",
                        f"Reply in one tap with phrases{like_en} from this {who_en_l}."],
        },
        {
            "title_ja": [f"表情ゆたかな{subj_ja}の{short_ja}スタンプ", f"{mood_ja}{subj_ja}の毎日スタンプ",
                         f"{subj_ja}の毎日スタンプ"],
            "title_en": [f"{mood2_en} {noun_en} Daily Stickers", f"{mood2_en} {subj_en} Stickers",
                         f"{subj_en} Stickers"],
            "desc_ja": [f"表情ゆたかな{who_ja}のスタンプです。{sample_ja}など、{use_ja}{de}気軽に使えます。",
                        f"表情ゆたかな{who_ja}のスタンプです。{sample_ja}など気軽に使えます。"],
            "desc_en": [f"Expressive stickers of {a_en} {who_en_l}{like_en}. Easy to use for {use_en}.",
                        f"Expressive stickers of {a_en} {who_en_l}{like_en}."],
        },
    ]
    out: list[dict] = []
    seen: set[str] = set()
    if volume > 1:
        for k in kinds:
            k["title_ja"] = [f"{t}{volume}" for t in k["title_ja"]]
            k["title_en"] = [f"{t} {volume}" for t in k["title_en"]]
    for k in kinds:
        c = {key: _first_fit(opts, LIMITS[key]) for key, opts in k.items()}
        if (c["title_ja"], c["title_en"]) in seen:
            continue
        seen.add((c["title_ja"], c["title_en"]))
        c["creator"] = creator
        c["copyright"] = copyright_
        out.append(c)
    return out


def _tidy(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text.replace("のの", "の").replace(" :", ":")


# ---------------------------------------------------------------------------
# 保存
# ---------------------------------------------------------------------------
def listing_path(config) -> Path:
    return config.root / "data" / "listing.json"


def load_listing(config) -> dict:
    path = listing_path(config)
    if not path.exists():
        return {k: "" for k in LIMITS}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {k: "" for k in LIMITS}
    return {k: str(data.get(k, "") or "") for k in LIMITS}


def save_listing(config, listing: dict) -> dict:
    clean = {k: str(listing.get(k, "") or "").strip() for k in LIMITS}
    path = listing_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    return clean
