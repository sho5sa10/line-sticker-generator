"""キャラクターの特徴を「選択肢」から日本語の説明文に組み立てます。

GUI の「かんたん入力」で使います。選択肢の定義・組み立てロジックをここに集めているので、
画面（JS）とテストの両方が同じ内容を使います。
"""

from __future__ import annotations

import json
from pathlib import Path

HUMAN_KINDS = ["人（日本人）", "人"]
DECADES = ["10代", "20代", "30代", "40代", "50代"]

# 各項目の定義。when を持つ項目は、条件を満たすときだけ表示・使用されます。
GROUPS: list[dict] = [
    {"key": "kind", "label": "キャラの種類", "type": "single",
     "options": ["人（日本人）", "人", "動物"]},
    {"key": "animal", "label": "動物の種類", "type": "single",
     "options": ["ねこ", "いぬ", "うさぎ", "くま", "パンダ", "ペンギン", "ハムスター", "ことり"],
     "when": {"kind": ["動物"]}},
    {"key": "gender", "label": "性別", "type": "single",
     "options": ["男性", "女性"], "when": {"kind": HUMAN_KINDS}},
    {"key": "age", "label": "年齢", "type": "single",
     "options": ["子ども", *DECADES, "シニア"], "when": {"kind": HUMAN_KINDS}},
    {"key": "job", "label": "職業・立場", "type": "single",
     "options": ["会社員", "学生", "店員", "エンジニア", "先生", "医師", "看護師", "主婦", "主夫"],
     "when": {"kind": HUMAN_KINDS}},
    {"key": "heads", "label": "頭身", "type": "single",
     "options": ["2頭身", "2〜2.5頭身", "3頭身"]},
    {"key": "hair", "label": "髪型", "type": "single",
     "options": ["短髪", "ボブ", "ロング", "ポニーテール", "おだんご", "七三分け", "坊主"],
     "when": {"kind": HUMAN_KINDS}},
    {"key": "hair_color", "label": "髪の色", "type": "single",
     "options": ["黒", "茶", "金", "銀", "ピンク", "青"], "when": {"kind": HUMAN_KINDS}},
    {"key": "outfit", "label": "服装", "type": "single",
     "options": ["ワイシャツとネクタイ", "スーツ", "パーカー", "Tシャツ", "セーラー服",
                 "学ラン", "エプロン", "白衣", "着物"]},
    {"key": "outfit_color", "label": "服の色", "type": "single",
     "options": ["白", "黒", "紺", "グレー", "赤", "黄", "緑", "水色", "ピンク"]},
    {"key": "body", "label": "体型", "type": "single",
     "options": ["ふつう", "少し丸い", "ぽっちゃり", "細身"]},
    {"key": "items", "label": "小物・特徴", "type": "multi",
     "options": ["メガネ", "帽子", "ひげ", "そばかす", "赤いほっぺ", "ヘッドホン", "リボン"]},
    {"key": "mood", "label": "雰囲気", "type": "multi",
     "options": ["親しみやすい", "コミカル", "かわいい", "クール", "ゆるい", "元気", "まじめ"]},
    {"key": "palette", "label": "配色", "type": "single",
     "options": ["シンプル", "カラフル", "パステル"]},
]

EXTRA_MAX_CHARS = 500

# ひな形。「会社員（男性）」は同梱の日本語説明（DEFAULT_CHARACTER_JA）と同じ文章になります。
PRESETS: dict[str, dict] = {
    "会社員（男性）": {
        "kind": "人（日本人）", "gender": "男性", "age": "30代", "job": "会社員",
        "heads": "2〜2.5頭身", "hair": "短髪", "hair_color": "黒",
        "outfit": "ワイシャツとネクタイ", "outfit_color": "白", "body": "少し丸い",
        "mood": ["親しみやすい", "コミカル"], "palette": "シンプル",
    },
    "会社員（女性）": {
        "kind": "人（日本人）", "gender": "女性", "age": "20代", "job": "会社員",
        "heads": "2〜2.5頭身", "hair": "ボブ", "hair_color": "茶",
        "outfit": "スーツ", "outfit_color": "紺", "body": "ふつう",
        "mood": ["親しみやすい", "かわいい"], "palette": "シンプル",
    },
    "学生": {
        "kind": "人（日本人）", "gender": "女性", "age": "10代", "job": "学生",
        "heads": "2頭身", "hair": "ポニーテール", "hair_color": "黒",
        "outfit": "セーラー服", "outfit_color": "紺", "body": "ふつう",
        "mood": ["かわいい", "元気"], "palette": "シンプル",
    },
    "ねこ": {
        "kind": "動物", "animal": "ねこ", "heads": "2頭身", "body": "少し丸い",
        "items": ["赤いほっぺ"], "mood": ["かわいい", "ゆるい"], "palette": "パステル",
    },
    "いぬ": {
        "kind": "動物", "animal": "いぬ", "heads": "2頭身",
        "outfit": "Tシャツ", "outfit_color": "赤", "body": "ふつう",
        "mood": ["親しみやすい", "元気"], "palette": "カラフル",
    },
    "おじいさん": {
        "kind": "人（日本人）", "gender": "男性", "age": "シニア",
        "heads": "2頭身", "hair": "短髪", "hair_color": "銀",
        "outfit": "着物", "outfit_color": "紺", "body": "少し丸い",
        "items": ["メガネ", "ひげ"], "mood": ["親しみやすい", "ゆるい"], "palette": "シンプル",
    },
}

# --- 文章化の辞書 -------------------------------------------------------
_HAIR_COLOR_ADJ = {"黒": "黒い", "茶": "茶色の", "金": "金色の", "銀": "銀色の",
                   "ピンク": "ピンク色の", "青": "青い"}
_HAIR_STYLE = {"ボブ": "ボブヘア", "ロング": "ロングヘア", "ポニーテール": "ポニーテール",
               "おだんご": "おだんごヘア", "七三分け": "七三分けの髪", "坊主": "坊主頭"}
_OUTFIT_COLOR_ADJ = {"白": "白い", "黒": "黒い", "紺": "紺色の", "グレー": "グレーの",
                     "赤": "赤い", "黄": "黄色い", "緑": "緑色の", "水色": "水色の",
                     "ピンク": "ピンク色の"}
_BODY = {"ふつう": "ふつうの体型。", "少し丸い": "少し丸みのある体型。",
         "ぽっちゃり": "ぽっちゃりした体型。", "細身": "細身の体型。"}
_ITEMS = {"メガネ": "メガネをかけている。", "帽子": "帽子をかぶっている。",
          "ひげ": "ひげが生えている。", "そばかす": "そばかすがある。",
          "赤いほっぺ": "ほっぺが赤い。", "ヘッドホン": "ヘッドホンをつけている。",
          "リボン": "リボンをつけている。"}
# 雰囲気: (つなぎの形, 最後の形) 例: 親しみやすく、コミカルな雰囲気。
_MOOD = {"親しみやすい": ("親しみやすく", "親しみやすい"), "コミカル": ("コミカルで", "コミカルな"),
         "かわいい": ("かわいく", "かわいい"), "クール": ("クールで", "クールな"),
         "ゆるい": ("ゆるく", "ゆるい"), "元気": ("元気で", "元気な"),
         "まじめ": ("まじめで", "まじめな")}
_PALETTE = {"シンプル": "配色はシンプルにする。", "カラフル": "配色はカラフルにする。",
            "パステル": "パステルカラーのやわらかい配色にする。"}


def _group(key: str) -> dict:
    return next(g for g in GROUPS if g["key"] == key)


def is_visible(group: dict, profile: dict) -> bool:
    """when 条件を満たすか（例: 髪型は「人」のときだけ）。"""
    for key, allowed in (group.get("when") or {}).items():
        if profile.get(key) not in allowed:
            return False
    return True


def normalize(profile: dict | None) -> dict:
    """未知の項目・未知の値・表示されない項目を取り除いた安全なプロフィールを返します。"""
    src = profile if isinstance(profile, dict) else {}
    out: dict = {}
    for g in GROUPS:  # 定義順に処理するので、kind が先に確定します
        value = src.get(g["key"])
        if g["type"] == "single":
            if isinstance(value, str) and value in g["options"]:
                out[g["key"]] = value
        else:
            if isinstance(value, list):
                picked = [v for v in g["options"] if v in value]  # 定義順にそろえる
                if picked:
                    out[g["key"]] = picked
    out = {k: v for k, v in out.items() if is_visible(_group(k), out)}
    extra = src.get("extra")
    if isinstance(extra, str) and extra.strip():
        out["extra"] = extra.strip()[:EXTRA_MAX_CHARS]
    return out


def _person_line(p: dict) -> str:
    kind, gender, age, job = p.get("kind"), p.get("gender"), p.get("age"), p.get("job")
    if kind == "動物":
        return f"{p.get('animal', '動物')}のキャラクター。"
    if kind not in HUMAN_KINDS:
        return ""
    nat = "日本人" if kind == "人（日本人）" else ""

    if age == "子ども":
        noun = {"男性": "男の子", "女性": "女の子"}.get(gender, "子ども")
    elif age == "シニア":
        noun = {"男性": "おじいさん", "女性": "おばあさん"}.get(gender, "お年寄り")
    else:
        noun = gender or ""

    if noun in ("男性", "女性"):
        core = f"{nat}{noun}"            # 日本人男性
    elif noun:
        core = f"{nat}の{noun}" if nat else noun   # 日本人の男の子
    else:
        core = nat                       # 日本人

    head = f"{age}くらいの" if age in DECADES else ""
    line = head + core + (f"の{job}" if job and core else job or "")
    return f"{line}。" if line else "人のキャラクター。"


def _hair_line(p: dict) -> str:
    style, color = p.get("hair"), p.get("hair_color")
    if not style and not color:
        return ""
    short = "短い" if style == "短髪" else ""
    if style in (None, "短髪"):
        if color == "黒":
            return f"{short}黒髪。"                               # 短い黒髪。
        return f"{short}{_HAIR_COLOR_ADJ.get(color, '')}髪。"      # 短い茶色の髪。
    return f"{_HAIR_COLOR_ADJ.get(color, '')}{_HAIR_STYLE[style]}。"  # 茶色のボブヘア。


def _outfit_lines(p: dict) -> list[str]:
    outfit = p.get("outfit")
    color = _OUTFIT_COLOR_ADJ.get(p.get("outfit_color"), "")
    if not outfit:
        return [f"{color}服を着ている。"] if color else []
    if outfit == "白衣":
        color = ""
    if outfit == "ワイシャツとネクタイ":
        return [f"{color}ワイシャツ。", "シンプルな濃い色のネクタイ。"]
    return [f"{color}{outfit}。"]


def _mood_line(p: dict) -> str:
    moods = p.get("mood") or []
    if not moods:
        return ""
    parts = [_MOOD[m][0] for m in moods[:-1]] + [_MOOD[moods[-1]][1]]
    return "、".join(parts) + "雰囲気。"


def compose(profile: dict | None) -> str:
    """選択内容から日本語の説明文を組み立てます（1行に1つの特徴）。"""
    p = normalize(profile)
    lines = [_person_line(p)]
    if p.get("heads"):
        lines.append(f"{p['heads']}のちびキャラ。")
    lines.append(_hair_line(p))
    lines += _outfit_lines(p)
    if p.get("body"):
        lines.append(_BODY[p["body"]])
    lines += [_ITEMS[i] for i in p.get("items", [])]
    lines.append(_mood_line(p))
    if p.get("palette"):
        lines.append(_PALETTE[p["palette"]])
    if p.get("extra"):
        lines += [ln.strip() for ln in p["extra"].splitlines() if ln.strip()]
    return "\n".join(ln for ln in lines if ln)


def load_profile(path: str | Path) -> dict | None:
    """保存された選択内容を読み込みます。無い・壊れている場合は None。"""
    p = Path(path)
    if not p.exists():
        return None
    try:
        return normalize(json.loads(p.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return None


def save_profile(path: str | Path, profile: dict) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(normalize(profile), ensure_ascii=False, indent=2) + "\n",
                 encoding="utf-8")
    return p


def infer_profile(text: str) -> dict | None:
    """説明文がどれかのひな形とまったく同じなら、そのひな形の選択内容を返します。

    選択内容の保存ファイルがまだ無いとき、今の説明文を選択肢に復元するために使います。
    """
    body = text.strip()
    for preset in PRESETS.values():
        if compose(preset) == body:
            return normalize(preset)
    return None
