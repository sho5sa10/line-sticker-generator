"""実行の記録（initial_run / regen_run）の安全性（Phase 7 STEP 2b のレビュー指摘 M-1・M-2・L-1）。

M-1: repair が、候補0件の記録を作り直しても、実行の記録・予約（next_seq）・知らない項目を失わない
M-2: 完了処理（finish）の保存に失敗しても、同じプロセスが「実行中」と主張し続けない（続きから再開できる）
L-1: 一覧の initial_run（running / pending / None）の表示を確かめる
画像生成APIは呼びません（偽のプロバイダ。通信も遮断します。fixture は test_initial_candidates と共通）。
"""

from __future__ import annotations

import subprocess
import sys

import pytest

pytest.importorskip("flask", reason="Flask が未インストールです")

from src import variants as vr  # noqa: E402
from src.providers.base import ProviderError  # noqa: E402
from tests.test_initial_candidates import (  # noqa: E402,F401 - fixture も読み込む
    GUI, _dead_pid, _entry, _generator, _ids, _join_jobs, _owner, _plan, _png, _record, _run, box, client,
)

RUN_SINCE = "2026-01-01T00:00:00.000000"


def _folder(cfg, sid="001"):
    folder = vr.variant_dir(cfg, sid)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _put_record(cfg, sid="001", **record):
    vr.update(cfg, lambda s: s.setdefault("stickers", {}).update({sid: record}))


def _stopped_initial(cfg, *, count=4, reserved=("v001", "v002"), done=(), next_seq=3, **extra):
    """途中で止まった初回生成: 候補0件・予約済み（v001 は画像あり未登録、v002 は応答前に停止）。"""
    folder = _folder(cfg)
    (folder / "v001.png").write_bytes(_png())
    _put_record(cfg, adopted=None, adopted_at=None, variants=[], next_seq=next_seq,
                initial_run={"since": RUN_SINCE, "count": count, "done": list(done),
                             "reserved": list(reserved), "owner": None}, **extra)


# ===========================================================================
# M-1: repair が実行の記録と予約を失わない
# ===========================================================================
def test_m1_1_repair_keeps_the_initial_run_and_next_seq(client, tmp_config):
    _stopped_initial(tmp_config)
    report = vr.repair_state(tmp_config)
    record = _record(tmp_config)
    assert record["initial_run"]["reserved"] == ["v001", "v002"]    # 予約・実行の記録が残る
    assert record["initial_run"]["count"] == 4
    assert record["next_seq"] >= 3                                   # 後退しない
    assert _ids(tmp_config) == ["v001"]                              # 記録に無かった画像は候補に戻す
    assert report["changed"] is True


def test_m1_2_reserved_number_is_not_reused_after_repair(client, tmp_config, box):
    _stopped_initial(tmp_config)
    vr.repair_state(tmp_config)
    results = vr.generate_variants(tmp_config, [_entry()], 1, _generator(tmp_config))   # CLI の本体
    assert [r["variant_id"] for r in results] == ["v003"]           # 予約済みの v002 は使わない
    assert box["calls"] == 1


def test_m1_2b_the_stopped_run_resumes_after_repair(client, tmp_config, box):
    """repair の後も、途中で止まった初回生成は続きから（残りだけ・登録済みは作り直さない）。"""
    _stopped_initial(tmp_config)
    vr.repair_state(tmp_config)
    d = _plan(client, ["001"], 4).get_json()
    assert [(t["id"], t["count"], t["resume"]) for t in d["targets"]] == [("001", 3, True)]
    r = _run(client, ["001"], 4)
    assert r.status_code == 200, r.get_json()
    ids = _ids(tmp_config)
    assert ids == ["v001", "v003", "v004", "v005"] and len(set(ids)) == 4
    assert box["calls"] == 3 and "initial_run" not in _record(tmp_config)


def test_m1_3_repair_keeps_unknown_fields(client, tmp_config):
    _stopped_initial(tmp_config, unknown_field="keep-me")
    vr.repair_state(tmp_config)
    assert _record(tmp_config)["unknown_field"] == "keep-me"


def test_m1_4_repair_keeps_the_regen_run(client, tmp_config):
    regen_run = {"since": RUN_SINCE, "count": 2, "done": [], "reserved": ["v002"], "owner": None}
    _folder(tmp_config)
    (vr.variant_dir(tmp_config, "001") / "v001.png").write_bytes(_png())
    _put_record(tmp_config, adopted=None, adopted_at=None, variants=[], next_seq=3, regen_run=regen_run)
    vr.repair_state(tmp_config)
    record = _record(tmp_config)
    assert record["regen_run"] == regen_run and record["next_seq"] >= 3


def test_m1_5_reserved_part_file_survives_repair_and_is_recovered(client, tmp_config, box):
    """予約番号の .part（課金済みの画像）: repair の後も番号を使い回さず、API を呼ばずに登録できる。"""
    _stopped_initial(tmp_config, count=2)
    part = vr.variant_dir(tmp_config, "001") / "v002.png.part"
    part.write_bytes(_png((30, 60, 200, 255)))
    vr.repair_state(tmp_config)
    assert part.exists() and _record(tmp_config)["next_seq"] >= 3
    probe = {"variants": [], "next_seq": _record(tmp_config)["next_seq"]}
    assert vr.allocate_variant(tmp_config, probe, "001")[0] not in ("v001", "v002")
    r = _run(client, ["001"], 4)
    assert r.status_code == 200, r.get_json()
    assert _ids(tmp_config) == ["v001", "v002"] and box["calls"] == 0     # どちらも作り直さない
    assert vr._is_complete_png(vr.variant_dir(tmp_config, "001") / "v002.png") and not part.exists()
    assert "initial_run" not in _record(tmp_config)


def test_m1_repair_of_a_healthy_state_without_run_is_unchanged(client, tmp_config):
    """実行の記録が無い通常の状態では、repair の結果は従来どおり（候補フォルダから作り直す）。"""
    _folder(tmp_config)
    (vr.variant_dir(tmp_config, "001") / "v001.png").write_bytes(_png())
    report = vr.repair_state(tmp_config)
    record = _record(tmp_config)
    assert report["stickers"]["001"]["rebuilt"] is True
    assert _ids(tmp_config) == ["v001"] and record["next_seq"] == 2 and record["adopted"] is None


def test_m1_repair_keeps_done(client, tmp_config):
    _stopped_initial(tmp_config, done=("v001",))
    vr.repair_state(tmp_config)
    run = _record(tmp_config)["initial_run"]
    assert run["done"] == ["v001"] and run["reserved"] == ["v001", "v002"]


@pytest.mark.parametrize("stale_next_seq", [None, 1])
def test_m1_next_seq_covers_reserved_numbers_even_when_behind(client, tmp_config, stale_next_seq):
    """next_seq が無い・予約より小さい（古いデータなど）記録でも、予約済みの番号の次から採番する。"""
    _stopped_initial(tmp_config, reserved=("v001", "v002", "v003"))
    def stale(state):
        record = state["stickers"]["001"]
        if stale_next_seq is None:
            record.pop("next_seq")
        else:
            record["next_seq"] = stale_next_seq
    vr.update(tmp_config, stale)
    vr.repair_state(tmp_config)
    assert _record(tmp_config)["next_seq"] >= 4
    results = vr.generate_variants(tmp_config, [_entry()], 1, _generator(tmp_config))
    assert [r["variant_id"] for r in results] == ["v004"]


def test_m1_repair_with_both_runs_then_resume(client, tmp_config, box):
    """initial_run と regen_run が両方ある候補0件の記録: repair で両方残り、どちらの予約番号も使わずに再開する。"""
    regen_run = {"since": RUN_SINCE, "count": 2, "done": [], "reserved": ["v003", "v004"], "owner": None}
    _stopped_initial(tmp_config, count=3, next_seq=5, regen_run=regen_run)
    vr.repair_state(tmp_config)
    record = _record(tmp_config)
    assert record["initial_run"]["reserved"] == ["v001", "v002"] and record["regen_run"] == regen_run
    assert record["next_seq"] >= 5
    d = _plan(client, ["001"], 4).get_json()
    assert [(t["count"], t["resume"], t["recovered"]) for t in d["targets"]] == [(2, True, 1)]
    r = _run(client, ["001"], 4)
    assert r.status_code == 200, r.get_json()
    assert _ids(tmp_config) == ["v001", "v005", "v006"] and box["calls"] == 2
    record = _record(tmp_config)
    assert "initial_run" not in record and record["regen_run"] == regen_run    # regen の記録には触れない


# ===========================================================================
# M-2: 完了処理の保存に失敗しても「実行中」のままにならない
# ===========================================================================
class _FinishSaveFails:
    """finish_initial / finish_regen の中の保存だけを失敗させます（関数そのものは本物）。"""

    def __init__(self, monkeypatch, finish_name):
        self.armed = True
        self.failures = 0
        self.in_finish = False
        real_finish = getattr(vr, finish_name)
        real_save = vr.save

        def save(config, data, **kwargs):
            if self.armed and self.in_finish:
                self.failures += 1
                raise PermissionError("偽: 完了処理の保存に失敗しました")
            return real_save(config, data, **kwargs)

        def finish(*args, **kwargs):
            self.in_finish = True
            try:
                return real_finish(*args, **kwargs)
            finally:
                self.in_finish = False
        monkeypatch.setattr(vr, "save", save)
        monkeypatch.setattr(vr, finish_name, finish)


def _fail_on_call(box, n):
    def before(path):
        if box["calls"] == n:
            raise ProviderError("偽の失敗")
    box["before"] = before


def _row(client, sid="001"):
    return next(s for s in client.get("/api/stickers").get_json()["stickers"] if s["id"] == sid)


def test_m2_1_failed_finish_does_not_leave_the_sticker_running(client, tmp_config, box, monkeypatch):
    fails = _FinishSaveFails(monkeypatch, "finish_initial")
    _fail_on_call(box, 3)                                   # 3枚中3枚目が失敗 → 途中で止まった実行
    _run(client, ["001"], 3)
    assert fails.failures >= 1                              # 完了処理の保存は本当に失敗している
    record = _record(tmp_config)
    assert record["initial_run"]["owner"] is not None       # 保存できなかったので owner は残っている
    assert vr.initial_status(record) == "pending"           # それでも「実行中」とは扱わない
    assert _row(client)["initial_run"] == "pending"
    d = _plan(client, ["001"], 3).get_json()
    assert d["busy"] == [] and [(t["count"], t["resume"]) for t in d["targets"]] == [(1, True)]


def test_m2_2_and_3_the_same_process_can_resume_after_a_failed_finish(client, tmp_config, box, monkeypatch):
    fails = _FinishSaveFails(monkeypatch, "finish_initial")
    _fail_on_call(box, 3)
    _run(client, ["001"], 3)
    assert fails.failures >= 1
    before = _record(tmp_config)["initial_run"]
    assert before["reserved"] == ["v001", "v002", "v003"] and before["done"] == ["v001", "v002"]

    fails.armed = False                                     # 保存できる状態に戻る
    box["before"] = None
    box["calls"] = 0
    r = _run(client, ["001"], 3)                            # 同じプロセス（同じ GUI サーバー）から再開
    assert r.status_code == 200, r.get_json()
    ids = _ids(tmp_config)
    assert ids == ["v001", "v002", "v004"] and len(set(ids)) == 3    # 予約済みの v003 は使わない
    assert box["calls"] == 1                                # 残りの1枚だけ
    for vid in ids:
        assert vr._is_complete_png(vr.variant_dir(tmp_config, "001") / f"{vid}.png")
    assert "initial_run" not in _record(tmp_config)         # 正常に片付く
    job = client.get("/api/job").get_json()["job"]
    assert job["status"] == "finished"


def test_m2_3_run_record_stays_consistent_after_a_failed_finish(client, tmp_config, box, monkeypatch):
    """保存に失敗した直後の記録: 予約は失われず、done に重複が無い。"""
    _FinishSaveFails(monkeypatch, "finish_initial")
    _fail_on_call(box, 2)
    _run(client, ["001"], 2)
    run = _record(tmp_config)["initial_run"]
    assert run["reserved"] == ["v001", "v002"] and run["done"] == ["v001"]
    assert len(set(run["done"])) == len(run["done"]) and set(run["done"]) <= set(run["reserved"])


def _mark_regen(client, tmp_config):
    vr.set_verdict(tmp_config, "003", "v001", "regen")
    plan = client.post("/api/variants/generate", json={"regen_only": True, "count": 2, "dry_run": True},
                       headers=GUI).get_json()
    assert plan["targets"] == [{"id": "003", "count": 2, "resume": False}]
    return plan


def test_m2_4_regen_also_recovers_from_a_failed_finish(client, tmp_config, box, monkeypatch):
    """同じ問題は regen（Phase 6）の完了処理にもある。保存に失敗しても「実行中」のままにならない。"""
    plan = _mark_regen(client, tmp_config)
    fails = _FinishSaveFails(monkeypatch, "finish_regen")
    _fail_on_call(box, 2)
    client.post("/api/variants/generate",
                json={"regen_only": True, "count": 2, "expected_total": plan["total"]}, headers=GUI)
    _join_jobs()
    assert fails.failures >= 1
    assert _record(tmp_config, "003")["regen_run"]["owner"] is not None
    again = client.post("/api/variants/generate", json={"regen_only": True, "count": 2, "dry_run": True},
                        headers=GUI).get_json()
    assert again["busy"] == []
    assert again["targets"] == [{"id": "003", "count": 1, "resume": True}]

    fails.armed = False
    box["before"] = None
    box["calls"] = 0
    r = client.post("/api/variants/generate",
                    json={"regen_only": True, "count": 2, "expected_total": again["total"]}, headers=GUI)
    _join_jobs()
    assert r.status_code == 200, r.get_json()
    ids = _ids(tmp_config, "003")
    assert ids == ["v001", "v002", "v004"] and box["calls"] == 1      # 予約済みの v003 は使わない
    assert "regen_run" not in _record(tmp_config, "003")


def test_m2_a_live_run_of_this_process_is_still_running(client, tmp_config, box):
    """回復の仕組みが、いま本当に実行中の初回生成まで「止まった」と扱わないこと。"""
    import threading

    in_call, release = threading.Event(), threading.Event()

    def hold(path):
        in_call.set()
        assert release.wait(30)
    box["before"] = hold
    client.post("/api/variants/initial", json={"ids": ["001"], "count": 1, "expected_total": 1}, headers=GUI)
    assert in_call.wait(30)
    try:
        assert vr.initial_status(_record(tmp_config)) == "running"
        assert _plan(client, ["001"], 1).get_json()["busy"] == ["001"]
    finally:
        release.set()
        _join_jobs()


def test_m2_failed_run_and_live_run_in_the_same_process_are_told_apart(client, tmp_config, box, monkeypatch):
    """同じプロセスで、A（締めの保存に失敗）= 止まった / B（いま実行中）= 実行中、と区別できる。"""
    import threading

    fails = _FinishSaveFails(monkeypatch, "finish_initial")
    _fail_on_call(box, 2)
    _run(client, ["001"], 2)                                 # A: 001 の締めの保存に失敗
    assert fails.failures >= 1
    fails.armed = False
    in_call, release = threading.Event(), threading.Event()

    def hold(path):
        in_call.set()
        assert release.wait(30)
    box["before"] = hold
    client.post("/api/variants/initial", json={"ids": ["002"], "count": 1, "expected_total": 1}, headers=GUI)
    assert in_call.wait(30)                                  # B: 002 を実行中
    try:
        assert vr.initial_status(_record(tmp_config, "001")) == "pending"
        assert vr.initial_status(_record(tmp_config, "002")) == "running"
        assert _record(tmp_config, "001")["initial_run"]["owner"]["pid"] == \
            _record(tmp_config, "002")["initial_run"]["owner"]["pid"]            # どちらも同じプロセス
    finally:
        release.set()
        _join_jobs()


def test_m2_only_the_ended_run_token_is_treated_as_stopped():
    """終わった実行の token だけが「止まった」扱い。同じ pid の別の token は、従来どおり動いている。"""
    import os

    ended, other = _owner(os.getpid()), _owner(os.getpid())
    ended["token"], other["token"] = "a" * 32, "b" * 32
    assert vr._regen_owner_alive(ended) and vr._regen_owner_alive(other)
    vr._mark_run_ended(ended["token"])
    try:
        assert not vr._regen_owner_alive(ended)
        assert vr._regen_owner_alive(other)
    finally:
        vr._forget_ended_run(ended["token"])
    assert vr._regen_owner_alive(ended)
    assert vr._regen_owner_alive({**other, "token": ["not", "hashable"]})     # 壊れた token でも落ちない


def test_m2_successful_finish_does_not_keep_the_token(client, tmp_config):
    """締めを保存できた実行の token は覚えたままにしない（増え続けない）。初回生成・再生成とも。"""
    before = set(vr._ENDED_RUN_TOKENS)
    _run(client, ["001"], 2)
    assert "initial_run" not in _record(tmp_config)
    assert vr._ENDED_RUN_TOKENS == before
    plan = _mark_regen(client, tmp_config)
    client.post("/api/variants/generate",
                json={"regen_only": True, "count": 2, "expected_total": plan["total"]}, headers=GUI)
    _join_jobs()
    assert "regen_run" not in _record(tmp_config, "003")
    assert vr._ENDED_RUN_TOKENS == before


def test_m2_regen_failed_run_and_live_run_in_the_same_process_are_told_apart(client, tmp_config, box,
                                                                              monkeypatch):
    """regen: A（締めの保存に失敗した再生成）= 止まった / B（同じプロセスで実行中の初回生成）= 実行中。"""
    import threading

    plan = _mark_regen(client, tmp_config)
    fails = _FinishSaveFails(monkeypatch, "finish_regen")
    _fail_on_call(box, 2)
    client.post("/api/variants/generate",
                json={"regen_only": True, "count": 2, "expected_total": plan["total"]}, headers=GUI)
    _join_jobs()
    assert fails.failures >= 1
    fails.armed = False
    in_call, release = threading.Event(), threading.Event()

    def hold(path):
        in_call.set()
        assert release.wait(30)
    box["before"] = hold
    client.post("/api/variants/initial", json={"ids": ["001"], "count": 1, "expected_total": 1}, headers=GUI)
    assert in_call.wait(30)
    try:
        regen_owner = _record(tmp_config, "003")["regen_run"]["owner"]
        initial_owner = _record(tmp_config, "001")["initial_run"]["owner"]
        assert regen_owner["pid"] == initial_owner["pid"]                     # どちらも同じプロセス
        assert not vr._regen_owner_alive(regen_owner)                         # A: 止まった
        assert vr._regen_owner_alive(initial_owner)                           # B: 実行中
        again = client.post("/api/variants/generate",
                            json={"regen_only": True, "count": 2, "dry_run": True}, headers=GUI).get_json()
        assert again["busy"] == [] and again["targets"] == [{"id": "003", "count": 1, "resume": True}]
        assert vr.initial_status(_record(tmp_config, "001")) == "running"
    finally:
        release.set()
        _join_jobs()


# ===========================================================================
# L-1: 一覧の initial_run（running / pending / None）
# ===========================================================================
def test_l1_1_running_while_generating(client, tmp_config, box):
    import threading

    in_call, release = threading.Event(), threading.Event()

    def hold(path):
        in_call.set()
        assert release.wait(30)
    box["before"] = hold
    client.post("/api/variants/initial", json={"ids": ["001"], "count": 1, "expected_total": 1}, headers=GUI)
    assert in_call.wait(30)
    try:
        assert vr.initial_status(_record(tmp_config)) == "running"
        assert _row(client)["initial_run"] == "running"
    finally:
        release.set()
        _join_jobs()


def test_l1_1b_running_for_another_live_process(client, tmp_config):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        _put_record(tmp_config, adopted=None, adopted_at=None, variants=[], next_seq=1,
                    initial_run={"since": RUN_SINCE, "count": 1, "done": [], "reserved": [],
                                 "owner": _owner(child.pid)})
        assert vr.initial_status(_record(tmp_config)) == "running"
        assert _row(client)["initial_run"] == "running"
    finally:
        child.kill()
        child.wait()


@pytest.mark.parametrize("owner", ["dead", None])
def test_l1_2_pending_when_the_owner_has_stopped(client, tmp_config, owner):
    _put_record(tmp_config, adopted=None, adopted_at=None, variants=[], next_seq=1,
                initial_run={"since": RUN_SINCE, "count": 1, "done": [], "reserved": [],
                             "owner": _owner(_dead_pid()) if owner == "dead" else None})
    assert vr.initial_status(_record(tmp_config)) == "pending"
    assert _row(client)["initial_run"] == "pending"


def test_l1_3_none_without_an_initial_run(client, tmp_config):
    assert vr.initial_status(None) is None
    assert vr.initial_status({"variants": [], "next_seq": 1}) is None
    assert vr.initial_status({"variants": [], "initial_run": "broken"}) is None
    assert _row(client, "001")["initial_run"] is None            # 記録なし
    assert _row(client, "003")["initial_run"] is None            # 候補あり・実行の記録なし


def test_l1_4_listing_reports_each_state_side_by_side(client, tmp_config):
    """一覧（/api/stickers と /api/state）が running / pending / None を取り違えない。"""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        for sid, owner in (("001", _owner(child.pid)), ("002", None)):
            _put_record(tmp_config, sid, adopted=None, adopted_at=None, variants=[], next_seq=1,
                        initial_run={"since": RUN_SINCE, "count": 1, "done": [], "reserved": [],
                                     "owner": owner})
        expected = {"001": "running", "002": "pending", "003": None, "004": None}
        for path in ("/api/stickers", "/api/state"):
            rows = client.get(path).get_json()["stickers"]
            assert {s["id"]: s["initial_run"] for s in rows} == expected, path
    finally:
        child.kill()
        child.wait()
