"""代表候補（variants best）のテスト。

既定では何も書かないこと、--adopt でも採用済み・代表なし・未評価には触らないことを確認します。
画像生成APIは呼びません。
"""

from __future__ import annotations

import argparse
import json

import pytest

from src import image_processor as ip
from src import scoring
from src import variants as vr
from src.main import build_parser, cmd_variants_best
from tests.test_scoring import sticker_like

IDS = ["001", "002", "003", "004", "005", "006"]


@pytest.fixture
def cfg(tmp_config):
    csv_path = tmp_config.root / "data" / "stickers.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    rows = "".join(f"{sid},セリフ{sid},手を振る,笑顔,basic\n" for sid in IDS)
    csv_path.write_text("id,text,action,expression,category\n" + rows, encoding="utf-8")
    tmp_config.raw["data"]["csv"] = "data/stickers.csv"
    return tmp_config


def _item(cfg, sid, vid, q=100, v=100, flags=(), verdict="pending", scored=True):
    ip.save_png(sticker_like(), vr.variant_dir(cfg, sid) / f"{vid}.png")
    item = {"variant_id": vid, "file": f"output/variants/{sid}/{vid}.png", "source": "api",
            "verdict": verdict, "human_rating": None, "note": "", "flags": list(flags)}
    if scored:
        item["derived_scores"] = {"quality": q, "visibility": v, "formula": scoring.SCORING_FORMULA}
    return item


def _write(cfg, stickers: dict):
    """{sid: (items, adopted)} を variants.json に書きます。"""
    cfg.variants_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.variants_path.write_text(json.dumps({"schema": 1, "stickers": {
        sid: {"adopted": adopted, "adopted_at": None, "next_seq": len(items) + 1, "variants": items}
        for sid, (items, adopted) in stickers.items()}}, ensure_ascii=False), encoding="utf-8")


def _args(*argv):
    return build_parser().parse_args(["variants", "best", *argv])


def _status(cfg, sid):
    return scoring.representative(cfg, vr.get_sticker(cfg, sid))


def _snapshot(cfg):
    files = sorted((str(p.relative_to(cfg.root)), p.read_bytes())
                   for p in (cfg.root / "output").rglob("*") if p.is_file())
    return files


# --- 選び方 -------------------------------------------------------------------
def test_picks_the_highest_score_without_gate(cfg):
    _write(cfg, {"001": ([_item(cfg, "001", "v001", 90, 90), _item(cfg, "001", "v002", 100, 95),
                          _item(cfg, "001", "v003", 100, 80, flags=["warn:fringe"])], None)})
    status, best = _status(cfg, "001")
    assert status == scoring.STATUS_BEST and best.variant_id == "v002"


def test_gated_candidate_is_never_picked_even_with_higher_score(cfg):
    _write(cfg, {"001": ([_item(cfg, "001", "v001", 100, 100, flags=["gate:cropped"]),
                          _item(cfg, "001", "v002", 40, 40, flags=["warn:thin"])], None)})
    status, best = _status(cfg, "001")
    assert status == scoring.STATUS_BEST and best.variant_id == "v002"


def test_rejected_or_regen_marked_candidates_are_not_picked(cfg):
    _write(cfg, {"001": ([_item(cfg, "001", "v001", 100, 100, verdict="rejected"),
                          _item(cfg, "001", "v002", 100, 100, verdict="regen"),
                          _item(cfg, "001", "v003", 50, 50)], None)})
    assert _status(cfg, "001")[1].variant_id == "v003"


def test_tie_is_broken_by_the_smaller_variant_id(cfg):
    _write(cfg, {"001": ([_item(cfg, "001", "v003"), _item(cfg, "001", "v001"),
                          _item(cfg, "001", "v002")], None),
                 "002": ([_item(cfg, "002", "v1000"), _item(cfg, "002", "v999")], None)})
    for _ in range(3):                                  # 何度呼んでも同じ
        assert _status(cfg, "001")[1].variant_id == "v001"
        assert _status(cfg, "002")[1].variant_id == "v999"   # 文字列順なら v1000 になってしまう


def test_detects_regen_required_unscored_and_no_candidates(cfg):
    _write(cfg, {
        "001": ([_item(cfg, "001", "v001", flags=["gate:too_small"]),
                 _item(cfg, "001", "v002", flags=["gate:cropped"])], None),
        "002": ([_item(cfg, "002", "v001"), _item(cfg, "002", "v002", scored=False)], None),
    })
    assert _status(cfg, "001") == (scoring.STATUS_REGEN_REQUIRED, None)
    assert _status(cfg, "002") == (scoring.STATUS_UNSCORED, None)    # 1件でも未評価なら決めない
    assert _status(cfg, "003") == (scoring.STATUS_NO_CANDIDATES, None)


def test_old_formula_counts_as_unscored(cfg):
    item = _item(cfg, "001", "v001")
    item["derived_scores"]["formula"] = "v0"
    _write(cfg, {"001": ([item], None)})
    assert _status(cfg, "001")[0] == scoring.STATUS_UNSCORED


def test_adopted_sticker_reports_adopted(cfg):
    _write(cfg, {"001": ([_item(cfg, "001", "v001", 50, 50), _item(cfg, "001", "v002")], "v001")})
    status, best = _status(cfg, "001")
    assert status == scoring.STATUS_ADOPTED and best.variant_id == "v002"   # 参考として表示だけ


# --- 既定では何も変えない -------------------------------------------------------
def _mixed(cfg):
    _write(cfg, {
        "001": ([_item(cfg, "001", "v001", 80, 80), _item(cfg, "001", "v002")], None),
        "002": ([_item(cfg, "002", "v001", flags=["gate:cropped"])], None),
        "003": ([_item(cfg, "003", "v001", scored=False)], None),
        "004": ([_item(cfg, "004", "v001")], "v001"),
    })


def test_default_lists_without_changing_anything(cfg, capsys):
    _mixed(cfg)
    before = _snapshot(cfg)
    assert cmd_variants_best(cfg, _args()) == 0
    assert cmd_variants_best(cfg, _args("--by-score")) == 0
    assert cmd_variants_best(cfg, _args("--ids", "001,002")) == 0
    assert _snapshot(cfg) == before
    out = capsys.readouterr().out
    for word in ("BEST", "REGEN_REQUIRED", "UNSCORED", "ADOPTED:v001", "NO_CANDIDATES"):
        assert word in out
    assert "BEST のID: 001" in out


# --- --adopt ----------------------------------------------------------------
def test_adopt_requires_explicit_ids(cfg, capsys):
    _mixed(cfg)
    before = _snapshot(cfg)
    assert cmd_variants_best(cfg, _args("--adopt")) == 1
    assert _snapshot(cfg) == before
    assert "--ids" in capsys.readouterr().err


def test_adopt_with_unknown_id_changes_nothing(cfg):
    _mixed(cfg)
    before = _snapshot(cfg)
    assert cmd_variants_best(cfg, _args("--adopt", "--ids", "001,999")) == 1
    assert _snapshot(cfg) == before


def test_adopt_only_adopts_best_and_skips_the_rest(cfg, capsys):
    _mixed(cfg)
    ip.save_png(sticker_like(), cfg.dir_generated / "004.png")    # 採用済みの原画（触ってはいけない）
    ip.save_png(sticker_like(), cfg.dir_final / "004.png")
    kept = {p: p.read_bytes() for p in (cfg.dir_generated / "004.png", cfg.dir_final / "004.png")}

    assert cmd_variants_best(cfg, _args("--adopt", "--ids", "001 002,003,004,005")) == 0

    state = json.loads(cfg.variants_path.read_text(encoding="utf-8"))["stickers"]
    assert state["001"]["adopted"] == "v002"                     # Gate無しの最高点
    assert (cfg.dir_final / "001.png").exists()
    assert state["002"]["adopted"] is None                       # 代表なし（Gate付きだけ）
    assert state["003"]["adopted"] is None                       # 未評価
    assert state["004"]["adopted"] == "v001"                     # 採用済みはそのまま
    assert all(p.read_bytes() == b for p, b in kept.items())
    assert "005" not in state                                    # 候補なし → 記録も作らない
    for sid in ("002", "003", "005"):
        assert not (cfg.dir_final / f"{sid}.png").exists()
    out = capsys.readouterr().out
    assert "001 ADOPTED v002" in out and "002 SKIP REGEN_REQUIRED" in out
    assert "004 SKIP ADOPTED" in out and "採用 1件 / 見送り 4件 / 失敗 0件" in out


def test_parse_ids_accepts_commas_and_spaces():
    from src.main import _parse_ids
    assert _parse_ids("001, 003 003,,005") == ["001", "003", "005"]
    assert _parse_ids(None) == []


def test_parser_has_no_adopt_by_default():
    args = _args()
    assert isinstance(args, argparse.Namespace) and args.adopt is False and args.ids is None
