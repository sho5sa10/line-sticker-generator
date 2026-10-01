"""Phase 6: 「再生成（regen）」の印が付いたスタンプだけに、新しい候補を安全に作る。

最優先は課金の防止です。同じ regen の指示で二重に生成しないこと、途中で失敗・中止しても
作れた候補を作り直さないこと、dry-run ではプロバイダを作らないことを確かめます。
画像生成APIは呼びません（偽のプロバイダで呼び出し回数を数えます）。
"""

from __future__ import annotations

import io
import json
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("flask", reason="Flask が未インストールです")

from src import image_generator as ig  # noqa: E402
from src import variants as vr  # noqa: E402
from src.csv_loader import StickerEntry  # noqa: E402
from src.providers import estimate_cost_usd  # noqa: E402
from src.providers.base import ImageGenerationProvider, ProviderError  # noqa: E402
from tests.test_scoring import sticker_like  # noqa: E402

GUI = {"X-Sticker-Client": "1"}
WAIT = 30
IDS = ["001", "002", "003"]
ENTRY1 = StickerEntry(id="001", text="了解！", action="敬礼", expression="笑顔", category="basic")


def _png(color=(120, 200, 130, 255)) -> bytes:
    buf = io.BytesIO()
    sticker_like(body=color).save(buf, format="PNG")
    return buf.getvalue()


def _state(cfg) -> dict:
    return json.loads(cfg.variants_path.read_text(encoding="utf-8"))


def _record(cfg, sid) -> dict:
    return _state(cfg)["stickers"][sid]


def _ids(cfg, sid) -> list[str]:
    return [v["variant_id"] for v in _record(cfg, sid)["variants"]]


def _item(cfg, sid, vid) -> dict:
    return next(v for v in _record(cfg, sid)["variants"] if v["variant_id"] == vid)


class FakeProvider(ImageGenerationProvider):
    """呼び出し回数を数える偽のプロバイダ（APIは呼ばない）。"""

    name = "fake"

    def __init__(self, box):
        self.box = box

    def generate(self, prompt, reference_image=None, output_path=None):
        box = self.box
        with box["lock"]:
            box["calls"] += 1
            n = box["calls"]
        hook = box.get("on_call")
        if hook:
            hook(n)
        if n in box.get("fail_on", ()):
            raise ProviderError(f"偽のプロバイダが {n} 回目で失敗しました")
        data = _png((40 + n * 20 % 200, 120, 200 - n * 10 % 150, 255))
        self._write(data, output_path)
        return data

    def estimate_cost_usd(self, count):
        return estimate_cost_usd("gpt-image-1", "medium", count)


@pytest.fixture
def env(tmp_config, monkeypatch):
    """001・002 に regen の印、003 は印なし。GUI のテストクライアント付き。"""
    from src.webapp import create_app

    csv_path = tmp_config.root / "data" / "stickers.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text("id,text,action,expression,category\n"
                        "001,了解！,敬礼,笑顔,basic\n002,OK,手,笑顔,basic\n003,はい,手,笑顔,basic\n",
                        encoding="utf-8")
    tmp_config.raw["data"]["csv"] = "data/stickers.csv"
    tmp_config.master_image_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_config.master_image_path.write_bytes(_png((250, 205, 60, 255)))
    monkeypatch.setenv("OPENAI_API_KEY", "test-dummy-not-used")        # 偽プロバイダなので通信しない
    monkeypatch.delenv("IMAGE_PROVIDER", raising=False)
    for sid in IDS:
        for _ in range(2):
            def reg(state, sid=sid):
                record = vr.ensure_record(tmp_config, state, sid)
                vid, path = vr.allocate_variant(tmp_config, record, sid)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(_png())
                vr.register_variant(tmp_config, record, vid, path, meta={})
            vr.update_sticker(tmp_config, sid, reg)
    vr.set_verdict(tmp_config, "001", "v002", "regen")
    vr.set_verdict(tmp_config, "002", "v001", "regen")

    box = {"calls": 0, "lock": threading.Lock(), "created": 0}

    def factory(config):
        box["created"] += 1
        return FakeProvider(box)
    monkeypatch.setattr(ig, "create_provider", factory)
    app = create_app(tmp_config)
    app.config["TESTING"] = True
    return app.test_client(), box


def _post(client, **body):
    return client.post("/api/variants/generate", json={"regen_only": True, **body}, headers=GUI)


def _plan(client, count=4):
    r = _post(client, count=count, dry_run=True)
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def _run(client, count=4):
    """dry-run で確認した合計を添えて実行し、ジョブの終了を待ちます（GUI と同じ流れ）。"""
    plan = _plan(client, count)
    r = _post(client, count=count, expected_total=plan["total"])
    assert r.status_code == 200, r.get_json()
    return _join_job(client)


def _join_job(client) -> dict:
    for t in [t for t in threading.enumerate() if t.name.startswith("job-")]:
        t.join(WAIT)
        assert not t.is_alive(), "ジョブが終わりません"
    return client.get("/api/job").get_json()["job"]


# ===========================================================================
# 対象の判定
# ===========================================================================
def test_marking_regen_records_the_time(env, tmp_config):
    item = _item(tmp_config, "001", "v002")
    assert item["verdict"] == "regen" and item["regen_marked_at"]
    first = item["regen_marked_at"]
    vr.set_verdict(tmp_config, "001", "v002", "regen")          # 付け直すと更新される
    assert _item(tmp_config, "001", "v002")["regen_marked_at"] > first


def test_only_stickers_marked_regen_are_targets(env, tmp_config):
    plan = vr.regen_plan(tmp_config, IDS, 4)
    assert [t["id"] for t in plan["targets"]] == ["001", "002"]      # 003 は印が無い
    vr.set_verdict(tmp_config, "002", "v001", "rejected")          # 印を外すと対象外
    assert [t["id"] for t in vr.regen_plan(tmp_config, IDS, 4)["targets"]] == ["001"]


@pytest.mark.parametrize("generated_at, marked_at, expected", [
    (None, "2026-09-27T10:00:00.000000", True),                        # 生成したことがない
    ("2026-09-27T10:00:00.000000", "2026-09-27T11:00:00.000000", True),  # あとで付け直した
    ("2026-09-27T10:05:00.000000", "2026-09-27T10:00:00.000000", False),  # 生成済み
    ("2026-09-27T10:00:00.000000", "2026-09-27T10:00:00.000000", False),  # 同時刻は生成済み
    (None, None, True),                                            # 以前のデータ（日時なし）
    ("2026-09-27T10:00:00.000000", None, False),                   # 以前のデータで生成済み
    ("2026-09-27T10:00:00", "2026-09-27T10:00:00.5", True),        # 秒までの旧形式と混在
])
def test_target_rule_with_timestamps(env, tmp_config, generated_at, marked_at, expected):
    def mutate(state):
        record = state["stickers"]["001"]
        item = next(v for v in record["variants"] if v["variant_id"] == "v002")
        if marked_at is None:
            item.pop("regen_marked_at", None)
        else:
            item["regen_marked_at"] = marked_at
        if generated_at is None:
            record.pop("regen_generated_at", None)
        else:
            record["regen_generated_at"] = generated_at
    vr.update(tmp_config, mutate)
    ids = [t["id"] for t in vr.regen_plan(tmp_config, ["001"], 4)["targets"]]
    assert (ids == ["001"]) is expected


# ===========================================================================
# dry-run・見積もり・入力の検証（プロバイダは作らない）
# ===========================================================================
def test_dry_run_never_creates_a_provider(env, tmp_config, monkeypatch):
    client, box = env

    def forbidden(config):
        raise AssertionError("dry-run でプロバイダを作ってはいけない")
    monkeypatch.setattr(ig, "create_provider", forbidden)
    monkeypatch.delenv("OPENAI_API_KEY")                       # APIキーも使わない
    before = tmp_config.variants_path.read_bytes()
    r = _post(client, count=3, dry_run=True)
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert [t["id"] for t in body["targets"]] == ["001", "002"]
    assert [t["count"] for t in body["targets"]] == [3, 3]
    assert body["total"] == 6 and body["count"] == 3
    assert body["usd"] == estimate_cost_usd(tmp_config.model, tmp_config.quality, 6)
    assert tmp_config.variants_path.read_bytes() == before     # 何も書かない
    assert box["calls"] == 0


@pytest.mark.parametrize("count", [0, 9, -1, "4", True, 2.5, None])
def test_count_is_validated_by_the_api(env, count):
    client, box = env
    body = {"dry_run": True}
    if count is not None:
        body["count"] = count
    r = _post(client, **body)
    if count is None:                                          # 省略時は既定値 4
        assert r.status_code == 200 and r.get_json()["count"] == vr.REGEN_DEFAULT_COUNT == 4
    else:
        assert r.status_code == 400
    assert box["calls"] == 0


def test_total_over_the_run_limit_is_rejected(env, tmp_config):
    client, box = env
    csv_rows = ["id,text,action,expression,category"]
    for n in range(1, 14):
        sid = f"{n:03d}"
        csv_rows.append(f"{sid},セリフ{n},手,笑顔,basic")
        if n > 3:
            def reg(state, sid=sid):
                record = vr.ensure_record(tmp_config, state, sid)
                vid, path = vr.allocate_variant(tmp_config, record, sid)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(_png())
                vr.register_variant(tmp_config, record, vid, path, meta={})
            vr.update_sticker(tmp_config, sid, reg)
        vr.set_verdict(tmp_config, sid, "v001", "regen")
    (tmp_config.root / "data" / "stickers.csv").write_text("\n".join(csv_rows) + "\n", encoding="utf-8")
    assert _plan(client, 7)["total"] == 91                     # 13 件 × 7 枚 = 91（上限内）
    r = _post(client, count=8, dry_run=True)                   # 13 × 8 = 104 > 100
    assert r.status_code == 400 and "100" in r.get_json()["error"]
    r = _post(client, count=8, expected_total=104)
    assert r.status_code == 400
    assert box["calls"] == 0


def test_regen_only_must_be_true(env):
    client, _box = env
    r = client.post("/api/variants/generate", json={"dry_run": True}, headers=GUI)
    assert r.status_code == 400


def test_missing_api_key_is_reported_before_anything_starts(env, tmp_config, monkeypatch):
    client, box = env
    monkeypatch.delenv("OPENAI_API_KEY")
    before = tmp_config.variants_path.read_bytes()
    r = _post(client, count=2, expected_total=4)
    assert r.status_code == 400 and "OPENAI_API_KEY" in r.get_json()["error"]
    assert tmp_config.variants_path.read_bytes() == before and box["calls"] == 0


def test_provider_setting_errors_are_reported(env, tmp_config, monkeypatch):
    client, box = env
    monkeypatch.setattr(ig, "create_provider", lambda config: (_ for _ in ()).throw(
        ValueError("未対応のプロバイダです: nope")))
    r = _post(client, count=2, expected_total=4)
    assert r.status_code == 400 and "nope" in r.get_json()["error"]
    assert box["calls"] == 0


def test_execution_requires_the_confirmed_total(env):
    """確認画面で見た合計（expected_total）が無い・違う場合は、生成を始めない。"""
    client, box = env
    assert _post(client, count=2).status_code == 400
    r = _post(client, count=2, expected_total=5)               # 実際は 2 件 × 2 = 4
    assert r.status_code == 409
    assert box["calls"] == 0


# ===========================================================================
# 実行・二重生成の防止
# ===========================================================================
def test_run_generates_only_for_targets_and_keeps_the_regen_mark(env, tmp_config):
    client, box = env
    before = {sid: _ids(tmp_config, sid) for sid in IDS}
    started = vr._regen_now()
    job = _run(client, count=2)

    assert job["status"] == "finished" and job["api_calls"] == 4 and box["calls"] == 4
    assert _ids(tmp_config, "001") == before["001"] + ["v003", "v004"]
    assert _ids(tmp_config, "002") == before["002"] + ["v003", "v004"]
    assert _ids(tmp_config, "003") == before["003"]           # 印の無いスタンプは作らない
    for sid, vid in (("001", "v002"), ("002", "v001")):
        assert _item(tmp_config, sid, vid)["verdict"] == "regen"          # 人の判断は変えない
        record = _record(tmp_config, sid)
        assert started <= record["regen_generated_at"] <= vr._regen_now()   # 開始時刻
        assert "regen_run" not in record
    for sid in ("001", "002"):
        for vid in ("v003", "v004"):
            assert _item(tmp_config, sid, vid)["verdict"] == "pending"


def test_rerunning_after_completion_generates_nothing(env, tmp_config):
    client, box = env
    _run(client, count=2)
    calls, state = box["calls"], tmp_config.variants_path.read_bytes()
    assert _plan(client, 2)["total"] == 0                      # 対象 0 件
    r = _post(client, count=2, expected_total=0)
    assert r.status_code == 400 and "対象" in r.get_json()["error"]
    assert box["calls"] == calls and tmp_config.variants_path.read_bytes() == state


def test_regen_marked_during_a_run_stays_a_target(env, tmp_config):
    """そのスタンプの生成を始めた後に付けた regen は、次回の対象として残る。"""
    client, box = env

    def on_call(n):
        if n == 1:          # 001 の生成中に、001 の別の候補へ regen を付ける
            vr.set_verdict(tmp_config, "001", "v001", "regen")
        if n == 2:          # 002 の生成中に、002 の候補へ regen を付け直す
            vr.set_verdict(tmp_config, "002", "v001", "regen")
    box["on_call"] = on_call
    _run(client, count=1)
    box.pop("on_call")
    plan = _plan(client, 1)
    assert [t["id"] for t in plan["targets"]] == ["001", "002"]    # 次回の対象として残る


def test_regen_marked_before_the_sticker_starts_is_covered_by_the_same_run(env, tmp_config):
    """同じ実行の中でも、そのスタンプの生成を始める前に付いた regen は、その生成で済む。"""
    client, box = env

    def on_call(n):
        if n == 1:          # 001 の生成中（002 はまだ始まっていない）に 002 へ regen を付け直す
            vr.set_verdict(tmp_config, "002", "v001", "regen")
    box["on_call"] = on_call
    _run(client, count=1)
    box.pop("on_call")
    assert _plan(client, 1)["targets"] == []                   # 002 は付け直した後に作ったので済み
    record = _record(tmp_config, "002")
    assert _item(tmp_config, "002", "v001")["regen_marked_at"] < record["regen_generated_at"]


def test_partial_failure_keeps_successes_and_retries_only_the_rest(env, tmp_config):
    client, box = env
    box["fail_on"] = {3}                                       # 001 の 3 枚目だけ失敗
    job = _run(client, count=4)
    assert job["status"] == "finished"
    record = _record(tmp_config, "001")
    assert "regen_generated_at" not in record                  # 成功扱いにしない
    assert record["regen_run"]["done"] == ["v003", "v004", "v006"]   # v005 は失敗（番号は再利用しない）
    assert _record(tmp_config, "002").get("regen_generated_at")      # 002 は全部成功

    box["fail_on"] = set()
    plan = _plan(client, 4)
    assert [(t["id"], t["count"]) for t in plan["targets"]] == [("001", 1)]   # 残り 1 枚だけ
    calls = box["calls"]
    _run(client, count=4)
    assert box["calls"] == calls + 1                           # 成功済みの 3 枚は作り直さない
    record = _record(tmp_config, "001")
    assert record["regen_generated_at"] and "regen_run" not in record
    assert _ids(tmp_config, "001") == ["v001", "v002", "v003", "v004", "v006", "v007"]
    assert not (vr.variant_dir(tmp_config, "001") / "v005.png").exists()


def test_cancelled_run_is_resumed_without_regenerating(env, tmp_config):
    client, box = env
    cancelled = threading.Event()

    def on_call(n):
        if n == 2 and not cancelled.is_set():
            cancelled.set()
            assert client.post("/api/job/cancel", json={}, headers=GUI).status_code == 200
    box["on_call"] = on_call
    job = _run(client, count=3)
    box.pop("on_call")
    assert job["status"] == "cancelled"
    assert "regen_generated_at" not in _record(tmp_config, "001")
    done = _record(tmp_config, "001")["regen_run"]["done"]
    assert len(done) == 2                                      # 2 枚目まで作って止まった
    calls = box["calls"]
    _run(client, count=3)
    assert box["calls"] == calls + 1 + 3                       # 001 の残り 1 枚 + 002 の 3 枚
    assert _record(tmp_config, "001")["regen_generated_at"]


def test_two_requests_at_once_start_only_one_run(env, tmp_config):
    client, box = env
    total = _plan(client, 2)["total"]
    gate, release = threading.Event(), threading.Event()

    def on_call(n):
        if n == 1:
            gate.set()
            assert release.wait(WAIT)
    box["on_call"] = on_call
    barrier = threading.Barrier(2, timeout=WAIT)
    codes = []

    def request():
        barrier.wait()
        codes.append(_post(client, count=2, expected_total=total).status_code)
    threads = [threading.Thread(target=request) for _ in range(2)]
    [t.start() for t in threads]
    [t.join(WAIT) for t in threads]
    assert gate.wait(WAIT)
    release.set()
    _join_job(client)
    assert sorted(codes) == [200, 409]                         # 2 本目は 500 ではなく 409
    assert box["calls"] == total                               # 二重に生成しない


def test_claim_is_taken_by_only_one_runner(env, tmp_config, monkeypatch):
    """2 つの処理が同時に同じスタンプの生成を始めようとしても、始められるのは 1 つだけ。"""
    barrier = threading.Barrier(2, timeout=1.0)
    real_read = vr._read_state_file

    def read(config):
        try:
            barrier.wait()          # 2 本とも記録を読んだところで揃える（鍵があれば揃わない）
        except threading.BrokenBarrierError:
            pass
        return real_read(config)
    monkeypatch.setattr(vr, "_read_state_file", read)
    results = []
    threads = [threading.Thread(target=lambda: results.append(vr.begin_regen(tmp_config, "001", 2)))
               for _ in range(2)]
    [t.start() for t in threads]
    [t.join(WAIT) for t in threads]
    monkeypatch.undo()
    claimed = [r for r in results if r is not None]
    assert len(claimed) == 1
    assert _record(tmp_config, "001")["regen_run"]["owner"]["token"] == claimed[0]["token"]


def test_a_run_owned_by_a_live_process_is_skipped(env, tmp_config):
    first = vr.begin_regen(tmp_config, "001", 2)
    assert first is not None
    assert vr.begin_regen(tmp_config, "001", 2) is None       # 実行中は他から始めない
    plan = vr.regen_plan(tmp_config, ["001"], 2)
    assert plan["targets"] == [] and plan["busy"] == ["001"]
    vr.finish_regen(tmp_config, "001", first["token"])        # 何も作らずに終わった = 途中失敗
    assert [t["id"] for t in vr.regen_plan(tmp_config, ["001"], 2)["targets"]] == ["001"]


def test_run_left_by_a_dead_process_is_resumed(env, tmp_config):
    first = vr.begin_regen(tmp_config, "001", 3)

    def crash(state):           # 持ち主のプロセスが落ちた（存在しない PID）
        state["stickers"]["001"]["regen_run"]["owner"]["pid"] = 999_999_991
        state["stickers"]["001"]["regen_run"]["done"] = ["v003"]
    vr.update(tmp_config, crash)
    plan = vr.regen_plan(tmp_config, ["001"], 3)
    assert [(t["id"], t["count"], t["resume"]) for t in plan["targets"]] == [("001", 2, True)]
    again = vr.begin_regen(tmp_config, "001", 3)
    assert again is not None and again["remaining"] == 2 and again["since"] == first["since"]


# ===========================================================================
# 生成中の他の操作（記録を壊さない）
# ===========================================================================
@pytest.mark.parametrize("action", ["adopt", "rating", "verdict", "repair"])
def test_other_operations_during_a_run_are_kept(env, tmp_config, action):
    client, box = env
    gate, release = threading.Event(), threading.Event()

    def on_call(n):
        if n == 1:
            gate.set()
            assert release.wait(WAIT)
    box["on_call"] = on_call
    total = _plan(client, 2)["total"]
    assert _post(client, count=2, expected_total=total).status_code == 200
    assert gate.wait(WAIT)                                     # 生成が API を呼んでいる最中

    started = time.monotonic()
    if action == "adopt":
        vr.adopt(tmp_config, ENTRY1, "v001")
    elif action == "rating":
        vr.set_rating(tmp_config, "001", "v001", 5)
    elif action == "verdict":
        vr.set_verdict(tmp_config, "003", "v001", "rejected")
    else:
        extra = vr.variant_dir(tmp_config, "003") / "v009.png"
        extra.write_bytes(_png())
        vr.repair_state(tmp_config)
    assert time.monotonic() - started < 10                     # API 呼び出し中もロックで待たせない
    release.set()
    job = _join_job(client)

    assert job["status"] == "finished"
    assert vr.state_status(tmp_config) == vr.STATE_OK
    if action == "adopt":
        assert _record(tmp_config, "001")["adopted"] == "v001"
    elif action == "rating":
        assert _item(tmp_config, "001", "v001")["human_rating"] == 5
    elif action == "verdict":
        assert _item(tmp_config, "003", "v001")["verdict"] == "rejected"
    else:
        assert "v009" in _ids(tmp_config, "003")
    for sid in ("001", "002"):
        assert _ids(tmp_config, sid)[-2:] == ["v003", "v004"]     # 新しい候補も欠けない
        assert _record(tmp_config, sid)["regen_generated_at"]


# ===========================================================================
# 外部からの操作・Host
# ===========================================================================
def test_cross_site_and_foreign_host_are_rejected(env):
    client, box = env
    r = client.post("/api/variants/generate", data="{}", content_type="text/plain",
                    headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post("/api/variants/generate", json={"regen_only": True, "dry_run": True},
                    headers={"Host": "evil.example:8765", **GUI})
    assert r.status_code == 403
    assert box["calls"] == 0 and box["created"] == 0
