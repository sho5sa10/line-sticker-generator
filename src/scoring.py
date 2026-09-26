"""候補画像の機械評価（Gate / Warn）。

人間の判断（verdict / human_rating / adopted）は一切変更しません。
候補PNGを読み取り、variants.json の raw_metrics / derived_scores / flags だけを更新します。

方針:
  - 生値（raw_metrics）を必ず保存し、閾値や重みを後から変えられるようにします。
  - 閾値は Phase 0 の実証で「壊れた画像を検出できた」ものだけを採用しています。
    実証していない指標（キャラクター一貫性・重複・意味の一致）は計算せず null のままにします。
  - 乱数・外部API・画像生成APIは使いません。同じ画像なら必ず同じ結果になります。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PIL import Image, ImageFilter, ImageStat

from . import image_processor as ip
from . import validator as vd
from . import variants as vr

# 式のバージョン。変えたら過去のスコアは再計算対象になります。
SCORING_FORMULA = "v1"

ANALYSIS_SIZE = 256      # 品質の計測に使う縮小サイズ（速度のため。結果は決定的）
SMALL_HEIGHT = 74        # LINE のタブ画像と同じ高さ。ここで読めるかを見ます

# --- 閾値（Phase 0 の実証にもとづく。実データの範囲は下のコメントを参照） -----
# 実データ100枚: coverage 0.387〜0.545 / fringe 0.021〜0.040 / colors 3,325〜5,976
#               ink74 0.406〜0.564 / contrast74 68.5〜82.0 / thin74 0.812〜0.896
GATE_COVERAGE = 0.05     # 実証: 1/6に縮小した画像が 0.013
GATE_CONTRAST74 = 30.0   # 実証: 低コントラスト画像が 16.97
WARN_COVERAGE = 0.12
WARN_FRINGE = 0.20       # 実証: にじみ画像が 0.152
WARN_COLORS = 8000       # 実証: ノイズ画像が 16,245
WARN_THIN74 = 0.60
WARN_MARGIN_PCT = 0.02


class ScoringError(Exception):
    """候補を評価できない場合（画像が無い・壊れている）に送出されます。"""


# ---------------------------------------------------------------------------
# 生値の計測
# ---------------------------------------------------------------------------
def measure(path: str | Path) -> dict:
    """候補PNG1枚の生値を測ります（既存の画像処理・検証関数を再利用）。"""
    p = Path(path)
    if not p.exists():
        raise ScoringError(f"候補の画像がありません: {p}")
    try:
        full = ip.load_rgba(p)
    except Exception as exc:  # noqa: BLE001 - 破損画像の例外は多岐にわたる
        raise ScoringError(f"候補の画像を読めません: {p} ({type(exc).__name__}: {exc})") from exc

    metrics = {"cropped_sides": ip.cropped_sides(full)}   # 既存の端切れ判定
    metrics.update(_quality_metrics(full))
    metrics.update(_small_size_metrics(full))
    return metrics


def _analysis_copy(img: Image.Image) -> Image.Image:
    small = img.copy()
    if max(small.size) > ANALYSIS_SIZE:
        small.thumbnail((ANALYSIS_SIZE, ANALYSIS_SIZE), Image.LANCZOS)
    return small


def _quality_metrics(full: Image.Image) -> dict:
    img = _analysis_copy(full)
    w, h = img.size
    alpha = img.getchannel("A")
    opaque = sum(1 for a in alpha.getdata() if a > ip.ALPHA_THRESHOLD)
    coverage = opaque / (w * h)
    if opaque == 0:
        return {"coverage": 0.0, "fringe": 0.0, "colors": 0, "min_margin_pct": 0.0, "blank": True}

    # 余白（既存の validator.measure_margins をそのまま使用）
    margins = vd.measure_margins(img) or (0, 0, 0, 0)
    # 半透明のふち（にじみ）の割合
    fringe = sum(1 for a in alpha.getdata() if ip.ALPHA_THRESHOLD < a < 200) / opaque
    # 色数（フラットなイラスト前提。多すぎ＝グラデ/ノイズ）
    mask = alpha.point(lambda v: 255 if v > ip.ALPHA_THRESHOLD else 0)
    flat = Image.new("RGB", img.size, (255, 255, 255))
    flat.paste(img.convert("RGB"), mask=mask)
    colors = len(flat.getcolors(maxcolors=100000) or [])

    return {
        "coverage": round(coverage, 4),
        "fringe": round(fringe, 4),
        "colors": colors,
        "min_margin_pct": round(min(margins) / min(w, h), 4),
        "blank": False,
    }


def _small_size_metrics(full: Image.Image) -> dict:
    """LINE 上の小ささ（74px）で見たときの読みやすさ。"""
    img = full.copy()
    img.thumbnail((max(int(SMALL_HEIGHT * full.width / full.height), 1), SMALL_HEIGHT),
                  Image.LANCZOS)
    alpha = img.getchannel("A")
    mask = alpha.point(lambda v: 255 if v > ip.ALPHA_THRESHOLD else 0)
    opaque = sum(1 for a in mask.getdata() if a)
    if opaque == 0:
        return {"ink74": 0.0, "edge74": 0.0, "contrast74": 0.0, "thin74": 0.0}

    # 明るい背景（LINEのトーク画面）に置いたときの見え方で測ります
    onwhite = Image.new("RGB", img.size, (255, 255, 255))
    onwhite.paste(img.convert("RGB"), mask=mask)
    gray = onwhite.convert("L")
    edge = ImageStat.Stat(gray.filter(ImageFilter.FIND_EDGES)).mean[0]
    contrast = ImageStat.Stat(gray, mask).stddev[0]
    # 細すぎる線: 1px 削って残る割合（低い＝細い線ばかりで潰れやすい）
    eroded = mask.filter(ImageFilter.MinFilter(3))
    survive = sum(1 for a in eroded.getdata() if a) / opaque
    return {
        "ink74": round(opaque / (img.width * img.height), 4),
        "edge74": round(edge, 2),
        "contrast74": round(contrast, 2),
        "thin74": round(survive, 4),
    }


# ---------------------------------------------------------------------------
# Gate / Warn と点数
# ---------------------------------------------------------------------------
def evaluate(metrics: dict) -> tuple[dict, list[str]]:
    """生値から flags と点数を出します（同じ生値なら必ず同じ結果）。"""
    flags: list[str] = []

    # --- Gate: 機械的に見て、そのままでは使えない ---
    if metrics.get("blank") or metrics.get("coverage", 0) <= 0:
        flags.append("gate:empty")
    elif metrics.get("coverage", 0) < GATE_COVERAGE:
        flags.append("gate:too_small")
    if metrics.get("cropped_sides"):
        flags.append("gate:cropped")
    if not metrics.get("blank") and metrics.get("contrast74", 0) < GATE_CONTRAST74:
        flags.append("gate:low_contrast")

    # --- Warn: 人が見て判断する ---
    if "gate:empty" not in flags and "gate:too_small" not in flags:
        if metrics.get("coverage", 0) < WARN_COVERAGE:
            flags.append("warn:low_coverage")
        if metrics.get("fringe", 0) > WARN_FRINGE:
            flags.append("warn:fringe")
        if metrics.get("colors", 0) > WARN_COLORS:
            flags.append("warn:many_colors")
        if metrics.get("thin74", 1) < WARN_THIN74:
            flags.append("warn:thin")
        if ("gate:cropped" not in flags
                and metrics.get("min_margin_pct", 1) < WARN_MARGIN_PCT):
            flags.append("warn:small_margin")

    quality = 0 if "gate:empty" in flags else _score_from(flags, {
        "gate:too_small": 60, "gate:cropped": 35,
        "warn:low_coverage": 15, "warn:fringe": 10, "warn:many_colors": 10,
        "warn:small_margin": 8,
    })
    visibility = 0 if "gate:empty" in flags else _score_from(flags, {
        "gate:low_contrast": 60, "warn:thin": 20, "gate:too_small": 30,
    })
    return ({
        "quality": quality,
        "visibility": visibility,
        # 以下は Phase 3 では計算しません（実証が不足しているため数字を作りません）
        "consistency": None,
        "duplication": None,
        "semantic": None,
        "formula": SCORING_FORMULA,
        "scored_at": datetime.now().isoformat(timespec="seconds"),
    }, flags)


def _score_from(flags: list[str], penalties: dict[str, int]) -> int:
    return max(0, 100 - sum(p for f, p in penalties.items() if f in flags))


def gates(flags) -> list[str]:
    return [f for f in flags if f.startswith("gate:")]


def warns(flags) -> list[str]:
    return [f for f in flags if f.startswith("warn:")]


# ---------------------------------------------------------------------------
# 候補の評価と保存
# ---------------------------------------------------------------------------
def needs_scoring(item: dict, *, force: bool = False) -> bool:
    """再計算が必要か（未計算 / 式が古い / 強制）。"""
    if force:
        return True
    scores = item.get("derived_scores")
    if not isinstance(scores, dict):
        return True
    return scores.get("formula") != SCORING_FORMULA


def score_sticker(config, sticker_id: str, data: dict, *, force: bool = False) -> list[dict]:
    """1スタンプ分の候補を評価して記録に書き込みます（保存は呼び出し側）。"""
    sticker = vr.get_sticker(config, sticker_id, data)
    if sticker is None:
        return []
    record = vr.ensure_record(config, data, sticker_id)
    results: list[dict] = []
    for item in record.get("variants", []):
        if not isinstance(item, dict):
            continue
        variant_id = str(item.get("variant_id", ""))
        if not needs_scoring(item, force=force):
            results.append({"id": sticker_id, "variant_id": variant_id, "status": "skipped",
                            "scores": item.get("derived_scores"), "flags": item.get("flags") or []})
            continue
        variant = sticker.find(variant_id)
        try:
            metrics = measure(variant.path(config))
        except ScoringError as exc:
            # 画像が無い・壊れているときは記録を変えません（人が対処します）
            results.append({"id": sticker_id, "variant_id": variant_id, "status": "error",
                            "detail": str(exc), "scores": None, "flags": []})
            continue
        scores, flags = evaluate(metrics)
        item["raw_metrics"] = metrics
        item["derived_scores"] = scores
        item["flags"] = flags
        results.append({"id": sticker_id, "variant_id": variant_id, "status": "scored",
                        "scores": scores, "flags": flags})
    return results


def score_all(config, sticker_ids, *, force: bool = False, data: dict | None = None) -> list[dict]:
    """複数スタンプを評価し、variants.json を既存の保存方式で更新します。"""
    state = vr.load(config) if data is None else data
    results: list[dict] = []
    changed = False
    for sticker_id in sticker_ids:
        part = score_sticker(config, sticker_id, state, force=force)
        results.extend(part)
        changed = changed or any(r["status"] == "scored" for r in part)
    if changed:
        vr.save(config, state)
    return results
