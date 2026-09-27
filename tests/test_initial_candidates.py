"""初回候補生成のテスト（Phase 7 STEP 2b）。

候補がまだ1件も無いスタンプに、GUI（POST /api/variants/initial）から最初の候補を作ります。
- 実行の記録は initial_run（regen_run とは別。Phase 6 の regen の意味は変えない）
- 番号の予約と initial_run.reserved、候補の登録と initial_run.done は、それぞれ1回の保存
- 途中で落ちても、作れた画像は API を呼ばずに登録し、残りだけを作る
画像生成APIは呼びません（偽のプロバイダ。通信も遮断し、試みがあれば失敗にします）。
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("flask", reason="Flask が未インストールです")

from src import image_generator as ig  # noqa: E402
from src import providers  # noqa: E402
from src import variants as vr  # noqa: E402
from src import webapp  # noqa: E402
from src.csv_loader import StickerEntry  # noqa: E402
from src.image_generator import ImageGenerator  # noqa: E402
from src.logger import RunLogger, StateStore  # noqa: E402
from src.providers import openai_provider  # noqa: E402
from src.providers.base import ImageGenerationProvider, ProviderError  # noqa: E402
from tests.test_scoring import sticker_like  # noqa: E402

PROJECT = Path(__file__).resolve().parents[1]
GUI = {"X-Sticker-Client": "1"}
WAIT = 30
IDS = ["001", "002", "003", "004"]


def _png(color=(120, 200, 130, 255)) -> bytes:
    buf = io.BytesIO()
    sticker_like(body=color).save(buf, format="PNG")
    return buf.getvalue()


class FakeProvider(ImageGenerationProvider):
    """呼び出し回数を数える偽のプロバイダ（APIは呼ばない）。"""

    name = "fake"

    def __init__(self, box):
        self.box = box

    def generate(self, prompt, reference_image=None, output_path=None):
        with self.box["lock"]:
            self.box["calls"] += 1
            n = self.box["calls"]
        if self.box.get("fail"):
            raise ProviderError("偽の失敗")
        if self.box.get("before"):
            self.box["before"](output_path)
        data = _png((20 * n % 255, 120, 200, 255))
        self._write(data, output_path)
        return data

    def estimate_cost_usd(self, count):
        return 0.011 * count


@pytest.fixture
def box(tmp_config, monkeypatch):
    """偽のプロバイダと通信の遮断（試みが1件でもあれば、テストの最後に失敗させます）。"""
    monkeypatch.setenv("OPENAI_API_KEY", "test-dummy-not-used")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9")
    b = {"calls": 0, "created": 0, "lock": threading.Lock(), "network": []}

    def create(config):
        b["created"] += 1
        return FakeProvider(b)

    def connect(self, address):
        b["network"].append(address)
        raise OSError(f"テスト中の通信は禁止です: {address}")

    def real_provider(*args, **kwargs):
        raise AssertionError("実プロバイダが呼ばれました")
    monkeypatch.setattr(ig, "create_provider", create)             # スタンプ・候補の生成
    monkeypatch.setattr(providers, "create_provider", create)      # 関数内で import する経路
    monkeypatch.setattr(openai_provider.OpenAIImageProvider, "generate", real_provider)
    monkeypatch.setattr(socket.socket, "connect", connect)
    yield b
    assert b["network"] == []


@pytest.fixture
def client(tmp_config, box):
    """001・002: 候補なし / 003: 候補1件 / 004: 原画だけ。"""
    from src.webapp import create_app

    csv_path = tmp_config.root / "data" / "stickers.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    rows = "".join(f"{i},テキスト{i},手を振る,笑顔,basic\n" for i in IDS)
    csv_path.write_text("id,text,action,expression,category\n" + rows, encoding="utf-8")
    tmp_config.raw["data"]["csv"] = "data/stickers.csv"
    tmp_config.raw["generation"]["retry_backoff_sec"] = 0
    tmp_config.master_image_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_config.master_image_path.write_bytes(_png((250, 205, 60, 255)))
    folder = vr.variant_dir(tmp_config, "003")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "v001.png").write_bytes(_png())

    def put(state):
        record = vr.ensure_record(tmp_config, state, "003")
        vr.register_variant(tmp_config, record, "v001", folder / "v001.png", meta={})
        record["next_seq"] = 2
    vr.update(tmp_config, put)
    (tmp_config.dir_generated / "004.png").write_bytes(_png((250, 205, 60, 255)))
    box["calls"] = box["created"] = 0
    app = create_app(tmp_config)
    app.config["TESTING"] = False           # 本番と同じく、捕まえていない例外は 500 になる
    return app.test_client()


def _entry(sid="001"):
    return StickerEntry(id=sid, text=f"テキスト{sid}", action="手を振る", expression="笑顔", category="basic")


def _state(cfg) -> dict:
    return json.loads(cfg.variants_path.read_text(encoding="utf-8")) if cfg.variants_path.exists() else {}


def _record(cfg, sid="001") -> dict | None:
    return (_state(cfg).get("stickers") or {}).get(sid)


def _ids(cfg, sid="001") -> list[str]:
    return [v["variant_id"] for v in (_record(cfg, sid) or {}).get("variants", [])]


def _join_jobs():
    for t in [t for t in threading.enumerate() if t.name.startswith("job-")]:
        t.join(WAIT)
        assert not t.is_alive(), "ジョブが終わりません"


def _plan(client, ids, count=None):
    body = {"ids": ids, "dry_run": True}
    if count is not None:
        body["count"] = count
    return client.post("/api/variants/initial", json=body, headers=GUI)


def _run(client, ids, count, expected=None):
    if expected is None:
        expected = _plan(client, ids, count).get_json()["expected_total"]
    r = client.post("/api/variants/initial", json={"ids": ids, "count": count, "expected_total": expected},
                    headers=GUI)
    _join_jobs()
    return r


def _snapshot(cfg):
    """記録と候補フォルダの中身（何も書かないことの確認用）。"""
    files = sorted(str(p.relative_to(cfg.root)) for p in cfg.dir_variants.rglob("*")) \
        if cfg.dir_variants.exists() else []
    raw = cfg.variants_path.read_bytes() if cfg.variants_path.exists() else None
    return raw, files


# ===========================================================================
# A. dry-run
# ===========================================================================
def test_dry_run_reports_targets_without_side_effects(client, tmp_config, box):
    before = _snapshot(tmp_config)
    r = _plan(client, IDS, 4)
    assert r.status_code == 200
    d = r.get_json()
    assert d["target_ids"] == ["001", "002"] and d["target_count"] == 2
    assert d["count"] == 4 and d["total"] == 8 and d["expected_total"] == 8
    assert {s["id"]: s["reason"] for s in d["skipped"]} == {"003": "has_candidates", "004": "has_original"}
    assert d["max_count"] == vr.REGEN_MAX_COUNT and d["max_total"] == vr.REGEN_MAX_TOTAL
    assert d["usd"] is None or d["usd"] >= 0
    assert box["created"] == 0 and box["calls"] == 0            # プロバイダを作らない・呼ばない
    assert _snapshot(tmp_config) == before                      # 記録もファイルも変えない
    assert _record(tmp_config, "001") is None                   # initial_run を作らない
    assert client.get("/api/job").get_json()["job"] is None     # ジョブを始めない


def test_default_count_is_the_regen_default(client):
    d = _plan(client, ["001"]).get_json()
    assert d["count"] == vr.REGEN_DEFAULT_COUNT == 4 and d["total"] == 4


# ===========================================================================
# B. 初回生成
# ===========================================================================
@pytest.mark.parametrize("count", [1, 4])
def test_generates_the_first_candidates(client, tmp_config, box, count):
    r = _run(client, ["001"], count)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["job"]["kind"] == "initial"
    record = _record(tmp_config)
    assert _ids(tmp_config) == [f"v{i:03d}" for i in range(1, count + 1)]
    assert all(v["verdict"] == vr.VERDICT_PENDING for v in record["variants"])
    assert record["adopted"] is None                            # 自動では採用しない
    assert record["next_seq"] == count + 1
    assert "initial_run" not in record                          # 完了処理で消える
    assert "regen_run" not in record and "regen_generated_at" not in record
    for vid in _ids(tmp_config):
        assert vr._is_complete_png(vr.variant_dir(tmp_config, "001") / f"{vid}.png")
    assert not list(vr.variant_dir(tmp_config, "001").glob("*.part"))
    assert not (tmp_config.dir_generated / "001.png").exists()  # 原画は作らない
    assert box["calls"] == count
    job = client.get("/api/job").get_json()["job"]
    assert job["status"] == "finished" and job["api_calls"] == count


def test_generates_for_several_stickers_and_skips_the_rest(client, tmp_config, box):
    r = _run(client, IDS, 2)
    assert r.status_code == 200
    assert _ids(tmp_config, "001") == _ids(tmp_config, "002") == ["v001", "v002"]
    assert _ids(tmp_config, "003") == ["v001"]                  # 候補ありには足さない
    assert _record(tmp_config, "004") is None                   # 原画だけのスタンプには作らない
    assert box["calls"] == 4


def test_listing_shows_the_new_candidates_as_unadopted(client, tmp_config):
    _run(client, ["001"], 1)
    row = next(s for s in client.get("/api/stickers").get_json()["stickers"] if s["id"] == "001")
    assert row["variant_count"] == 1 and row["adopted"] is None and row["initial_run"] is None
    one = client.get("/api/variants/001").get_json()
    assert [v["variant_id"] for v in one["variants"]] == ["v001"] and one["adopted"] is None


def test_the_single_candidate_can_be_adopted_by_hand(client, tmp_config):
    _run(client, ["001"], 1)
    r = client.post("/api/variants/001/v001/adopt", json={}, headers=GUI)
    assert r.status_code == 200, r.get_json()
    assert _record(tmp_config)["adopted"] == "v001"


# ===========================================================================
# C / D. 対象外
# ===========================================================================
def test_stickers_with_candidates_are_not_targeted(client, tmp_config, box):
    before = _snapshot(tmp_config)
    r = _run(client, ["003"], 2, expected=0)
    assert r.status_code == 400 and "対象" in r.get_json()["error"]
    assert _snapshot(tmp_config) == before and box["calls"] == 0


def test_stickers_with_only_an_original_are_not_targeted(client, tmp_config, box):
    r = _run(client, ["004"], 2, expected=0)
    assert r.status_code == 400
    assert _record(tmp_config, "004") is None and box["calls"] == 0 and box["created"] == 0


def test_empty_record_without_original_is_a_new_target(client, tmp_config, box):
    """候補0件の記録（予約後に失敗した跡など）は新規の対象。予約済みの番号は使わない。"""
    vr.update(tmp_config, lambda s: s["stickers"].update(
        {"001": {"adopted": None, "adopted_at": None, "next_seq": 3, "variants": []}}))
    assert _plan(client, ["001"], 1).get_json()["target_ids"] == ["001"]
    _run(client, ["001"], 1)
    assert _ids(tmp_config) == ["v003"]


# ===========================================================================
# E. expected_total
# ===========================================================================
def test_expected_total_mismatch_starts_nothing(client, tmp_config, box):
    before = _snapshot(tmp_config)
    r = _run(client, ["001", "002"], 4, expected=4)             # いまは 8
    assert r.status_code == 409
    assert r.get_json()["total"] == 8
    assert box["calls"] == 0 and box["created"] == 0
    assert _snapshot(tmp_config) == before                      # initial_run も作らない
    assert client.get("/api/job").get_json()["job"] is None


@pytest.mark.parametrize("expected", [None, "8", True, 8.0])
def test_expected_total_must_be_an_integer(client, tmp_config, box, expected):
    body = {"ids": ["001", "002"], "count": 4}
    if expected is not None:
        body["expected_total"] = expected
    r = client.post("/api/variants/initial", json=body, headers=GUI)
    assert r.status_code == 400 and box["calls"] == 0 and box["created"] == 0


# ===========================================================================
# F. 枚数の制限
# ===========================================================================
@pytest.mark.parametrize("count", [0, -1, 9, "4", True, 1.5, None])
def test_invalid_count_is_rejected(client, tmp_config, box, count):
    for dry in (True, False):
        r = client.post("/api/variants/initial",
                        json={"ids": ["001"], "count": count, "dry_run": dry, "expected_total": 4}, headers=GUI)
        assert r.status_code == 400, (count, dry)
    assert box["calls"] == 0 and box["created"] == 0 and _record(tmp_config) is None


def test_total_over_the_limit_is_rejected(tmp_config, box):
    from src.webapp import create_app

    ids = [f"{i:03d}" for i in range(1, 14)]                    # 13 × 8 = 104 枚
    csv_path = tmp_config.root / "data" / "stickers.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text("id,text,action,expression,category\n"
                        + "".join(f"{i},T,手,笑顔,basic\n" for i in ids), encoding="utf-8")
    tmp_config.raw["data"]["csv"] = "data/stickers.csv"
    tmp_config.master_image_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_config.master_image_path.write_bytes(_png())
    client = create_app(tmp_config).test_client()
    for body in ({"dry_run": True}, {"expected_total": 104}):
        r = client.post("/api/variants/initial", json={"ids": ids, "count": 8, **body}, headers=GUI)
        assert r.status_code == 400 and r.get_json()["total"] == 104
    ok = client.post("/api/variants/initial", json={"ids": ids[:12], "count": 8, "dry_run": True}, headers=GUI)
    assert ok.status_code == 200 and ok.get_json()["total"] == 96
    assert box["calls"] == 0 and box["created"] == 0


# ===========================================================================
# ids
# ===========================================================================
@pytest.mark.parametrize("ids", [[], None, "001", ["001", "001"], ["001", "999"], [""], [1]])
def test_invalid_ids_are_rejected(client, tmp_config, box, ids):
    body = {"count": 1, "dry_run": True}
    if ids is not None:
        body["ids"] = ids
    r = client.post("/api/variants/initial", json=body, headers=GUI)
    assert r.status_code == 400, ids
    assert box["created"] == 0 and _record(tmp_config) is None


# ===========================================================================
# 実行前の確認
# ===========================================================================
def test_provider_creation_failure_starts_nothing(client, tmp_config, box, monkeypatch):
    def broken(config):
        raise ProviderError("プロバイダの設定が不正です")
    monkeypatch.setattr(ig, "create_provider", broken)
    before = _snapshot(tmp_config)
    r = _run(client, ["001"], 1, expected=1)
    assert r.status_code == 400 and "プロバイダ" in r.get_json()["error"]
    assert _snapshot(tmp_config) == before and client.get("/api/job").get_json()["job"] is None


def test_missing_master_image_starts_nothing(client, tmp_config, box):
    tmp_config.master_image_path.unlink()
    r = _run(client, ["001"], 1, expected=1)
    assert r.status_code == 400 and box["calls"] == 0 and _record(tmp_config) is None


def test_regen_api_keeps_its_meaning(client, tmp_config, box):
    """既存の /api/variants/generate は regen_only=true だけ。候補なしのスタンプは対象にならない。"""
    r = client.post("/api/variants/generate", json={"regen_only": False, "count": 2, "dry_run": True}, headers=GUI)
    assert r.status_code == 400
    d = client.post("/api/variants/generate", json={"regen_only": True, "count": 2, "dry_run": True},
                    headers=GUI).get_json()
    assert d["targets"] == [] and d["total"] == 0


# ===========================================================================
# G. GUI のジョブ（JobManager / JobBusyError）
# ===========================================================================
def _fire_together(client_app_config, monkeypatch, n, body):
    """n 本のリクエストを、計画を全員が終えてから jobs.start() に進ませて同時に送る。"""
    flask_app = client_app_config
    real = webapp.JobManager.start
    all_planned = threading.Barrier(n, timeout=5)

    def start(self, *args):
        try:
            all_planned.wait()
        except threading.BrokenBarrierError:
            pass
        return real(self, *args)
    monkeypatch.setattr(webapp.JobManager, "start", start)
    go = threading.Barrier(n, timeout=5)
    responses = []

    def request():
        c = flask_app.test_client()
        go.wait()
        r = c.post("/api/variants/initial", json=body, headers=GUI)
        responses.append((r.status_code, r.get_json(silent=True)))
    threads = [threading.Thread(target=request) for _ in range(n)]
    [t.start() for t in threads]
    [t.join(WAIT) for t in threads]
    monkeypatch.setattr(webapp.JobManager, "start", real)
    _join_jobs()
    return responses


@pytest.mark.parametrize("n", [2, 5])
def test_requests_started_together_run_one_job(client, tmp_config, box, monkeypatch, n):
    responses = _fire_together(client.application, monkeypatch, n,
                               {"ids": ["001"], "count": 2, "expected_total": 2})
    codes = sorted(code for code, _ in responses)
    assert codes == [200] + [409] * (n - 1), responses
    for code, body in responses:
        if code == 409:
            assert body == {"error": "すでに処理が実行中です。完了を待つか中止してください。"}
    assert box["calls"] == 2                                    # 1ジョブ分だけ
    assert _ids(tmp_config) == ["v001", "v002"]


def test_second_request_while_the_provider_is_running_gets_409(client, tmp_config, box):
    in_call, release = threading.Event(), threading.Event()

    def hold(path):
        in_call.set()
        assert release.wait(WAIT)
    box["before"] = hold
    r1 = client.post("/api/variants/initial", json={"ids": ["001"], "count": 1, "expected_total": 1}, headers=GUI)
    assert r1.status_code == 200 and in_call.wait(WAIT)
    r2 = client.post("/api/variants/initial", json={"ids": ["002"], "count": 1, "expected_total": 1}, headers=GUI)
    release.set()
    _join_jobs()
    assert r2.status_code == 409 and "実行中" in r2.get_json()["error"]
    assert box["calls"] == 1 and _record(tmp_config, "002") is None


def test_unexpected_runtime_error_is_not_hidden_as_409(client, tmp_config, box, monkeypatch):
    def broken_start(self, kind, total, target, *args):
        raise RuntimeError("スレッドを作れない（予期しない失敗）")
    monkeypatch.setattr(webapp.JobManager, "start", broken_start)
    r = client.post("/api/variants/initial", json={"ids": ["001"], "count": 1, "expected_total": 1}, headers=GUI)
    assert r.status_code == 500


# ===========================================================================
# H. initial_run
# ===========================================================================
def test_initial_run_is_recorded_while_generating(client, tmp_config, box):
    in_call, release = threading.Event(), threading.Event()

    def hold(path):
        in_call.set()
        assert release.wait(WAIT)
    box["before"] = hold
    client.post("/api/variants/initial", json={"ids": ["001"], "count": 2, "expected_total": 2}, headers=GUI)
    assert in_call.wait(WAIT)
    run = _record(tmp_config)["initial_run"]
    release.set()
    _join_jobs()
    assert set(run) == {"since", "count", "done", "reserved", "owner"}
    assert run["count"] == 2 and run["done"] == [] and run["reserved"] == ["v001"]
    assert isinstance(run["since"], str)
    owner = run["owner"]
    assert owner["pid"] == os.getpid() and owner["host"] == vr._this_host()
    assert "started" in owner and isinstance(owner["token"], str) and owner["token"]
    assert "initial_run" not in _record(tmp_config)


def test_every_saved_state_has_registered_candidates_in_done(client, tmp_config, box, monkeypatch):
    """登録と done は同じ保存: 保存された記録のどの時点でも、登録済みの予約番号は done に入っている。"""
    snapshots = []
    real_save = vr.save

    def save(config, data, **kwargs):
        record = (data.get("stickers") or {}).get("001")
        if isinstance(record, dict) and isinstance(record.get("initial_run"), dict):
            snapshots.append(json.loads(json.dumps(record)))
        return real_save(config, data, **kwargs)
    monkeypatch.setattr(vr, "save", save)
    _run(client, ["001"], 3)
    assert snapshots
    for record in snapshots:
        run = record["initial_run"]
        registered = {v["variant_id"] for v in record["variants"]} & set(run["reserved"])
        assert registered == set(run["done"]), record
        assert set(run["done"]) <= set(run["reserved"])


def test_reservation_and_reserved_are_saved_together(client, tmp_config, box, monkeypatch):
    """番号を進めた保存には、必ずその番号の reserved への追加が含まれる。"""
    seen = []
    real_save = vr.save

    def save(config, data, **kwargs):
        record = (data.get("stickers") or {}).get("001")
        if isinstance(record, dict) and isinstance(record.get("initial_run"), dict):
            seen.append((record["next_seq"], list(record["initial_run"]["reserved"])))
        return real_save(config, data, **kwargs)
    monkeypatch.setattr(vr, "save", save)
    _run(client, ["001"], 3)
    for next_seq, reserved in seen:
        assert reserved == [f"v{i:03d}" for i in range(1, next_seq)], seen


def _owner(pid):
    return {"pid": pid, "host": vr._this_host(), "started": vr._process_start(pid)[1], "token": "x" * 32}


def _put_run(cfg, sid="001", **run):
    """途中の実行を置きます。予約済みの番号は、実際の予約と同じく next_seq も進めます。"""
    def put(state):
        record = vr.ensure_record(cfg, state, sid)
        record["initial_run"] = {"since": "2026-01-01T00:00:00.000000", "done": [], "reserved": [], **run}
        reserved = record["initial_run"]["reserved"]
        if reserved:
            record["next_seq"] = max(record["next_seq"], max(int(v[1:]) for v in reserved) + 1)
    vr.update(cfg, put)


def test_a_live_owner_is_not_run_twice(client, tmp_config, box):
    _put_run(tmp_config, count=2, owner=_owner(os.getpid()))     # このプロセスが実行中
    d = _plan(client, ["001"], 2).get_json()
    assert d["target_ids"] == [] and d["busy"] == ["001"]
    assert {s["id"]: s["reason"] for s in d["skipped"]}["001"] == "running"
    r = _run(client, ["001"], 2, expected=0)
    assert r.status_code == 400 and box["calls"] == 0
    outcome = vr.run_initial(tmp_config, _entry(), 2, _generator(tmp_config))
    assert outcome["claimed"] is False and box["calls"] == 0
    assert _record(tmp_config)["initial_run"]["owner"]["pid"] == os.getpid()


def _dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_a_stopped_run_resumes_even_with_candidates(client, tmp_config, box):
    """途中で止まった実行は、候補が既にあっても続きから（残りだけ）。"""
    folder = vr.variant_dir(tmp_config, "001")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "v001.png").write_bytes(_png())

    def put(state):
        record = vr.ensure_record(tmp_config, state, "001")
        vr.register_variant(tmp_config, record, "v001", folder / "v001.png", meta={})
        record["next_seq"] = 2
        record["initial_run"] = {"since": "2026-01-01T00:00:00.000000", "count": 3, "done": ["v001"],
                                 "reserved": ["v001"], "owner": _owner(_dead_pid())}
    vr.update(tmp_config, put)
    d = _plan(client, ["001"], 4).get_json()
    assert [(t["id"], t["count"], t["resume"]) for t in d["targets"]] == [("001", 2, True)]
    assert d["total"] == 2
    _run(client, ["001"], 4)
    assert _ids(tmp_config) == ["v001", "v002", "v003"] and box["calls"] == 2
    assert "initial_run" not in _record(tmp_config)


def test_owner_none_is_a_stopped_run(client, tmp_config, box):
    """失敗・中止で止まった実行（owner=None）も、続きから。"""
    _put_run(tmp_config, count=2, owner=None, reserved=["v001"])
    assert _plan(client, ["001"], 4).get_json()["total"] == 2
    _run(client, ["001"], 4)
    assert _ids(tmp_config) == ["v002", "v003"] and box["calls"] == 2   # 予約済みの v001 は使わない


def test_failure_keeps_the_run_for_later(client, tmp_config, box):
    box["fail"] = True
    _run(client, ["001"], 2)
    record = _record(tmp_config)
    assert record["variants"] == [] and record["initial_run"]["owner"] is None
    assert record["initial_run"]["reserved"] == ["v001", "v002"] and record["initial_run"]["done"] == []
    box["fail"] = False
    assert _plan(client, ["001"], 4).get_json()["targets"][0]["count"] == 2
    _run(client, ["001"], 4)
    assert _ids(tmp_config) == ["v003", "v004"] and "initial_run" not in _record(tmp_config)


def test_cancel_stops_between_candidates_and_resumes_later(client, tmp_config, box):
    def cancel_after_first(path):
        if box["calls"] == 1:
            client.post("/api/job/cancel", headers=GUI)
    box["before"] = cancel_after_first
    _run(client, ["001"], 3)
    assert _ids(tmp_config) == ["v001"] and _record(tmp_config)["initial_run"]["owner"] is None
    box["before"] = None
    _run(client, ["001"], 3)
    assert _ids(tmp_config) == ["v001", "v002", "v003"] and "initial_run" not in _record(tmp_config)


# ===========================================================================
# 途中で落ちた場合（実際の子プロセスを終了させる）
# ===========================================================================
_CHILD = r"""
import io, os, socket, sys, time
from pathlib import Path
import yaml
sys.path.insert(0, sys.argv[1])
os.environ["OPENAI_API_KEY"] = "test-dummy-not-used"
os.environ["OPENAI_BASE_URL"] = "http://127.0.0.1:9"
cfg_path, mode, count, sid = Path(sys.argv[2]), sys.argv[3], int(sys.argv[4]), sys.argv[5]
root = cfg_path.parent.parent

def connect(self, address):
    (root / f"network-{os.getpid()}.txt").write_text(str(address))
    raise OSError("通信は禁止")
socket.socket.connect = connect
from src import image_generator as ig, providers, variants as vr
from src.providers import openai_provider
from src.config import Config
from src.csv_loader import StickerEntry
from src.image_generator import ImageGenerator
from src.logger import RunLogger, StateStore
from src.providers.base import ImageGenerationProvider
from tests.test_scoring import sticker_like

def real_provider(*a, **k):
    raise AssertionError("実プロバイダが呼ばれました")
openai_provider.OpenAIImageProvider.generate = real_provider

class Fake(ImageGenerationProvider):
    name = "fake"
    def generate(self, prompt, reference_image=None, output_path=None):
        with open(root / "provider_calls.txt", "a", encoding="utf-8") as f:
            f.write(f"{os.getpid()} {Path(output_path).name}\n")
        if mode == "reserve":                       # 予約の直後、画像が返る前に落ちる
            os._exit(9)
        if mode == "wait_losers":                   # 同時に始めた他のプロセスが判断を終えるまで待つ
            deadline = time.time() + 20
            while len(list(root.glob("loser-*"))) < count and time.time() < deadline:
                time.sleep(0.02)
        buf = io.BytesIO()
        sticker_like(body=(os.getpid() % 200 + 30, 90, 160, 255)).save(buf, "PNG")
        data = buf.getvalue()
        if mode == "half_part":                     # .part に半分だけ書いたところで落ちる
            Path(output_path).write_bytes(data[: len(data) // 2])
            os._exit(9)
        self._write(data, output_path)
        if mode == "provider_ok":                   # .part を書き終えた直後に落ちる
            os._exit(9)
        return data
    def estimate_cost_usd(self, count):
        return 0.0

ig.create_provider = lambda config: Fake()
providers.create_provider = lambda config: Fake()
cfg = Config(raw=yaml.safe_load(cfg_path.read_text(encoding="utf-8")), root=root, path=cfg_path)
cfg.raw["generation"]["retry_backoff_sec"] = 0
if mode == "png_saved":                             # vNNN.png ができ、登録する直前に落ちる
    vr.generation_meta = lambda *a, **k: os._exit(9)
if mode == "before_finish":                         # 最後の登録の後、締めの前に落ちる
    vr.finish_initial = lambda *a, **k: os._exit(9)
seen = {"n": 0}
def on_event(sid_, vid, status, detail):
    seen["n"] += 1
    if mode == "registered" and seen["n"] == 1:     # 1枚目を登録した直後に落ちる
        os._exit(9)
if mode == "wait_losers":
    while not (root / "go").exists():
        time.sleep(0.01)
generator = ImageGenerator(cfg, RunLogger(cfg.log_path, echo=False), StateStore(cfg.state_path))
entry = StickerEntry(id=sid, text="T", action="手", expression="笑顔", category="basic")
outcome = vr.run_initial(cfg, entry, count, generator, on_event=on_event)
if mode == "wait_losers" and not outcome["claimed"]:
    (root / f"loser-{os.getpid()}").write_text("")
print("claimed" if outcome["claimed"] else "not-claimed", flush=True)
"""


def _child(cfg, mode, count, sid="001", **kwargs):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(PROJECT),
           "OPENAI_API_KEY": "test-dummy-not-used", "OPENAI_BASE_URL": "http://127.0.0.1:9"}
    return subprocess.Popen([sys.executable, "-c", _CHILD, str(PROJECT), str(cfg.path), mode, str(count), sid],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            env=env, **kwargs)


def _crash(cfg, mode, count):
    p = _child(cfg, mode, count)
    out, err = p.communicate(timeout=120)
    assert p.returncode == 9, (out, err)
    assert not list(cfg.root.glob("network-*.txt"))
    calls = cfg.root / "provider_calls.txt"
    return len(calls.read_text(encoding="utf-8").splitlines()) if calls.exists() else 0


def _generator(cfg):
    return ImageGenerator(cfg, RunLogger(cfg.log_path, echo=False), StateStore(cfg.state_path))


def test_crash_right_after_reservation_does_not_reuse_the_number(client, tmp_config, box):
    """Case A: 予約の直後。応答前の1回分は取り戻せないが、番号は使い回さず、残りを作る。"""
    assert _crash(tmp_config, "reserve", 2) == 1
    run = _record(tmp_config)["initial_run"]
    assert run["reserved"] == ["v001"] and run["done"] == []
    d = _plan(client, ["001"], 4).get_json()
    assert [(t["count"], t["resume"]) for t in d["targets"]] == [(2, True)]
    _run(client, ["001"], 4)
    assert _ids(tmp_config) == ["v002", "v003"] and box["calls"] == 2
    assert "initial_run" not in _record(tmp_config)


@pytest.mark.parametrize("count", [1, 2])
def test_crash_after_the_provider_reuses_the_part_file(client, tmp_config, box, count):
    """Case B: .png.part を書き終えた直後。API を呼ばずに .part から登録する。"""
    assert _crash(tmp_config, "provider_ok", count) == 1
    folder = vr.variant_dir(tmp_config, "001")
    assert (folder / "v001.png.part").exists() and not (folder / "v001.png").exists()
    d = _plan(client, ["001"], 4).get_json()
    assert [(t["count"], t["resume"], t["recovered"]) for t in d["targets"]] == [(count - 1, True, 1)]
    assert (folder / "v001.png.part").exists()                  # dry-run では戻さない
    r = _run(client, ["001"], 4)
    assert r.status_code == 200, r.get_json()
    assert box["calls"] == count - 1                            # 復元した分は呼ばない
    assert _ids(tmp_config) == [f"v{i:03d}" for i in range(1, count + 1)]
    assert vr._is_complete_png(folder / "v001.png") and not (folder / "v001.png.part").exists()
    assert "initial_run" not in _record(tmp_config)


def test_crash_after_the_png_is_saved_registers_it(client, tmp_config, box):
    """Case C: vNNN.png ができ、登録する前。API を呼ばずに登録する。"""
    assert _crash(tmp_config, "png_saved", 1) == 1
    assert (vr.variant_dir(tmp_config, "001") / "v001.png").exists() and _ids(tmp_config) == []
    r = _run(client, ["001"], 4)
    assert r.status_code == 200 and box["calls"] == 0
    assert _ids(tmp_config) == ["v001"] and _record(tmp_config)["next_seq"] == 2
    assert "initial_run" not in _record(tmp_config)


def test_crash_after_a_registration_makes_only_the_rest(client, tmp_config, box):
    """Case D: 1枚目を登録した直後。登録済みは作り直さず、残りだけ。"""
    assert _crash(tmp_config, "registered", 2) == 1
    assert _record(tmp_config)["initial_run"]["done"] == ["v001"]
    _run(client, ["001"], 4)
    assert _ids(tmp_config) == ["v001", "v002"] and box["calls"] == 1
    assert "initial_run" not in _record(tmp_config)


def test_crash_before_finish_closes_without_generating(client, tmp_config, box):
    """Case E: 最後の登録の後、締めの前。API を呼ばず、新しい候補も作らずに完了として締める。"""
    assert _crash(tmp_config, "before_finish", 2) == 2
    run = _record(tmp_config)["initial_run"]
    assert run["done"] == ["v001", "v002"]
    d = _plan(client, ["001"], 4).get_json()
    assert [(t["count"], t["resume"]) for t in d["targets"]] == [(0, True)] and d["total"] == 0
    r = _run(client, ["001"], 4)
    assert r.status_code == 200 and box["calls"] == 0
    assert _ids(tmp_config) == ["v001", "v002"] and "initial_run" not in _record(tmp_config)


def test_half_written_part_is_not_registered(client, tmp_config, box):
    """壊れた .part は使わない。番号も使い回さず、新しい番号で作り直す。"""
    assert _crash(tmp_config, "half_part", 1) == 1
    part = vr.variant_dir(tmp_config, "001") / "v001.png.part"
    before = part.read_bytes()
    d = _plan(client, ["001"], 4).get_json()
    assert [(t["count"], t["recovered"]) for t in d["targets"]] == [(1, 0)]
    _run(client, ["001"], 4)
    assert _ids(tmp_config) == ["v002"] and box["calls"] == 1
    assert part.read_bytes() == before and not (part.parent / "v001.png").exists()


def test_broken_png_is_not_registered(client, tmp_config, box):
    folder = vr.variant_dir(tmp_config, "001")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "v001.png").write_bytes(_png()[:40])
    _put_run(tmp_config, count=1, owner=None, reserved=["v001"])
    _run(client, ["001"], 4)
    assert _ids(tmp_config) == ["v002"] and box["calls"] == 1


# ===========================================================================
# 同時実行（スレッド・別プロセス・CLI）
# ===========================================================================
@pytest.mark.parametrize("n", [2, 5])
def test_threads_starting_the_same_sticker_run_it_once(client, tmp_config, box, n):
    gate = threading.Barrier(n, timeout=WAIT)
    outcomes = []

    def work():
        gen = _generator(tmp_config)
        try:
            gate.wait()
        except threading.BrokenBarrierError:
            pass
        outcomes.append(vr.run_initial(tmp_config, _entry(), 2, gen))
    threads = [threading.Thread(target=work) for _ in range(n)]
    [t.start() for t in threads]
    [t.join(WAIT) for t in threads]
    assert sum(o["claimed"] for o in outcomes) == 1
    assert _ids(tmp_config) == ["v001", "v002"] and box["calls"] == 2


@pytest.mark.parametrize("n", [2, 5])
def test_initial_and_plain_generation_in_threads_never_share_a_number(client, tmp_config, box, n):
    """初回生成と、既存の候補生成（CLI の本体）が同時でも、番号は重複しない。"""
    gate = threading.Barrier(n, timeout=WAIT)

    def wait_all(path):
        try:
            gate.wait()          # 全員が番号を予約し終えてから画像を返す
        except threading.BrokenBarrierError:
            pass
    box["before"] = wait_all
    results = []

    def initial():
        results.append(vr.run_initial(tmp_config, _entry(), 1, _generator(tmp_config)))

    def plain():
        results.extend(vr.generate_variants(tmp_config, [_entry()], 1, _generator(tmp_config)))
    threads = [threading.Thread(target=initial)] + [threading.Thread(target=plain) for _ in range(n - 1)]
    [t.start() for t in threads]
    [t.join(WAIT) for t in threads]
    ids = _ids(tmp_config)
    assert sorted(ids) == [f"v{i:03d}" for i in range(1, n + 1)] and len(set(ids)) == n
    assert box["calls"] == n
    assert "initial_run" not in _record(tmp_config)


@pytest.mark.parametrize("n", [2, 5])
def test_processes_starting_the_same_sticker_run_it_once(tmp_config, box, n):
    tmp_config.master_image_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_config.master_image_path.write_bytes(_png())
    procs = [_child(tmp_config, "wait_losers", n - 1) for _ in range(n)]
    (tmp_config.root / "go").write_text("")
    outs = []
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, err
        outs.append(out.strip())
    assert sorted(outs) == ["claimed"] + ["not-claimed"] * (n - 1), outs
    calls = (tmp_config.root / "provider_calls.txt").read_text(encoding="utf-8").splitlines()
    assert len(calls) == n - 1                                  # 1プロセス分（count = n-1 枚）だけ
    assert _ids(tmp_config) == [f"v{i:03d}" for i in range(1, n)]
    assert "initial_run" not in _record(tmp_config)
    assert not list(tmp_config.root.glob("network-*.txt"))


_CLI = r"""
import io, os, runpy, socket, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
os.environ["OPENAI_API_KEY"] = "test-dummy-not-used"
os.environ["OPENAI_BASE_URL"] = "http://127.0.0.1:9"
root = Path(sys.argv[2]).parent.parent

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

class Fake(ImageGenerationProvider):
    name = "fake"
    def generate(self, prompt, reference_image=None, output_path=None):
        with open(root / "cli_calls.txt", "a", encoding="utf-8") as f:
            f.write(Path(output_path).name + "\n")
        buf = io.BytesIO()
        sticker_like(body=(200, 60, 60, 255)).save(buf, "PNG")
        self._write(buf.getvalue(), output_path)
        return buf.getvalue()
    def estimate_cost_usd(self, count):
        return 0.0
ig.create_provider = lambda config: Fake()
providers.create_provider = lambda config: Fake()
sys.argv = ["src.main", "--config", sys.argv[2], "generate", "--id", "001", "--variants", sys.argv[3], "--yes"]
runpy.run_module("src.main", run_name="__main__")
"""


def test_cli_and_initial_api_at_the_same_time(client, tmp_config, box):
    """API の初回生成がプロバイダを呼んでいる間に、CLI の --variants を実プロセスで実行する。"""
    in_call, release = threading.Event(), threading.Event()

    def hold(path):
        if box["calls"] == 1:
            in_call.set()
            assert release.wait(WAIT)
    box["before"] = hold
    r = client.post("/api/variants/initial", json={"ids": ["001"], "count": 3, "expected_total": 3}, headers=GUI)
    assert r.status_code == 200 and in_call.wait(WAIT)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(PROJECT),
           "OPENAI_API_KEY": "test-dummy-not-used", "OPENAI_BASE_URL": "http://127.0.0.1:9"}
    p = subprocess.run([sys.executable, "-c", _CLI, str(PROJECT), str(tmp_config.path), "2"],
                       capture_output=True, text=True, encoding="utf-8", env=env, timeout=120,
                       cwd=tmp_config.root)
    release.set()
    _join_jobs()
    assert p.returncode == 0, (p.stdout, p.stderr)
    record = _record(tmp_config)                                 # JSON として読める
    ids = _ids(tmp_config)
    assert sorted(ids) == ["v001", "v002", "v003", "v004", "v005"] and len(set(ids)) == 5
    assert record["next_seq"] == 6 and "initial_run" not in record
    cli_calls = (tmp_config.root / "cli_calls.txt").read_text(encoding="utf-8").split()
    assert box["calls"] == 3 and sorted(cli_calls) == ["v002.png.part", "v003.png.part"]   # CLI は予約の間の番号
    for vid in ids:
        assert vr._is_complete_png(vr.variant_dir(tmp_config, "001") / f"{vid}.png")
    assert not list(tmp_config.root.glob("network-*.txt"))


# ===========================================================================
# GUI（app.js の判定を node で実行して確かめる・画面の要素）
# ===========================================================================
APP_JS = PROJECT / "src" / "web" / "static" / "app.js"
INDEX = PROJECT / "src" / "web" / "templates" / "index.html"


def _js_function(name: str) -> str:
    src = APP_JS.read_text(encoding="utf-8")
    m = re.search(rf"^function {name}\(.*?^}}", src, re.S | re.M)
    assert m, f"app.js に function {name} がありません"
    return m.group(0)


def _node(code: str):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node が無いため、画面の判定を実行できません")
    p = subprocess.run([node, "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def test_gui_initial_button_is_only_for_stickers_without_candidates():
    cases = [
        {"variant_count": 0, "has_raw": False, "initial_run": None},          # 候補なし → 出す
        {"variant_count": 0, "has_raw": True, "initial_run": None},           # 原画だけ → 出さない
        {"variant_count": 1, "has_raw": False, "initial_run": None},          # 候補あり → 出さない
        {"variant_count": 1, "has_raw": False, "initial_run": "pending"},     # 途中で止まった → 出す（続き）
        {"variant_count": 0, "has_raw": False, "initial_run": "running"},     # 実行中 → 出さない
    ]
    code = _js_function("canStartInitial") + f"\nconsole.log(JSON.stringify({json.dumps(cases)}.map(canStartInitial)))"
    assert _node(code) == [True, False, False, True, False]


def test_gui_compare_opens_for_a_single_unadopted_candidate():
    cases = [
        {"variant_count": 1, "adopted": None},        # 初回生成の1件（未採用）→ 比較画面
        {"variant_count": 1, "adopted": "v001"},      # 原画の v001 だけ → 従来どおり拡大表示
        {"variant_count": 2, "adopted": "v001"},
        {"variant_count": 0, "adopted": None},
    ]
    code = _js_function("canCompare") + f"\nconsole.log(JSON.stringify({json.dumps(cases)}.map(canCompare)))"
    assert _node(code) == [True, False, True, False]


def test_gui_has_the_initial_controls_and_uses_the_initial_api():
    html = INDEX.read_text(encoding="utf-8")
    js = APP_JS.read_text(encoding="utf-8")
    assert 'id="btn-initial"' in html and 'id="initial-count"' in html
    assert "最初の候補を作る" in html
    assert 'data-act="initial"' in js and "canStartInitial(s)" in js
    assert "/api/variants/initial" in js and "expected_total" in js
    assert "AIで作り直す" in js                    # 既存のボタンは残す（別の操作）
    # 一覧の画像・バッジから比較画面を開く条件は canCompare
    assert "canCompare(sticker)" in js and "canCompare(s)" in js
