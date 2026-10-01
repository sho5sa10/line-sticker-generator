"""同じスタンプの生成を、GUI・CLI をまたいで同時に走らせないこと（Phase 7 STEP 2e・M-3）。

GUI の初回生成・再生成と CLI `generate --variants N` は、スタンプ単位の「実行権」（ロックファイル）を
待たずに取ってから生成し、取れなければ始めません。実行権は生成が終わるまで持ちます。
- GUI 同士は従来どおり（ジョブは1件・409）
- GUI × CLI、CLI × CLI、初回生成 × 再生成は、同じスタンプでは片方だけ
- 別のスタンプは並行して生成できる
- 止まった実行の記録（pending）だけでは拒否しない。異常終了で残った実行権は回収する
- 採用・repair・登録は、実行権では止めない
画像生成APIは呼びません（偽のプロバイダ。通信も遮断します。fixture は test_initial_candidates と共通）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("flask", reason="Flask が未インストールです")

from src import variants as vr  # noqa: E402
from src import webapp  # noqa: E402
from tests.test_initial_candidates import (  # noqa: E402,F401 - fixture も読み込む
    GUI, PROJECT, _entry, _generator, _ids, _join_jobs, _owner, _png, _record, box, client,
)

WAIT = 30
BUSY_TEXT = "別の処理で生成中"

# CLI（python -m src.main generate --id <id> --variants N --yes）を、偽のプロバイダで実行する子プロセス。
# mode: run（そのまま）/ hold（1枚目の API 呼び出しで、go ファイルができるまで待つ）/ die（1枚目の API 呼び出し中に強制終了）
_CLI = r"""
import io, os, runpy, socket, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
os.environ["OPENAI_API_KEY"] = "test-dummy-not-used"
os.environ["OPENAI_BASE_URL"] = "http://127.0.0.1:9"
cfg_path, sid, count, mode, tag = sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6]
root = Path(cfg_path).parent.parent

def connect(self, address):
    (root / f"network-{os.getpid()}.txt").write_text(str(address))
    raise OSError("通信は禁止")
socket.socket.connect = connect
from src import image_generator as ig, providers
from src.providers import openai_provider
from src.providers.base import ImageGenerationProvider
from tests.test_scoring import sticker_like

def real_provider(*a, **k):
    raise AssertionError("実プロバイダが呼ばれました")
openai_provider.OpenAIImageProvider.generate = real_provider
state = {"n": 0}

class Fake(ImageGenerationProvider):
    name = "fake"
    def generate(self, prompt, reference_image=None, output_path=None):
        state["n"] += 1
        with open(root / f"calls-{tag}.txt", "a", encoding="utf-8") as f:
            f.write(Path(output_path).name + "\n")
        if state["n"] == 1 and mode in ("hold", "die"):
            (root / f"in-call-{tag}").write_text(str(os.getpid()))
            if mode == "die":
                os._exit(9)                      # 実行権を持ったまま異常終了（finally も走らない）
            deadline = time.time() + 30
            while not (root / f"go-{tag}").exists() and time.time() < deadline:
                time.sleep(0.02)
        buf = io.BytesIO()
        sticker_like(body=(200, 60, 60, 255)).save(buf, "PNG")
        self._write(buf.getvalue(), output_path)
        return buf.getvalue()
    def estimate_cost_usd(self, count):
        return 0.0
ig.create_provider = lambda config: Fake()
providers.create_provider = lambda config: Fake()
sys.argv = ["src.main", "--config", cfg_path, "generate", "--id", sid, "--variants", count, "--yes"]
runpy.run_module("src.main", run_name="__main__")
"""


def _cli(cfg, sid="001", count=4, mode="run", tag="a"):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(PROJECT),
           "OPENAI_API_KEY": "test-dummy-not-used", "OPENAI_BASE_URL": "http://127.0.0.1:9"}
    return subprocess.Popen([sys.executable, "-c", _CLI, str(PROJECT), str(cfg.path), sid, str(count), mode, tag],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            env=env, cwd=cfg.root)


def _wait_file(path: Path):
    deadline = time.time() + WAIT
    while not path.exists() and time.time() < deadline:
        time.sleep(0.02)
    assert path.exists(), path


def _cli_calls(cfg, tag="a"):
    f = cfg.root / f"calls-{tag}.txt"
    return len(f.read_text(encoding="utf-8").split()) if f.exists() else 0


def _finish(proc):
    out, err = proc.communicate(timeout=120)
    return proc.returncode, out, err


def _lock_file(cfg, sid="001"):
    return cfg.variants_path.parent / f"variants.run-{sid}.lock"


def _assert_consistent(cfg, sid="001"):
    record = _record(cfg, sid)
    ids = [v["variant_id"] for v in record["variants"]]
    assert len(ids) == len(set(ids)), ids
    for key in ("initial_run", "regen_run"):
        run = record.get(key)
        if isinstance(run, dict):
            done = run.get("done") or []
            assert len(done) == len(set(done)) and set(done) <= set(run.get("reserved") or []), run
    for vid in ids:
        assert vr._is_complete_png(vr.variant_dir(cfg, sid) / f"{vid}.png"), vid
    if ids:
        assert record["next_seq"] >= max(int(v[1:]) for v in ids) + 1
    return ids


class _HoldGui:
    """GUI のジョブを、1枚目の API 呼び出しの中で止めておく。"""

    def __init__(self, box):
        self.in_call, self.release = threading.Event(), threading.Event()

        def before(path):
            if not self.in_call.is_set():
                self.in_call.set()
                assert self.release.wait(WAIT)
        box["before"] = before


def _start_gui_initial(client, sid="001", count=4):
    return client.post("/api/variants/initial", json={"ids": [sid], "count": count, "expected_total": count},
                       headers=GUI)


def _no_network(cfg):
    assert not list(cfg.root.glob("network-*.txt"))


# ===========================================================================
# M3-A: GUI 初回生成 × GUI 初回生成（従来どおり）
# ===========================================================================
def test_m3a_gui_and_gui_on_the_same_sticker(client, tmp_config, box):
    hold = _HoldGui(box)
    r1 = _start_gui_initial(client)
    assert r1.status_code == 200 and hold.in_call.wait(WAIT)
    r2 = _start_gui_initial(client)            # 1本目は実行中（開始の判定を済ませ、owner がある）
    hold.release.set()
    _join_jobs()
    # 既存の仕様どおりの拒否: 計画の段階で「実行中」（400・busy）。同時に計画を通った場合は 409（STEP 2b で確認済み）
    assert r2.status_code == 400 and r2.get_json()["busy"] == ["001"]
    assert _assert_consistent(tmp_config) == ["v001", "v002", "v003", "v004"] and box["calls"] == 4


# ===========================================================================
# M3-B: GUI 初回生成 × CLI（どちらが先でも、片方だけ）
# ===========================================================================
def test_m3b_cli_is_refused_while_gui_generates(client, tmp_config, box):
    hold = _HoldGui(box)
    assert _start_gui_initial(client).status_code == 200 and hold.in_call.wait(WAIT)
    held = _lock_file(tmp_config).exists()                       # GUI が実行権を持っているか
    rc, out, err = _finish(_cli(tmp_config, count=4))
    hold.release.set()
    _join_jobs()
    assert (rc, _cli_calls(tmp_config), box["calls"]) == (1, 0, 4), err     # 4 + 4 = 8 にならない
    assert BUSY_TEXT in err and held                             # EXIT_ERROR・stderr に理由
    assert _cli_calls(tmp_config) == 0                           # CLI は API を呼ばない
    assert box["calls"] == 4                                     # 4 + 4 = 8 にならない
    assert _assert_consistent(tmp_config) == ["v001", "v002", "v003", "v004"]
    assert not _lock_file(tmp_config).exists()                   # 終わったら外れる
    _no_network(tmp_config)


def test_m3b_gui_is_refused_while_cli_generates(client, tmp_config, box):
    cli = _cli(tmp_config, count=4, mode="hold")
    _wait_file(tmp_config.root / "in-call-a")
    d = client.post("/api/variants/initial", json={"ids": ["001"], "count": 4, "dry_run": True},
                    headers=GUI).get_json()
    assert d["target_ids"] == [] and d["busy"] == ["001"]
    assert {s["id"]: s["reason"] for s in d["skipped"]} == {"001": "running"}
    r = client.post("/api/variants/initial", json={"ids": ["001"], "count": 4, "expected_total": 4}, headers=GUI)
    (tmp_config.root / "go-a").write_text("")
    rc, out, err = _finish(cli)
    _join_jobs()
    assert r.status_code == 400 and r.get_json()["busy"] == ["001"]      # 既存の形式（L-3 は変えない）
    assert rc == 0 and _cli_calls(tmp_config) == 4 and box["calls"] == 0
    assert _assert_consistent(tmp_config) == ["v001", "v002", "v003", "v004"]
    assert "initial_run" not in (_record(tmp_config) or {})
    assert not _lock_file(tmp_config).exists()
    _no_network(tmp_config)


def test_m3b_cli_taking_the_sticker_after_the_gui_plan_wins(client, tmp_config, box, monkeypatch):
    """確認（計画）と開始の間に CLI が実行権を取った場合: GUI のジョブは作らない（確認→開始の隙間が無い）。"""
    real_start = webapp.JobManager.start
    cli_box = {}

    def start_after_cli_took_it(self, *args):
        cli_box["proc"] = _cli(tmp_config, count=4, mode="hold")
        _wait_file(tmp_config.root / "in-call-a")                # 計画は通った後で、CLI が実行権を取った
        return real_start(self, *args)
    monkeypatch.setattr(webapp.JobManager, "start", start_after_cli_took_it)
    r = _start_gui_initial(client)
    _join_jobs()
    job = client.get("/api/job").get_json()["job"]
    (tmp_config.root / "go-a").write_text("")
    rc, out, err = _finish(cli_box["proc"])
    assert r.status_code == 200 and job["status"] == "finished" and box["calls"] == 0
    assert "作りませんでした" in job["events"][-1]["message"]
    assert rc == 0 and _cli_calls(tmp_config) == 4
    assert _assert_consistent(tmp_config) == ["v001", "v002", "v003", "v004"]
    assert "initial_run" not in (_record(tmp_config) or {})


# ===========================================================================
# M3-C: CLI × CLI
# ===========================================================================
def test_m3c_cli_and_cli_on_the_same_sticker(client, tmp_config, box):
    first = _cli(tmp_config, count=4, mode="hold", tag="a")
    _wait_file(tmp_config.root / "in-call-a")
    rc2, out2, err2 = _finish(_cli(tmp_config, count=4, tag="b"))
    (tmp_config.root / "go-a").write_text("")
    rc1, out1, err1 = _finish(first)
    assert rc1 == 0 and rc2 == 1 and BUSY_TEXT in err2
    assert _cli_calls(tmp_config, "a") == 4 and _cli_calls(tmp_config, "b") == 0
    assert _assert_consistent(tmp_config) == ["v001", "v002", "v003", "v004"]
    assert not _lock_file(tmp_config).exists()
    _no_network(tmp_config)


# ===========================================================================
# 初回生成 × 再生成（同じスタンプ）・GUI 再生成 × CLI
# ===========================================================================
def _initial_and_regen_ready(cfg, sid="003"):
    """003（候補 v001）に、途中で止まった初回生成と regen の印を用意する。"""
    def put(state):
        record = state["stickers"][sid]
        record["initial_run"] = {"since": "2026-01-01T00:00:00.000000", "count": 3, "done": ["v001"],
                                 "reserved": ["v001"], "owner": None}
    vr.update(cfg, put)
    vr.set_verdict(cfg, sid, "v001", "regen")


def test_initial_and_regen_do_not_run_together_on_the_same_sticker(client, tmp_config, box):
    _initial_and_regen_ready(tmp_config)
    hold = _HoldGui(box)
    out = {}
    t = threading.Thread(target=lambda: out.__setitem__(
        "regen", vr.run_regen(tmp_config, _entry("003"), 2, _generator(tmp_config))))
    t.start()
    assert hold.in_call.wait(WAIT)
    initial = vr.run_initial(tmp_config, _entry("003"), 4, _generator(tmp_config))    # regen の実行中
    hold.release.set()
    t.join(WAIT)
    assert initial["claimed"] is False and out["regen"]["complete"] is True
    assert box["calls"] == 2
    record = _record(tmp_config, "003")
    assert record["initial_run"]["owner"] is None                # 初回生成の記録はそのまま（pending）
    assert "regen_run" not in record
    ids = _assert_consistent(tmp_config, "003")
    assert ids == ["v001", "v002", "v003"]
    again = vr.run_initial(tmp_config, _entry("003"), 4, _generator(tmp_config))       # regen の後なら続きを作れる
    assert again["claimed"] is True and again["complete"] is True
    assert _assert_consistent(tmp_config, "003") == ["v001", "v002", "v003", "v004", "v005"]


def test_cli_is_refused_while_gui_regen_runs_and_regen_plan_reports_busy(client, tmp_config, box):
    vr.set_verdict(tmp_config, "003", "v001", "regen")
    plan = client.post("/api/variants/generate", json={"regen_only": True, "count": 2, "dry_run": True},
                       headers=GUI).get_json()
    hold = _HoldGui(box)
    client.post("/api/variants/generate", json={"regen_only": True, "count": 2, "expected_total": plan["total"]},
                headers=GUI)
    assert hold.in_call.wait(WAIT)
    rc, out, err = _finish(_cli(tmp_config, sid="003", count=2))
    hold.release.set()
    _join_jobs()
    assert rc == 1 and BUSY_TEXT in err and _cli_calls(tmp_config) == 0
    assert _assert_consistent(tmp_config, "003") == ["v001", "v002", "v003"] and box["calls"] == 2

    vr.set_verdict(tmp_config, "003", "v002", "regen")          # CLI の実行中は、再生成の計画でも busy
    cli = _cli(tmp_config, sid="003", count=1, mode="hold", tag="c")
    _wait_file(tmp_config.root / "in-call-c")
    d = client.post("/api/variants/generate", json={"regen_only": True, "count": 2, "dry_run": True},
                    headers=GUI).get_json()
    (tmp_config.root / "go-c").write_text("")
    assert _finish(cli)[0] == 0
    assert d["busy"] == ["003"] and d["targets"] == []


# ===========================================================================
# 別のスタンプは互いに止めない
# ===========================================================================
def test_other_stickers_are_not_blocked(client, tmp_config, box):
    hold = _HoldGui(box)
    assert _start_gui_initial(client, "001", 2).status_code == 200 and hold.in_call.wait(WAIT)
    rc, out, err = _finish(_cli(tmp_config, sid="002", count=2))         # GUI 実行中に、別スタンプの CLI
    hold.release.set()
    _join_jobs()
    assert rc == 0 and _cli_calls(tmp_config) == 2
    assert _assert_consistent(tmp_config, "001") == ["v001", "v002"]
    assert _assert_consistent(tmp_config, "002") == ["v001", "v002"]

    cli = _cli(tmp_config, sid="002", count=1, mode="hold", tag="d")    # CLI 実行中に、別スタンプの GUI 再生成
    _wait_file(tmp_config.root / "in-call-d")
    box["before"] = None
    vr.set_verdict(tmp_config, "003", "v001", "regen")
    plan = client.post("/api/variants/generate", json={"regen_only": True, "count": 1, "dry_run": True},
                       headers=GUI).get_json()
    r = client.post("/api/variants/generate",
                    json={"regen_only": True, "count": 1, "expected_total": plan["total"]}, headers=GUI)
    _join_jobs()
    (tmp_config.root / "go-d").write_text("")
    assert _finish(cli)[0] == 0
    assert plan["busy"] == [] and r.status_code == 200
    assert _assert_consistent(tmp_config, "003") == ["v001", "v002"]
    assert _assert_consistent(tmp_config, "002") == ["v001", "v002", "v003"]


# ===========================================================================
# pending・異常終了・正常終了
# ===========================================================================
def test_pending_run_records_alone_do_not_block(client, tmp_config, box):
    """止まった初回生成・再生成の記録（owner なし・止まったプロセス）だけでは、新しい生成を拒否しない。"""
    _initial_and_regen_ready(tmp_config)
    vr.update(tmp_config, lambda s: s["stickers"]["003"].update({"regen_run": {
        "since": "2026-01-01T00:00:00.000000", "count": 2, "done": [], "reserved": [], "owner": _owner(99999999)}}))
    rc, out, err = _finish(_cli(tmp_config, sid="003", count=1))
    assert rc == 0 and _cli_calls(tmp_config) == 1
    d = client.post("/api/variants/initial", json={"ids": ["003"], "count": 4, "dry_run": True},
                    headers=GUI).get_json()
    assert d["target_ids"] == ["003"] and d["busy"] == []


def test_lock_is_released_after_normal_runs_and_the_next_run_starts(client, tmp_config, box):
    assert _start_gui_initial(client, "001", 1).status_code == 200
    _join_jobs()
    assert not _lock_file(tmp_config).exists()
    rc, out, err = _finish(_cli(tmp_config, count=1))
    assert rc == 0 and not _lock_file(tmp_config).exists()
    rc, out, err = _finish(_cli(tmp_config, count=1, tag="b"))
    assert rc == 0
    assert _assert_consistent(tmp_config) == ["v001", "v002", "v003"]


def test_lock_left_by_a_killed_cli_is_recovered(client, tmp_config, box):
    """CLI が実行権を持ったまま異常終了（os._exit）: 残ったロックは回収され、永久に締め出されない。"""
    rc, out, err = _finish(_cli(tmp_config, count=4, mode="die"))
    assert rc == 9 and _lock_file(tmp_config).exists()           # 実行権のロックが残っている
    d = client.post("/api/variants/initial", json={"ids": ["001"], "count": 4, "dry_run": True},
                    headers=GUI).get_json()
    assert d["busy"] == []                                       # 持ち主のいないロックは「実行中」ではない
    rc, out, err = _finish(_cli(tmp_config, count=2, tag="b"))
    assert rc == 0 and _cli_calls(tmp_config, "b") == 2
    assert not _lock_file(tmp_config).exists()
    ids = _assert_consistent(tmp_config)
    assert "v001" not in ids and len(ids) == 2                   # 落ちた CLI が予約した v001 は使い回さない


def test_lock_held_by_a_live_process_blocks(client, tmp_config, box):
    """生きている別プロセスが持つ実行権は「実行中」。古いロックとして外さない。"""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        path = _lock_file(tmp_config)
        path.parent.mkdir(parents=True, exist_ok=True)
        import json as _json
        owner = _owner(child.pid)
        path.write_text(_json.dumps({"pid": owner["pid"], "host": owner["host"], "started": owner["started"],
                                     "created": time.time()}), encoding="utf-8")
        d = client.post("/api/variants/initial", json={"ids": ["001"], "count": 1, "dry_run": True},
                        headers=GUI).get_json()
        rc, out, err = _finish(_cli(tmp_config, count=1))
        assert d["busy"] == ["001"] and rc == 1 and BUSY_TEXT in err and _cli_calls(tmp_config) == 0
        assert path.exists()
    finally:
        child.kill()
        child.wait()


# ===========================================================================
# M3-D: GUI × CLI × repair・採用は実行権で止めない
# ===========================================================================
def test_m3d_gui_cli_and_repair(client, tmp_config, box, monkeypatch):
    real_meta = vr.generation_meta

    def meta(*args, **kwargs):
        vr.repair_state(tmp_config)                              # 登録の前に毎回 repair
        return real_meta(*args, **kwargs)
    monkeypatch.setattr(vr, "generation_meta", meta)
    hold = _HoldGui(box)
    assert _start_gui_initial(client).status_code == 200 and hold.in_call.wait(WAIT)
    t0 = time.monotonic()
    report = vr.repair_state(tmp_config)                         # 実行権を持った生成中でも repair は止まらない
    repair_sec = time.monotonic() - t0
    rc, out, err = _finish(_cli(tmp_config, count=4))
    hold.release.set()
    _join_jobs()
    assert repair_sec < 5 and report["kind"] in ("ok", "missing")
    assert rc == 1 and _cli_calls(tmp_config) == 0 and box["calls"] == 4
    assert _assert_consistent(tmp_config) == ["v001", "v002", "v003", "v004"]
    assert "initial_run" not in _record(tmp_config)


def test_adoption_is_not_blocked_by_the_generation_lock(client, tmp_config, box):
    vr.set_verdict(tmp_config, "003", "v001", "regen")
    plan = client.post("/api/variants/generate", json={"regen_only": True, "count": 1, "dry_run": True},
                       headers=GUI).get_json()
    hold = _HoldGui(box)
    client.post("/api/variants/generate", json={"regen_only": True, "count": 1, "expected_total": plan["total"]},
                headers=GUI)
    assert hold.in_call.wait(WAIT)
    assert _lock_file(tmp_config, "003").exists()
    t0 = time.monotonic()
    r = client.post("/api/variants/003/v001/adopt", json={}, headers=GUI)      # 同じスタンプの採用
    adopt_sec = time.monotonic() - t0
    hold.release.set()
    _join_jobs()
    assert r.status_code == 200, r.get_json()
    assert adopt_sec < 10 and _record(tmp_config, "003")["adopted"] == "v001"
    assert _assert_consistent(tmp_config, "003") == ["v001", "v002"]
