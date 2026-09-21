"""キャラクターの絵に合わせて、セリフの文字スタイル（色・フォント）を提案します。

- 色: キャラクター画像から「線の色」「体の色」「差し色」を読み取ります。
- フォント: かんたん入力で選んだ雰囲気（かわいい・コミカル・上品など）から選びます。

提案は「候補」を返すだけで、設定は変更しません（画面で選んで保存します）。
"""

from __future__ import annotations

import colorsys
from pathlib import Path

from PIL import Image

from . import character_profile as cprof
from .fonts import FontInfo, available_fonts

# ---------------------------------------------------------------------------
# 色の読み取り
# ---------------------------------------------------------------------------
RGB = tuple[int, int, int]


def _hex(c: RGB) -> str:
    return "#{:02X}{:02X}{:02X}".format(*c)


def _luminance(c: RGB) -> float:
    def ch(v: int) -> float:
        v = v / 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = c
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast_ratio(a: RGB, b: RGB) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _hsv(c: RGB) -> tuple[float, float, float]:
    return colorsys.rgb_to_hsv(*(v / 255 for v in c))


def color_name(c: RGB) -> str:
    """だいたいの色の名前（提案理由の説明用）。"""
    h, s, v = _hsv(c)
    if v < 0.25:
        return "黒"
    if s < 0.15:
        return "白" if v > 0.85 else "グレー"
    if v < 0.55 and (h < 0.11 or h > 0.95):
        return "焦げ茶"
    deg = h * 360
    for limit, name in ((15, "赤"), (40, "オレンジ"), (65, "黄"), (90, "黄緑"), (160, "緑"),
                        (200, "水色"), (250, "青"), (290, "紫"), (345, "ピンク"), (361, "赤")):
        if deg < limit:
            return name
    return "色"


def _mean(pixels: list[RGB]) -> RGB:
    n = len(pixels)
    return tuple(round(sum(p[i] for p in pixels) / n) for i in range(3))  # type: ignore[return-value]


HUE_BIN = 20  # 色相を20度ごとに数えます


def analyze_character(img: Image.Image) -> dict:
    """キャラクターの「線の色」「体の色」「差し色」を推定します。

    面積の大きい色が何色にも分かれて小さな差し色が埋もれないよう、
    色の減らし方ではなく「色相（色味）ごとの面積」で判断します。
    """
    small = img.convert("RGBA")
    small.thumbnail((240, 240))
    pixels = [p[:3] for p in small.getdata() if p[3] > 200]
    default = {"outline": (34, 34, 34), "body": (255, 255, 255), "accent": None, "accents": []}
    if not pixels:
        return default
    total = len(pixels)
    hsv = [(p, _hsv(p)) for p in pixels]

    darks = [p for p, (h, s, v) in hsv if v < 0.3]
    outline = _mean(darks) if len(darks) >= total * 0.02 else (34, 34, 34)

    # 鮮やかな色を色相ごとに集計
    bins: dict[int, list[RGB]] = {}
    for p, (h, s, v) in hsv:
        if s >= 0.35 and v >= 0.45:
            bins.setdefault(int(h * 360) // HUE_BIN, []).append(p)
    ranked = sorted(bins.items(), key=lambda kv: -len(kv[1]))

    if ranked and len(ranked[0][1]) >= total * 0.10:
        _, body_pixels = ranked[0]
        body = _mean(body_pixels)
        others = ranked[1:]
    else:  # 白いキャラなど、鮮やかな色が少ない場合
        lights = [p for p, (h, s, v) in hsv if v >= 0.8 and s < 0.2]
        body = _mean(lights) if lights else (255, 255, 255)
        others = ranked

    # 差し色: 体の色と違う色味で、ある程度の面積があるもの（多い順に2つまで）
    accents = [_mean(px) for _, px in others if len(px) >= total * 0.01][:2]
    return {"outline": outline, "body": body,
            "accent": accents[0] if accents else None, "accents": accents}


def _source_image(config) -> Path | None:
    """色を読み取る画像。マスター画像を優先し、無ければ最初の原画。"""
    if config.master_image_path.exists():
        return config.master_image_path
    generated = sorted(config.dir_generated.glob("*.png"))
    return generated[0] if generated else None


# ---------------------------------------------------------------------------
# フォントの選び方
# ---------------------------------------------------------------------------
def mood_font_tag(profile: dict | None) -> tuple[str, str]:
    """雰囲気からフォントの系統を決めます。(タグ, 理由)"""
    p = profile or {}
    moods = set(p.get("mood") or [])
    if moods & {"コミカル", "元気"}:
        return "impact", "雰囲気が「" + "・".join(sorted(moods & {"コミカル", "元気"})) + "」なので、太くて目立つフォント"
    if "上品" in moods:
        return "elegant", "雰囲気が「上品」なので明朝体"
    if moods & {"かわいい", "ゆるい", "やさしい"}:
        return "gentle", "雰囲気が「" + "・".join(sorted(moods & {"かわいい", "ゆるい", "やさしい"})) + "」なので、手書き風のやさしいフォント"
    if moods & {"クール", "まじめ"}:
        return "cool", "雰囲気が「" + "・".join(sorted(moods & {"クール", "まじめ"})) + "」なので、すっきりしたフォント"
    if p.get("kind") in ("動物", "ふしぎな生き物", "食べ物"):
        return "gentle", "動物などのキャラなので、手書き風のやさしいフォント"
    return "standard", "読みやすい定番のフォント"


def _pick_font(fonts: list[FontInfo], tag: str) -> FontInfo | None:
    return next((f for f in fonts if tag in f.tags), None) or (fonts[0] if fonts else None)


_STROKE = {"impact": 8, "elegant": 8, "gentle": 7, "cool": 7, "standard": 7, "custom": 7}


# ---------------------------------------------------------------------------
# 提案
# ---------------------------------------------------------------------------
def suggest_styles(config) -> dict:
    """キャラクターに合う文字スタイルの候補を返します。"""
    fonts = available_fonts(config)
    profile = cprof.load_profile(config.character_profile_path)
    tag, font_reason = mood_font_tag(profile)
    main_font = _pick_font(fonts, tag)
    standard_font = _pick_font(fonts, "standard")

    src = _source_image(config)
    if src:
        with Image.open(src) as im:
            palette = analyze_character(im)
    else:
        palette = {"outline": (34, 34, 34), "body": (255, 255, 255), "accent": None}
    outline, body, accent = palette["outline"], palette["body"], palette["accent"]
    white: RGB = (255, 255, 255)

    def entry(name, fill, stroke, font, reason):
        return {
            "name": name,
            "fill": _hex(fill),
            "stroke_fill": _hex(stroke),
            "font_id": font.id if font else None,
            "font_label": font.label if font else "",
            "stroke_width": _STROKE.get(tag if font is main_font else "standard", 7),
            "reason": reason,
        }

    candidates = []
    # 1) キャラの体の色 × キャラの線の色
    if contrast_ratio(body, outline) >= 3 and body != white:
        candidates.append(entry(
            "キャラの体の色", body, outline, main_font,
            f"体の{color_name(body)}（{_hex(body)}）を文字に、線の{color_name(outline)}"
            f"（{_hex(outline)}）を縁取りに使いました。{font_reason}にしています。"))
    # 2) 差し色 × キャラの線の色（2色まで）
    for acc in palette.get("accents") or ([accent] if accent else []):
        if contrast_ratio(acc, outline) >= 3:
            candidates.append(entry(
                f"差し色（{color_name(acc)}）", acc, outline, main_font,
                f"キャラの中の差し色の{color_name(acc)}（{_hex(acc)}）を文字に使いました。"
                f"目を引きやすい組み合わせです。{font_reason}にしています。"))
    # 3) 白 × キャラの線の色（定番）
    candidates.append(entry(
        "白文字（定番）", white, outline, main_font,
        f"白い文字に、キャラの線と同じ{color_name(outline)}の縁取り。"
        f"どんな絵にもなじみ、いちばん読みやすい組み合わせです。{font_reason}にしています。"))
    # 4) キャラの線の色の文字 × 白い縁取り
    candidates.append(entry(
        "濃い文字＋白い縁", outline, white, standard_font,
        f"キャラの線の{color_name(outline)}を文字に、白を縁取りにしました。"
        "落ち着いた印象で、定番の読みやすいフォントにしています。"))

    return {
        "source": src.name if src else None,
        "palette": {
            "outline": _hex(outline), "body": _hex(body),
            "accents": [_hex(a) for a in palette.get("accents") or []],
        },
        "font_reason": font_reason,
        "suggestions": candidates,
    }
