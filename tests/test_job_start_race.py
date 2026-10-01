"""Phase 7 STEP 1: ジョブの同時開始で 500 にならないこと。

GUI のジョブは同時に1件だけです。入口の「実行中か」の確認をほぼ同時に通り抜けた複数の
リクエストのうち、2本目以降は JobManager.start() で断られます。その断りを 500 ではなく
409（既存の /api/variants/generate と同じ形）で返すこと、ジョブは1件しか動かないことを確かめます。
画像生成APIは呼びません（偽のプロバイダ。万一のため API の宛先も閉じたローカルポートにします）。
"""

from __future__ import annotations

import io
import threading
import time

import pytest

pytest.importorskip("flask", reason="Flask が未インストールです")

from src import image_generator as ig  # noqa: E402
from src import providers  # noqa: E402
from src import variants as vr  # noqa: E402
from src import webapp  # noqa: E402
from src.providers.base import ImageGenerationProvider  # noqa: E402
from tests.test_scoring import sticker_like  # noqa: E402

GUI = {"X-Sticker-Client": "1"}
WAIT = 30


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
        if self.box.get("delay"):
            time.sleep(self.box["delay"])
        data = _png()
        self._write(data, output_path)
        return data

    def estimate_cost_usd(self, count):
        return 0.0


@pytest.fixture
def app(tmp_config, monkeypatch):
    from src.webapp import create_app

    csv_path = tmp_config.root / "data" / "stickers.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text("id,text,action,expression,category\n001,了解！,敬礼,笑顔,basic\n",
                        encoding="utf-8")
    tmp_config.raw["data"]["csv"] = "data/stickers.csv"
    tmp_config.master_image_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_config.master_image_path.write_bytes(_png((250, 205, 60, 255)))
    (tmp_config.dir_generated / "001.png").write_bytes(_png((250, 205, 60, 255)))
    monkeypatch.setenv("OPENAI_API_KEY", "test-dummy-not-used")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9")      # 万一の通信も外へ出さない
    box = {"calls": 0, "lock": threading.Lock(), "delay": 0.0}
    fake = lambda config: FakeProvider(box)  # noqa: E731
    monkeypatch.setattr(ig, "create_provider", fake)            # スタンプの生成
    monkeypatch.setattr(providers, "create_provider", fake)     # マスター画像の生成
    flask_app = create_app(tmp_config)
    flask_app.config["TESTING"] = False        # 本番と同じく、捕まえていない例外は 500 になる
    return flask_app, box


def _join_jobs():
    for t in [t for t in threading.enumerate() if t.name.startswith("job-")]:
        t.join(WAIT)
        assert not t.is_alive(), "ジョブが終わりません"


def _fire_together(flask_app, monkeypatch, n, path, body):
    """n 本のリクエストを、入口の「実行中か」の確認を全員が通り抜けてから進ませて同時に送る。"""
    real = webapp.JobManager.is_running
    all_checked = threading.Barrier(n, timeout=5)
    seen = threading.local()

    def is_running(self):
        result = real(self)
        if not getattr(seen, "done", False):         # 各リクエストの入口の確認だけで揃える
            seen.done = True
            try:
                all_checked.wait()
            except threading.BrokenBarrierError:
                pass
        return result
    monkeypatch.setattr(webapp.JobManager, "is_running", is_running)
    start = threading.Barrier(n, timeout=5)
    responses = []

    def request():
        client = flask_app.test_client()
        start.wait()
        r = client.post(path, json=body, headers=GUI)
        responses.append((r.status_code, r.get_json(silent=True)))
    threads = [threading.Thread(target=request) for _ in range(n)]
    [t.start() for t in threads]
    [t.join(WAIT) for t in threads]
    monkeypatch.setattr(webapp.JobManager, "is_running", real)
    _join_jobs()
    return responses


def _assert_one_started_rest_409(responses, n):
    codes = sorted(code for code, _ in responses)
    assert codes == [200] + [409] * (n - 1), codes            # 500 は 1 本も無い
    for code, body in responses:
        assert isinstance(body, dict)                           # HTML の 500 ページではなく JSON
        if code == 409:
            assert "実行中" in body["error"]


@pytest.mark.parametrize("n", [2, 3, 5, 10])
def test_generate_dry_run_started_together_returns_409_not_500(app, monkeypatch, n):
    flask_app, box = app
    responses = _fire_together(flask_app, monkeypatch, n, "/api/generate",
                               {"ids": ["001"], "dry_run": True})
    _assert_one_started_rest_409(responses, n)
    assert box["calls"] == 0                                    # dry-run はプロバイダを呼ばない


@pytest.mark.parametrize("n", [2, 5])
def test_generate_started_together_runs_only_one_job(app, tmp_config, monkeypatch, n):
    flask_app, box = app
    box["delay"] = 0.2
    responses = _fire_together(flask_app, monkeypatch, n, "/api/generate",
                               {"ids": ["001"], "force": True})
    _assert_one_started_rest_409(responses, n)
    assert box["calls"] == 1                                    # 生成は 1 回だけ（二重課金なし）
    assert vr._is_complete_png(tmp_config.dir_generated / "001.png")
    job = flask_app.test_client().get("/api/job").get_json()["job"]
    assert job["kind"] == "generate" and job["status"] == "finished" and job["api_calls"] == 1


def test_render_started_together_returns_409_not_500(app, monkeypatch):
    flask_app, box = app
    responses = _fire_together(flask_app, monkeypatch, 2, "/api/render", {"ids": ["001"]})
    _assert_one_started_rest_409(responses, 2)
    assert box["calls"] == 0


def test_master_generate_started_together_returns_409_not_500(app, monkeypatch):
    flask_app, box = app
    responses = _fire_together(flask_app, monkeypatch, 2, "/api/master/generate", {})
    _assert_one_started_rest_409(responses, 2)
    assert box["calls"] == 1                                    # マスター画像の生成も 1 回だけ


def test_second_request_while_the_provider_is_running_gets_409(app, monkeypatch):
    """Case C: 1 本目がプロバイダを呼んでいる最中の 2 本目。"""
    flask_app, box = app
    in_call, release = threading.Event(), threading.Event()

    class Slow(FakeProvider):
        def generate(self, prompt, reference_image=None, output_path=None):
            in_call.set()
            assert release.wait(WAIT)
            return super().generate(prompt, reference_image, output_path)
    monkeypatch.setattr(ig, "create_provider", lambda config: Slow(box))
    client = flask_app.test_client()
    assert client.post("/api/generate", json={"ids": ["001"], "force": True}, headers=GUI).status_code == 200
    assert in_call.wait(WAIT)
    r = client.post("/api/generate", json={"ids": ["001"], "force": True}, headers=GUI)
    release.set()
    _join_jobs()
    assert r.status_code == 409 and "実行中" in r.get_json()["error"]
    assert box["calls"] == 1


def test_job_busy_error_means_only_that_a_job_is_running():
    """専用の例外は「すでに実行中」だけを表し、既存の except RuntimeError とも互換。"""
    assert issubclass(webapp.JobBusyError, RuntimeError)
    jobs = webapp.JobManager()
    gate = threading.Event()
    jobs.start("test", 1, lambda job: gate.wait(WAIT))
    with pytest.raises(webapp.JobBusyError):
        jobs.start("test", 1, lambda job: None)
    gate.set()


def test_unexpected_errors_are_not_hidden_as_409(app, monkeypatch):
    """ジョブを始められない理由が「実行中」以外なら、409 に隠さない。"""
    flask_app, _box = app

    def broken_start(self, kind, total, target, *args):
        raise RuntimeError("スレッドを作れない（予期しない失敗）")
    monkeypatch.setattr(webapp.JobManager, "start", broken_start)
    client = flask_app.test_client()
    for path, body in (("/api/generate", {"ids": ["001"], "dry_run": True}),
                       ("/api/render", {"ids": ["001"]}), ("/api/master/generate", {})):
        assert client.post(path, json=body, headers=GUI).status_code == 500, path


def test_natural_concurrency_never_returns_500(app, monkeypatch):
    flask_app, _box = app
    seen = {}
    for _ in range(50):
        start = threading.Barrier(5, timeout=5)
        codes = []

        def request():
            start.wait()
            codes.append(flask_app.test_client().post(
                "/api/generate", json={"ids": ["001"], "dry_run": True}, headers=GUI).status_code)
        threads = [threading.Thread(target=request) for _ in range(5)]
        [t.start() for t in threads]
        [t.join(WAIT) for t in threads]
        _join_jobs()
        for code in codes:
            seen[code] = seen.get(code, 0) + 1
    assert 500 not in seen and seen.get(200, 0) >= 50


def test_job_manager_start_admits_exactly_one_job_under_contention():
    """JobManager.start() の「確認 → 登録」は鍵の中で行われ、同時に呼ばれても始まるのは 1 件だけ。"""
    jobs = webapp.JobManager()
    real = webapp.JobManager.is_running
    n = 5
    inside = threading.Barrier(n, timeout=1.0)
    gate = threading.Event()

    def is_running(self):
        result = real(self)
        try:
            inside.wait()          # start() の中の確認を全員が同時に通ろうとする（鍵があれば揃わない）
        except threading.BrokenBarrierError:
            pass
        return result
    started, busy = [], []

    def call():
        try:
            started.append(jobs.start("test", 1, lambda job: gate.wait(WAIT)))
        except webapp.JobBusyError:
            busy.append(1)
    webapp.JobManager.is_running = is_running
    try:
        threads = [threading.Thread(target=call) for _ in range(n)]
        [t.start() for t in threads]
        [t.join(WAIT) for t in threads]
    finally:
        webapp.JobManager.is_running = real
        gate.set()
    assert len(started) == 1 and len(busy) == n - 1
