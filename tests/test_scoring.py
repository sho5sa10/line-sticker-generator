"""候補の機械評価（Phase 3: Gate / Warn）のテスト。

画像生成APIは呼びません。閾値は Phase 0 の実証で検出できた劣化だけを対象にしています。
最重要は「評価しても画像・採用状態・人の判断が一切変わらない」ことです。
"""

from __future__ import annotations

import hashlib
import json

import pytest
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

from src import image_processor as ip
from src import scoring
from src import variants as vr
from tests.conftest import make_character


def sticker_like(size=(512, 512), body=(250, 205, 60, 255)) -> Image.Image:
    """実際のスタンプに近いテスト画像（太い輪郭＋顔）。

    conftest の make_character は単色の楕円で、輪郭が無いため縮小時のコントラストが
    ほぼ 0 になります（実データは 68〜82）。機械評価の確認には向かないので、
    ここでは輪郭と目・口のある画像を使います。
    """
    w, h = size
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    outline = (40, 30, 25, 255)
    d.ellipse((w * 0.18, h * 0.12, w * 0.82, h * 0.88), fill=body, outline=outline,
              width=int(w * 0.035))
    d.ellipse((w * 0.36, h * 0.38, w * 0.44, h * 0.48), fill=outline)
    d.ellipse((w * 0.56, h * 0.38, w * 0.64, h * 0.48), fill=outline)
    d.arc((w * 0.42, h * 0.52, w * 0.58, h * 0.64), 200, 340, fill=outline, width=int(w * 0.02))
    d.ellipse((w * 0.26, h * 0.52, w * 0.34, h * 0.58), fill=(245, 150, 140, 255))
    d.ellipse((w * 0.66, h * 0.52, w * 0.74, h * 0.58), fill=(245, 150, 140, 255))
    return img


# --- 劣化画像（Phase 0 の実証で使ったものと同じ作り方） ----------------------
def degrade(img: Image.Image, kind: str) -> Image.Image:
    if kind == "blank":
        return Image.new("RGBA", img.size, (0, 0, 0, 0))
    if kind == "tiny":
        small = img.copy()
        small.thumbnail((img.width // 6, img.height // 6))
        out = Image.new("RGBA", img.size, (0, 0, 0, 0))
        out.alpha_composite(small, (img.width // 2, img.height // 2))
        return out
    if kind == "cropped":
        big = img.resize((int(img.width * 1.6), int(img.height * 1.6)), Image.LANCZOS)
        left = (big.width - img.width) // 2
        top = int(big.height * 0.25)
        return big.crop((left, top, left + img.width, top + img.height))
    if kind == "lowcontrast":
        rgb = ImageEnhance.Contrast(img.convert("RGB")).enhance(0.18)
        rgb = ImageEnhance.Brightness(rgb).enhance(1.35)
        out = rgb.convert("RGBA")
        out.putalpha(img.getchannel("A"))
        return out
    if kind == "fringe":
        a = img.getchannel("A").filter(ImageFilter.GaussianBlur(14)).point(lambda v: min(v, 120))
        base = Image.new("RGBA", img.size, (60, 40, 20, 0))
        base.putalpha(a)
        base.alpha_composite(img)
        return base
    if kind == "noisy":
        grad = Image.linear_gradient("L").resize(img.size)
        rgb = Image.merge("RGB", (grad, grad.transpose(Image.ROTATE_90),
                                  grad.point(lambda v: 255 - v)))
        blended = Image.blend(img.convert("RGB"), rgb, 0.45)
        out = blended.convert("RGBA")
        out.putalpha(img.getchannel("A"))
        return out
    raise ValueError(kind)


def _put(cfg, sid, vid, img=None, kind=None):
    base = img or sticker_like()
    if kind:
        base = degrade(base, kind)
    path = vr.variant_dir(cfg, sid) / f"{vid}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    base.save(path, format="PNG")
    return path


def _state_file(cfg, sid="001", vids=("v001",), adopted=None, extra=None):
    items = []
    for vid in vids:
        item = {"variant_id": vid, "file": f"output/variants/{sid}/{vid}.png",
                "source": "api", "verdict": "pending", "human_rating": None, "note": ""}
        item.update((extra or {}).get(vid, {}))
        items.append(item)
    cfg.variants_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.variants_path.write_text(json.dumps({
        "schema": 1, "stickers": {sid: {"adopted": adopted, "adopted_at": None,
                                        "next_seq": len(vids) + 1, "variants": items}}},
        ensure_ascii=False), encoding="utf-8")


def _state(cfg) -> dict:
    return json.loads(cfg.variants_path.read_text(encoding="utf-8"))


def _item(cfg, idx=0, sid="001") -> dict:
    return _state(cfg)["stickers"][sid]["variants"][idx]


def _sha1(path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


# --- 基本 (1〜6) -----------------------------------------------------------
def test_scores_a_normal_image(tmp_config):
    _put(tmp_config, "001", "v001")
    _state_file(tmp_config)

    results = scoring.score_all(tmp_config, ["001"])
    assert [r["status"] for r in results] == ["scored"]

    item = _item(tmp_config)
    m = item["raw_metrics"]
    assert 0 < m["coverage"] < 1 and m["colors"] > 0
    assert m["cropped_sides"] == [] and "contrast74" in m and "thin74" in m
    s = item["derived_scores"]
    assert s["quality"] == 100 and s["visibility"] == 100
    assert s["formula"] == scoring.SCORING_FORMULA == "v1"
    assert s["scored_at"].startswith("20")
    assert s["consistency"] is None and s["duplication"] is None and s["semantic"] is None
    assert item["flags"] == []


# --- Gate (7〜10) ----------------------------------------------------------
@pytest.mark.parametrize("kind,flag", [
    ("blank", "gate:empty"),
    ("tiny", "gate:too_small"),
    ("cropped", "gate:cropped"),
    ("lowcontrast", "gate:low_contrast"),
])
def test_broken_images_get_gate(tmp_config, kind, flag):
    _put(tmp_config, "001", "v001", kind=kind)
    _state_file(tmp_config)
    scoring.score_all(tmp_config, ["001"])

    item = _item(tmp_config)
    assert flag in item["flags"], item["flags"]
    assert scoring.gates(item["flags"])
    # Gate が付いても候補ファイルは残り、人の判断も採用状態も変わりません
    assert (tmp_config.dir_variants / "001" / "v001.png").exists()
    assert item["verdict"] == "pending"
    assert _state(tmp_config)["stickers"]["001"]["adopted"] is None


def test_gate_lowers_scores(tmp_config):
    _put(tmp_config, "001", "v001", kind="blank")
    _state_file(tmp_config)
    scoring.score_all(tmp_config, ["001"])
    s = _item(tmp_config)["derived_scores"]
    assert s["quality"] == 0 and s["visibility"] == 0


# --- Warn (11〜13) ---------------------------------------------------------
@pytest.mark.parametrize("kind,flag", [("fringe", "warn:fringe"), ("noisy", "warn:many_colors")])
def test_warn_conditions(tmp_config, kind, flag):
    _put(tmp_config, "001", "v001", kind=kind)
    _state_file(tmp_config, extra={"v001": {"verdict": "pending", "human_rating": 3}})
    scoring.score_all(tmp_config, ["001"])

    item = _item(tmp_config)
    assert flag in item["flags"]
    assert not scoring.gates(item["flags"])          # Warn だけなら Gate は付かない
    assert item["verdict"] == "pending"              # 12, 13: 候補も判断も残る
    assert item["human_rating"] == 3
    assert (tmp_config.dir_variants / "001" / "v001.png").exists()
    assert item["derived_scores"]["quality"] < 100   # 減点はされる


def test_warn_thresholds_are_not_triggered_by_normal_images(tmp_config):
    _put(tmp_config, "001", "v001")
    _state_file(tmp_config)
    scoring.score_all(tmp_config, ["001"])
    assert _item(tmp_config)["flags"] == []


# --- 不変条件 (14〜19) -----------------------------------------------------
def test_scoring_changes_nothing_but_scores(tmp_config):
    png = _put(tmp_config, "001", "v001")
    ip.save_png(make_character((512, 512)), tmp_config.dir_generated / "001.png")
    ip.save_png(make_character((370, 320)), tmp_config.dir_final / "001.png")
    _state_file(tmp_config, vids=("v001",), adopted="v001",
                extra={"v001": {"verdict": "rejected", "human_rating": 2, "note": "メモ"}})

    gen, fin = tmp_config.dir_generated / "001.png", tmp_config.dir_final / "001.png"
    before = (_sha1(png), png.stat().st_mtime_ns, _sha1(gen), gen.stat().st_mtime_ns,
              _sha1(fin), fin.stat().st_mtime_ns)

    scoring.score_all(tmp_config, ["001"])

    assert (_sha1(png), png.stat().st_mtime_ns, _sha1(gen), gen.stat().st_mtime_ns,
            _sha1(fin), fin.stat().st_mtime_ns) == before
    record = _state(tmp_config)["stickers"]["001"]
    assert record["adopted"] == "v001"               # 17
    assert record["variants"][0]["verdict"] == "rejected"    # 18
    assert record["variants"][0]["human_rating"] == 2        # 19
    assert record["variants"][0]["note"] == "メモ"
    assert "derived_scores" in record["variants"][0]


def test_state_json_is_not_written_when_nothing_changes(tmp_config):
    _put(tmp_config, "001", "v001")
    _state_file(tmp_config)
    scoring.score_all(tmp_config, ["001"])
    before = tmp_config.variants_path.stat().st_mtime_ns
    scoring.score_all(tmp_config, ["001"])           # 2回目は計算済みなので保存しない
    assert tmp_config.variants_path.stat().st_mtime_ns == before


# --- 再計算 (20〜22) -------------------------------------------------------
def test_old_formula_is_rescored(tmp_config):
    _put(tmp_config, "001", "v001")
    _state_file(tmp_config, extra={"v001": {
        "derived_scores": {"quality": 1, "formula": "v0"}, "flags": ["warn:古い"]}})
    results = scoring.score_all(tmp_config, ["001"])
    assert [r["status"] for r in results] == ["scored"]
    item = _item(tmp_config)
    assert item["derived_scores"]["formula"] == "v1" and item["derived_scores"]["quality"] == 100
    assert item["flags"] == []


def test_force_rescores_and_is_deterministic(tmp_config):
    _put(tmp_config, "001", "v001")
    _state_file(tmp_config)
    scoring.score_all(tmp_config, ["001"])
    first = _item(tmp_config)

    results = scoring.score_all(tmp_config, ["001"], force=True)
    assert [r["status"] for r in results] == ["scored"]
    second = _item(tmp_config)
    assert second["raw_metrics"] == first["raw_metrics"]        # 22: 同じ画像なら同じ生値
    assert second["derived_scores"]["quality"] == first["derived_scores"]["quality"]
    assert second["flags"] == first["flags"]


def test_needs_scoring_rules(tmp_config):
    assert scoring.needs_scoring({}) is True
    assert scoring.needs_scoring({"derived_scores": {"formula": "v0"}}) is True
    assert scoring.needs_scoring({"derived_scores": {"formula": "v1"}}) is False
    assert scoring.needs_scoring({"derived_scores": {"formula": "v1"}}, force=True) is True


# --- エラー (23〜25) -------------------------------------------------------
def test_missing_png_is_reported_without_changing_state(tmp_config):
    _state_file(tmp_config)                          # PNG を作らない
    results = scoring.score_all(tmp_config, ["001"])
    assert results[0]["status"] == "error" and "ありません" in results[0]["detail"]
    assert "derived_scores" not in _item(tmp_config)


def test_corrupt_png_is_reported_without_changing_state(tmp_config):
    path = _put(tmp_config, "001", "v001")
    path.write_bytes(b"not a png")
    _state_file(tmp_config)
    results = scoring.score_all(tmp_config, ["001"])
    assert results[0]["status"] == "error" and "読めません" in results[0]["detail"]
    assert "derived_scores" not in _item(tmp_config)


def test_broken_state_file_is_not_overwritten_by_scoring(tmp_config):
    """Phase 5b(C2) で変更: 壊れた記録の上にスコアを書くと、それまでの記録を失うため断ります。"""
    ip.save_png(make_character((512, 512)), tmp_config.dir_generated / "001.png")
    tmp_config.variants_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_config.variants_path.write_text("{壊れている", encoding="utf-8")
    broken = tmp_config.variants_path.read_text(encoding="utf-8")

    with pytest.raises(vr.StateCorruptError, match="壊れて"):
        scoring.score_all(tmp_config, ["001"])

    assert tmp_config.variants_path.read_text(encoding="utf-8") == broken

    # 退避すれば、legacy の候補（generated/001.png）を評価できる
    vr.quarantine_corrupt_state(tmp_config)
    results = scoring.score_all(tmp_config, ["001"])
    assert [r["status"] for r in results] == ["scored"]
    record = _state(tmp_config)["stickers"]["001"]
    assert record["variants"][0]["source"] == "legacy"
    assert record["variants"][0]["derived_scores"]["formula"] == "v1"


def test_sticker_without_candidates_is_skipped(tmp_config):
    assert scoring.score_all(tmp_config, ["001"]) == []
    assert not tmp_config.variants_path.exists()


# --- 生値と判定の切り分け ---------------------------------------------------
def test_evaluate_is_pure_and_threshold_driven():
    """生値だけから判定できる（画像を再度読まない）ことの確認。"""
    good = {"coverage": 0.45, "fringe": 0.02, "colors": 4300, "min_margin_pct": 0.06,
            "cropped_sides": [], "ink74": 0.47, "contrast74": 75.0, "thin74": 0.88,
            "blank": False}
    scores, flags = scoring.evaluate(good)
    assert flags == [] and scores["quality"] == 100

    bad = dict(good, contrast74=16.9, thin74=0.4, fringe=0.3, colors=16000)
    scores, flags = scoring.evaluate(bad)
    assert scoring.gates(flags) == ["gate:low_contrast"]
    assert set(scoring.warns(flags)) == {"warn:fringe", "warn:many_colors", "warn:thin"}
    assert scores["visibility"] < 100 and scores["quality"] < 100
    # 何度呼んでも同じ（scored_at 以外）
    again, flags2 = scoring.evaluate(bad)
    assert flags2 == flags and again["quality"] == scores["quality"]
