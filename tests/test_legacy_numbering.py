"""原画（legacy）の取り込みと、止まった初回生成・予約済みの番号（Phase 7 STEP 3-A・C1 / D1 / C2）。

- 初回生成の途中（initial_run がある）の記録には、原画を候補として取り込まない（見せもしない）
- 原画を置く番号は、予約済みの番号（next_seq より前・候補・実行の記録の予約と完了）と
  書き込み途中の .png.part がある番号を避ける（ファイルが無いだけで空いているとは限らない）
- 既に記録にある原画の番号・採用の情報は変えない
画像生成APIは呼びません（偽のプロバイダ。通信も遮断します。fixture は test_initial_candidates と共通）。
"""

from __future__ import annotations

import pytest

pytest.importorskip("flask", reason="Flask が未インストールです")

from src import variants as vr  # noqa: E402
from tests.test_initial_candidates import (  # noqa: E402,F401 - fixture も読み込む
    GUI, _crash, _entry, _generator, _ids, _plan, _png, _put_run, _record, _run, box, client,
)
from tests.test_m3_cli_gui_concurrency import _cli, _cli_calls, _finish  # noqa: E402

YELLOW = (250, 205, 60, 255)


def _original(cfg, sid="001"):
    """原画（generated/<id>.png）を置き、その SHA1 を返す。"""
    path = cfg.dir_generated / f"{sid}.png"
    path.write_bytes(_png(YELLOW))
    return vr.sha1_file(path)


def _put_record(cfg, sid="001", **record):
    vr.update(cfg, lambda s: s.setdefault("stickers", {}).update(
        {sid: {"adopted": None, "adopted_at": None, "variants": [], **record}}))


def _item(cfg, vid, sid="001"):
    return next(v for v in _record(cfg, sid)["variants"] if v["variant_id"] == vid)


def _shas(cfg, sid="001"):
    return {v["variant_id"]: vr.sha1_file(cfg.root / v["file"]) for v in _record(cfg, sid)["variants"]}


def _row(client, sid="001"):
    return next(s for s in client.get("/api/stickers").get_json()["stickers"] if s["id"] == sid)


# ===========================================================================
# C1: .png.part（課金済みの画像）が、原画に隠されない
# ===========================================================================
def test_c1_part_file_is_not_hidden_by_the_original(client, tmp_config, box):
    """初回生成が .part を受け取った直後に落ち → 原画ができ → CLI の候補生成 → 初回生成を再開。"""
    assert _crash(tmp_config, "provider_ok", 1) == 1               # 実際の子プロセスを os._exit で止める
    folder = vr.variant_dir(tmp_config, "001")
    part = folder / "v001.png.part"
    assert part.exists() and not (folder / "v001.png").exists()
    part_sha = vr.sha1_file(part)
    original_sha = _original(tmp_config)                          # 「AIで作り直す」などで原画ができた

    rc, out, err = _finish(_cli(tmp_config, count=1))               # CLI の候補生成（実プロセス）
    assert rc == 0, err
    assert _cli_calls(tmp_config) == 1
    record = _record(tmp_config)
    assert _ids(tmp_config) == ["v002"]                            # 原画を v001 に取り込まない
    assert record["adopted"] is None and "adopted_file" not in record
    assert record["initial_run"]["reserved"] == ["v001"]          # 止まった初回生成はそのまま
    assert part.exists() and not (folder / "v001.png").exists()   # .part は隠されていない

    plan = _plan(client, ["001"], 4).get_json()
    assert [(t["count"], t["resume"], t["recovered"]) for t in plan["targets"]] == [(0, True, 1)]
    calls = box["calls"]
    r = _run(client, ["001"], 4)
    assert r.status_code == 200, r.get_json()
    assert box["calls"] == calls                                   # API を呼び直さない
    shas = _shas(tmp_config)
    assert sorted(shas) == ["v001", "v002"]
    assert shas["v001"] == part_sha != original_sha                # v001 は課金済みの画像（原画ではない）
    assert original_sha not in shas.values()                       # 原画は候補に入らない
    assert not part.exists() and vr._is_complete_png(folder / "v001.png")
    record = _record(tmp_config)
    assert "initial_run" not in record and record["adopted"] is None and record["next_seq"] >= 3


# ===========================================================================
# D1: 止まった初回生成（前回できた画像なし）＋原画 → 再開しても原画を取り込まない
# ===========================================================================
def test_d1_resuming_a_stopped_run_does_not_take_in_the_original(client, tmp_config, box):
    _put_run(tmp_config, count=2, owner=None, reserved=["v001", "v002"])   # 候補0件・next_seq=3
    original_sha = _original(tmp_config)
    row = _row(client)
    assert (row["variant_count"], row["has_raw"], row["initial_run"]) == (0, True, "pending")
    assert client.get("/api/variants/001").get_json()["variant_count"] == 0     # 原画を候補として見せない

    plan = _plan(client, ["001"], 4).get_json()
    assert [(t["count"], t["resume"]) for t in plan["targets"]] == [(2, True)]
    calls = box["calls"]
    _run(client, ["001"], 4)
    assert box["calls"] - calls == 2
    shas = _shas(tmp_config)
    assert sorted(shas) == ["v003", "v004"]                        # 予約済みの v001・v002 は使わない
    assert original_sha not in shas.values()
    record = _record(tmp_config)
    assert record["adopted"] is None and "adopted_file" not in record
    assert "initial_run" not in record and record["next_seq"] == 5


# ===========================================================================
# C2: 予約後に失敗した跡（next_seq=5・ファイルなし）＋原画 → 原画は v005
# ===========================================================================
def test_c2_original_does_not_reuse_reserved_numbers(client, tmp_config, box):
    _put_record(tmp_config, next_seq=5)
    original_sha = _original(tmp_config)
    shown = client.get("/api/variants/001").get_json()
    assert shown["legacy"] is True and [v["variant_id"] for v in shown["variants"]] == ["v005"]  # 表示も同じ番号

    vr.generate_variants(tmp_config, [_entry()], 1, _generator(tmp_config))     # CLI の本体
    record = _record(tmp_config)
    assert _ids(tmp_config) == ["v005", "v006"]
    assert _item(tmp_config, "v005")["source"] == vr.SOURCE_LEGACY
    assert _shas(tmp_config)["v005"] == original_sha
    assert record["adopted"] == "v005" and record["adopted_file"]["sha1"] == original_sha
    assert record["next_seq"] == 7                                 # 後退しない
    folder = vr.variant_dir(tmp_config, "001")
    assert sorted(p.name for p in folder.iterdir()) == ["v005.png", "v006.png"]


def test_original_skips_reserved_part_and_existing_numbers(client, tmp_config, box):
    """v001・v002 は実行の記録で予約済み、v003 は .part、v004 は別の画像 → 原画は v005。"""
    folder = vr.variant_dir(tmp_config, "001")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "v003.png.part").write_bytes(_png((30, 60, 200, 255)))
    (folder / "v004.png").write_bytes(_png((200, 60, 60, 255)))
    _put_record(tmp_config, next_seq=1, regen_run={"since": "2026-01-01T00:00:00.000000", "count": 2,
                                                   "done": [], "reserved": ["v001", "v002"], "owner": None})
    original_sha = _original(tmp_config)
    record = _record(tmp_config)
    assert vr.ensure_legacy_variant(tmp_config, "001", record).variants[0].variant_id == "v005"
    before = {p.name: vr.sha1_file(p) for p in folder.iterdir()}

    vr.generate_variants(tmp_config, [_entry()], 1, _generator(tmp_config))
    assert _ids(tmp_config) == ["v005", "v006"]
    assert _shas(tmp_config)["v005"] == original_sha and _record(tmp_config)["adopted"] == "v005"
    for name, sha in before.items():                               # .part・別の画像は上書きしない
        assert vr.sha1_file(folder / name) == sha


# ===========================================================================
# R-A: repair の経路でも、止まった初回生成の間は原画を取り込まない
# ===========================================================================
def test_ra_repair_does_not_take_in_the_original_during_a_stopped_initial_run(client, tmp_config, box):
    """止まった初回生成（3枚・予約 v001/v002・v001 は画像あり未登録）＋原画 → repair → 再開。"""
    folder = vr.variant_dir(tmp_config, "001")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "v001.png").write_bytes(_png((30, 60, 200, 255)))          # 前回できていた画像（未登録）
    v001_sha = vr.sha1_file(folder / "v001.png")
    _put_run(tmp_config, count=3, owner=None, reserved=["v001", "v002"])  # 候補0件・next_seq=3
    original_sha = _original(tmp_config)
    run_before = _record(tmp_config)["initial_run"]

    vr.repair_state(tmp_config)
    record = _record(tmp_config)
    shas = _shas(tmp_config)
    assert shas == {"v001": v001_sha}                              # 前回できていた画像だけ（復元の対象はそのまま）
    assert original_sha not in shas.values()                       # 原画は取り込まない
    assert record["adopted"] is None and "adopted_file" not in record
    assert not (folder / "v002.png").exists()                      # 予約済みの v002 を原画に使わない
    assert record["initial_run"] == run_before and record["next_seq"] >= 3

    plan = _plan(client, ["001"], 4).get_json()
    assert [(t["count"], t["resume"], t["recovered"]) for t in plan["targets"]] == [(2, True, 1)]
    calls = box["calls"]
    r = _run(client, ["001"], 4)
    assert r.status_code == 200, r.get_json()
    assert box["calls"] - calls == 2                               # 原画を「前回の画像」と数えず、残り2枚を作る
    shas = _shas(tmp_config)
    assert sorted(shas) == ["v001", "v003", "v004"]                # v002 は使わない
    assert original_sha not in shas.values()
    record = _record(tmp_config)
    assert "initial_run" not in record and record["adopted"] is None and record["next_seq"] == 5


def test_repair_takes_in_the_original_after_the_reserved_numbers(client, tmp_config, box):
    """候補0件の記録（予約後に失敗した跡・next_seq=5・initial_run なし）の repair: 原画は予約を避けた v005。"""
    folder = vr.variant_dir(tmp_config, "001")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "v001.png").write_bytes(_png((30, 60, 200, 255)))          # 記録に無い画像
    _put_record(tmp_config, next_seq=5)
    original_sha = _original(tmp_config)
    vr.repair_state(tmp_config)
    record = _record(tmp_config)
    assert _ids(tmp_config) == ["v001", "v005"]                    # 予約済みの v002〜v004 を原画に使わない
    assert _shas(tmp_config)["v005"] == original_sha and record["adopted"] == "v005"
    assert record["next_seq"] == 6


def test_repair_without_a_record_still_takes_in_the_original(client, tmp_config, box):
    """記録の無いスタンプの repair は従来どおり: 原画を候補（採用中）として取り込む。"""
    folder = vr.variant_dir(tmp_config, "001")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "v001.png").write_bytes(_png((30, 60, 200, 255)))          # 別の画像
    original_sha = _original(tmp_config)
    vr.repair_state(tmp_config)
    record = _record(tmp_config)
    assert _ids(tmp_config) == ["v001", "v002"]
    assert _shas(tmp_config)["v002"] == original_sha and record["adopted"] == "v002"
    assert record["adopted_file"]["sha1"] == original_sha and record["next_seq"] == 3


# ===========================================================================
# 通常のケース（従来どおり）
# ===========================================================================
def test_normal_legacy_is_still_taken_in_as_v001(client, tmp_config, box):
    original_sha = _original(tmp_config)
    assert client.get("/api/variants/001").get_json()["variants"][0]["variant_id"] == "v001"
    vr.generate_variants(tmp_config, [_entry()], 1, _generator(tmp_config))
    record = _record(tmp_config)
    assert _ids(tmp_config) == ["v001", "v002"]
    assert _shas(tmp_config)["v001"] == original_sha and record["adopted"] == "v001"
    assert record["next_seq"] == 3


def test_existing_candidates_do_not_take_in_the_original(client, tmp_config, box):
    vr.generate_variants(tmp_config, [_entry()], 1, _generator(tmp_config))    # 候補 v001（原画なし）
    original_sha = _original(tmp_config)
    vr.generate_variants(tmp_config, [_entry()], 1, _generator(tmp_config))
    assert _ids(tmp_config) == ["v001", "v002"]
    assert original_sha not in _shas(tmp_config).values() and _record(tmp_config)["adopted"] is None


def test_adopted_legacy_record_is_not_changed(client, tmp_config, box):
    """既に記録にある原画（採用中の v001・adopted_file）の番号と採用の情報は変えない。"""
    _original(tmp_config)
    vr.generate_variants(tmp_config, [_entry()], 1, _generator(tmp_config))
    before = _record(tmp_config)
    assert before["adopted"] == "v001" and isinstance(before["adopted_file"], dict)
    vr.update(tmp_config, lambda s: s["stickers"]["001"].update({"next_seq": 9}))
    vr.generate_variants(tmp_config, [_entry()], 1, _generator(tmp_config))
    after = _record(tmp_config)
    assert after["adopted"] == "v001" and after["adopted_file"] == before["adopted_file"]
    assert _item(tmp_config, "v001") == [v for v in before["variants"] if v["variant_id"] == "v001"][0]
    assert _ids(tmp_config) == ["v001", "v002", "v009"] and after["next_seq"] == 10
