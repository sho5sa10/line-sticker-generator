"""生成中の repair で候補が二重登録されないこと（Phase 7 STEP 2d・レビュー指摘 C-1）。

候補の PNG を保存してから記録に登録するまでの間は、ロックを持っていません。その間に repair が走ると、
repair が「記録に無い画像」として同じ番号を加え、その後の登録がもう一度加えていました。
- 登録（register_variant）は、同じ番号が既にあれば追加しない（冪等）
- repair は、動いている実行（initial_run / regen_run の owner が生きている）が予約中の番号を加えない
画像生成APIは呼びません（偽のプロバイダ。通信も遮断します。fixture は test_initial_candidates と共通）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("flask", reason="Flask が未インストールです")

from src import variants as vr  # noqa: E402
from tests.test_initial_candidates import (  # noqa: E402,F401 - fixture も読み込む
    GUI, PROJECT, _entry, _generator, _ids, _join_jobs, _png, _record, _run, box, client,
)

WAIT = 30


def _assert_consistent(cfg, sid="001"):
    """variant_id の重複なし・done の重複なし・done ⊆ reserved・PNG はすべて完全。"""
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
    return ids


class _Watch:
    """保存されるどの時点でも: variant の重複なし・done の重複なし・done ⊆ reserved・reserved は減らない・next_seq は戻らない。"""

    def __init__(self, monkeypatch, sid="001"):
        self.problems = []
        self.reserved = {}
        self.next_seq = 0
        real_save = vr.save

        def save(config, data, **kwargs):
            record = (data.get("stickers") or {}).get(sid)
            if isinstance(record, dict):
                ids = [v.get("variant_id") for v in record.get("variants") or [] if isinstance(v, dict)]
                if len(ids) != len(set(ids)):
                    self.problems.append(("variant の重複", ids))
                seq = record.get("next_seq")
                if isinstance(seq, int):
                    if seq < self.next_seq:
                        self.problems.append(("next_seq が戻った", self.next_seq, seq))
                    self.next_seq = max(self.next_seq, seq)
                for key in ("initial_run", "regen_run"):
                    run = record.get(key)
                    owner = run.get("owner") if isinstance(run, dict) else None
                    if not isinstance(owner, dict):
                        continue
                    done, reserved = run.get("done") or [], run.get("reserved") or []
                    if len(done) != len(set(done)) or not set(done) <= set(reserved):
                        self.problems.append((key, "done", list(done), list(reserved)))
                    prev = self.reserved.get((key, owner.get("token")), [])
                    if reserved[:len(prev)] != prev:
                        self.problems.append((key, "reserved が減った", prev, list(reserved)))
                    self.reserved[(key, owner.get("token"))] = list(reserved)
            return real_save(config, data, **kwargs)
        monkeypatch.setattr(vr, "save", save)


def _repair_before_registration(monkeypatch, cfg, times=1, seen=None, sid="001"):
    """PNG を保存した直後・登録する前（generation_meta の呼び出し時）に repair を走らせる。"""
    real_meta = vr.generation_meta
    state = {"n": 0}

    def meta(*args, **kwargs):
        if state["n"] < times:
            state["n"] += 1
            report = vr.repair_state(cfg)
            if seen is not None:
                seen.append((report, json.loads(json.dumps(_record(cfg, sid) or {}))))
        return real_meta(*args, **kwargs)
    monkeypatch.setattr(vr, "generation_meta", meta)
    return state


def _mark_regen_on_003(client, cfg):
    vr.set_verdict(cfg, "003", "v001", "regen")
    plan = client.post("/api/variants/generate", json={"regen_only": True, "count": 2, "dry_run": True},
                       headers=GUI).get_json()
    assert plan["targets"] == [{"id": "003", "count": 2, "resume": False}]
    return plan


# ===========================================================================
# 1〜4: 生成中の repair（初回生成・再生成・既存の候補生成、候補0件・候補あり）
# ===========================================================================
def test_1_3_repair_during_initial_generation_of_a_sticker_without_candidates(client, tmp_config, box,
                                                                             monkeypatch):
    watch = _Watch(monkeypatch)
    state = _repair_before_registration(monkeypatch, tmp_config, times=2)
    r = _run(client, ["001"], 2)
    assert r.status_code == 200 and state["n"] == 2
    assert _assert_consistent(tmp_config) == ["v001", "v002"]
    assert box["calls"] == 2 and "initial_run" not in _record(tmp_config)
    assert watch.problems == []


def test_2_repair_during_regen(client, tmp_config, box, monkeypatch):
    plan = _mark_regen_on_003(client, tmp_config)
    watch = _Watch(monkeypatch, "003")
    state = _repair_before_registration(monkeypatch, tmp_config, times=2)
    client.post("/api/variants/generate",
                json={"regen_only": True, "count": 2, "expected_total": plan["total"]}, headers=GUI)
    _join_jobs()
    assert state["n"] == 2
    assert _assert_consistent(tmp_config, "003") == ["v001", "v002", "v003"]
    assert box["calls"] == 2 and "regen_run" not in _record(tmp_config, "003")
    assert watch.problems == []


@pytest.mark.parametrize("existing", [False, True])
def test_3_4_repair_during_plain_generation(client, tmp_config, box, monkeypatch, existing):
    """既存の候補生成（CLI --variants の本体・実行の記録なし）でも二重登録しない。候補0件・候補あり。"""
    if existing:
        vr.generate_variants(tmp_config, [_entry()], 1, _generator(tmp_config))
    watch = _Watch(monkeypatch)
    _repair_before_registration(monkeypatch, tmp_config)
    vr.generate_variants(tmp_config, [_entry()], 1, _generator(tmp_config))
    expected = ["v001", "v002"] if existing else ["v001"]
    assert _assert_consistent(tmp_config) == expected
    assert watch.problems == []


# ===========================================================================
# 5・6: .part の復元・PNG 保存済みで登録前（repair が実行中の番号を取り込まない）
# ===========================================================================
def test_5_part_recovery_and_repair_do_not_double_register(client, tmp_config, box):
    """止まった実行（owner なし）: .part の番号と、PNG 保存済みで未登録の番号。repair の後に再開しても二重にならない。"""
    folder = vr.variant_dir(tmp_config, "001")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "v001.png").write_bytes(_png())                     # PNG 保存済み・未登録
    (folder / "v002.png.part").write_bytes(_png((30, 60, 200, 255)))    # .part のまま
    vr.update(tmp_config, lambda s: s.setdefault("stickers", {}).update({"001": {
        "adopted": None, "adopted_at": None, "variants": [], "next_seq": 3,
        "initial_run": {"since": "2026-01-01T00:00:00.000000", "count": 3, "done": [],
                        "reserved": ["v001", "v002"], "owner": None}}}))
    vr.repair_state(tmp_config)                                   # 止まった実行の画像は、従来どおり repair が加える
    assert _ids(tmp_config) == ["v001"]
    r = _run(client, ["001"], 4)
    assert r.status_code == 200
    assert _assert_consistent(tmp_config) == ["v001", "v002", "v003"]
    assert box["calls"] == 1 and "initial_run" not in _record(tmp_config)


def test_6_repair_does_not_take_a_number_reserved_by_a_live_run(client, tmp_config, box, monkeypatch):
    """PNG 保存済み・登録前: 動いている実行の予約番号は repair が加えない（登録は生成側が行う）。"""
    seen = []
    _repair_before_registration(monkeypatch, tmp_config, seen=seen)
    _run(client, ["001"], 1)
    report, during = seen[0]
    assert (vr.variant_dir(tmp_config, "001") / "v001.png").exists()
    assert during["variants"] == [] and report["stickers"] == {}        # repair は v001 を加えていない
    assert during["initial_run"]["reserved"] == ["v001"]               # 実行の記録と予約は残っている
    assert during["initial_run"]["owner"] is not None
    assert _assert_consistent(tmp_config) == ["v001"]
    assert _record(tmp_config)["variants"][0]["source"] == vr.SOURCE_API    # 生成側が登録した


# ===========================================================================
# 7: register_variant を同じ番号で2回
# ===========================================================================
def test_7_register_variant_is_idempotent_for_the_same_file(client, tmp_config):
    folder = vr.variant_dir(tmp_config, "001")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "v001.png").write_bytes(_png())
    record = {"adopted": None, "variants": [], "next_seq": 2}
    first = vr.register_variant(tmp_config, record, "v001", folder / "v001.png", meta={"provider": "fake"})
    first["verdict"] = vr.VERDICT_REJECTED                               # 間に人が付けた判断
    second = vr.register_variant(tmp_config, record, "v001", folder / "v001.png",
                                 meta={"provider": "other", "prompt_sha1": "abc"})
    assert [v["variant_id"] for v in record["variants"]] == ["v001"]
    assert second is first
    assert first["verdict"] == vr.VERDICT_REJECTED and first["provider"] == "fake"   # 上書きしない
    assert first["prompt_sha1"] == "abc"                                 # 無い情報だけ補う
    assert record["next_seq"] == 2


def test_7b_register_variant_rejects_the_same_number_for_another_file(client, tmp_config):
    folder = vr.variant_dir(tmp_config, "001")
    folder.mkdir(parents=True, exist_ok=True)
    record = {"adopted": None, "variants": [{"variant_id": "v001", "file": "output/variants/001/other.png"}]}
    with pytest.raises(vr.VariantError):
        vr.register_variant(tmp_config, record, "v001", folder / "v001.png", meta={})
    assert len(record["variants"]) == 1


def test_7c_run_done_is_not_duplicated(client, tmp_config):
    """done に同じ番号を二度加えない（初回生成・再生成のフック）。"""
    for key, hook in (("initial_run", vr.initial_register_hook), ("regen_run", vr.regen_register_hook)):
        state = {"stickers": {"001": {key: {"done": ["v001"], "reserved": ["v001"],
                                            "owner": {"token": "t" * 32}}}}}
        hook("001", "t" * 32)(state, state["stickers"]["001"], "v001")
        assert state["stickers"]["001"][key]["done"] == ["v001"], key


# ===========================================================================
# 8〜12: repair の後も実行の記録・予約・done・next_seq を保つ（生成中）
# ===========================================================================
def test_8_12_run_state_survives_repair_during_generation(client, tmp_config, box, monkeypatch):
    seen = []
    watch = _Watch(monkeypatch)
    _repair_before_registration(monkeypatch, tmp_config, times=3, seen=seen)
    box_before = box["calls"]
    _run(client, ["001"], 3)
    for _report, during in seen:
        run = during["initial_run"]
        assert run["owner"] is not None and run["reserved"] and set(run["done"]) <= set(run["reserved"])
        assert during["next_seq"] == int(run["reserved"][-1][1:]) + 1
    assert _assert_consistent(tmp_config) == ["v001", "v002", "v003"]
    assert box["calls"] - box_before == 3 and watch.problems == []


def test_12_regen_run_survives_repair_during_regen(client, tmp_config, box, monkeypatch):
    plan = _mark_regen_on_003(client, tmp_config)
    seen = []
    _repair_before_registration(monkeypatch, tmp_config, times=2, seen=seen, sid="003")
    client.post("/api/variants/generate",
                json={"regen_only": True, "count": 2, "expected_total": plan["total"]}, headers=GUI)
    _join_jobs()
    assert len(seen) == 2
    for report, during in seen:
        run = during["regen_run"]                                       # repair の後も実行の記録が残る
        assert run["owner"] is not None and set(run["done"]) <= set(run["reserved"])
        assert during["next_seq"] == int(run["reserved"][-1][1:]) + 1
        assert "003" not in report["stickers"]                          # 実行中の予約番号は取り込まない
    assert _assert_consistent(tmp_config, "003") == ["v001", "v002", "v003"]
    assert "regen_run" not in _record(tmp_config, "003")


# ===========================================================================
# initial × regen × repair（同じスタンプ）
# ===========================================================================
@pytest.mark.parametrize("first", ["regen", "initial"])
def test_initial_regen_and_repair_together(client, tmp_config, box, monkeypatch, first):
    """同じスタンプの初回生成と再生成は同時に生成へ入れない（STEP 2e: 生成の実行権）。登録前に repair を挟んでも壊れない。

    先に実行権を取った側だけが生成し、後から始めた側は「始めなかった」（claimed=False）で、画像を作らない。
    先の処理が終わって実行権が外れたら、後の処理を改めて始められる。
    """
    folder = vr.variant_dir(tmp_config, "001")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "v001.png").write_bytes(_png())

    def put(state):
        record = vr.ensure_record(tmp_config, state, "001")
        vr.register_variant(tmp_config, record, "v001", folder / "v001.png", meta={})
        record["next_seq"] = 2
        record["initial_run"] = {"since": "2026-01-01T00:00:00.000000", "count": 3, "done": ["v001"],
                                 "reserved": ["v001"], "owner": None}
    vr.update(tmp_config, put)
    vr.set_verdict(tmp_config, "001", "v001", "regen")
    watch = _Watch(monkeypatch)
    repairs = _repair_before_registration(monkeypatch, tmp_config, times=20)
    runs = {"initial": lambda: vr.run_initial(tmp_config, _entry(), 4, _generator(tmp_config)),
            "regen": lambda: vr.run_regen(tmp_config, _entry(), 2, _generator(tmp_config))}
    second = "initial" if first == "regen" else "regen"
    run_key = {"initial": "initial_run", "regen": "regen_run"}
    lock_path = tmp_config.variants_path.with_name("variants.run-001.lock")
    in_call, release = threading.Event(), threading.Event()

    def hold_first_call(path):
        if not in_call.is_set():
            in_call.set()
            assert release.wait(WAIT)
    box["before"] = hold_first_call
    out = {}
    leader = threading.Thread(target=lambda: out.__setitem__(first, runs[first]()))
    leader.start()
    assert in_call.wait(WAIT)                                    # 先の処理が実行権を持って API を呼んでいる
    before = _record(tmp_config)
    files_before = sorted(p.name for p in folder.iterdir())
    refused = runs[second]()                                     # 後から始めようとした処理
    assert refused["claimed"] is False and refused["complete"] is False
    assert box["calls"] == 1                                     # 後の処理は API を呼ばない
    assert sorted(p.name for p in folder.iterdir()) == files_before    # 画像も作らない
    after = _record(tmp_config)
    assert after.get(run_key[second]) == before.get(run_key[second])   # 後の処理の記録にも触れない
    assert lock_path.exists()
    release.set()
    leader.join(WAIT)
    assert out[first]["claimed"] is True and out[first]["complete"] is True
    assert not lock_path.exists()                                # 実行権は外れた
    record = _record(tmp_config)
    assert run_key[first] not in record                          # 先の処理の記録は片付いた
    _assert_consistent(tmp_config)

    box["before"] = None
    later = runs[second]()                                       # 実行権が外れた後は、改めて始められる
    assert later["claimed"] is True and later["complete"] is True
    ids = _assert_consistent(tmp_config)
    assert sorted(ids) == ["v001", "v002", "v003", "v004", "v005"] and box["calls"] == 4
    record = _record(tmp_config)
    assert "initial_run" not in record and "regen_run" not in record and not lock_path.exists()
    assert repairs["n"] == 4 and watch.problems == []           # 登録の前に毎回 repair を挟んだ


def test_many_generations_with_repeated_repair(client, tmp_config, box):
    """5スレッドの候補生成と、繰り返しの repair: 登録は1件も失われず、重複もしない（ロックが無いと壊れる）。"""
    stop = threading.Event()
    results = []

    def repair_loop():
        while not stop.is_set():
            vr.repair_state(tmp_config)

    def generate():
        results.extend(vr.generate_variants(tmp_config, [_entry()], 3, _generator(tmp_config)))
    repairer = threading.Thread(target=repair_loop)
    repairer.start()
    workers = [threading.Thread(target=generate) for _ in range(5)]
    [t.start() for t in workers]
    [t.join(WAIT * 2) for t in workers]
    stop.set()
    repairer.join(WAIT)
    made = sorted(r["variant_id"] for r in results if r["status"] == "generated")
    assert len(made) == 15
    assert sorted(_assert_consistent(tmp_config)) == made
    assert _record(tmp_config)["next_seq"] == 16


# ===========================================================================
# 13: 別プロセス（子プロセスが PNG 保存後・登録前で待つ間に、親が repair）
# ===========================================================================
_CHILD = r"""
import io, os, socket, sys, time
from pathlib import Path
import yaml
sys.path.insert(0, sys.argv[1])
os.environ["OPENAI_API_KEY"] = "test-dummy-not-used"
os.environ["OPENAI_BASE_URL"] = "http://127.0.0.1:9"
cfg_path, mode = Path(sys.argv[2]), sys.argv[3]
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
            f.write(Path(output_path).name + "\n")
        buf = io.BytesIO()
        sticker_like(body=(200, 90, 60, 255)).save(buf, "PNG")
        self._write(buf.getvalue(), output_path)
        return buf.getvalue()
    def estimate_cost_usd(self, count):
        return 0.0
ig.create_provider = lambda config: Fake()
providers.create_provider = lambda config: Fake()
real_meta = vr.generation_meta
state = {"n": 0}
def meta(*a, **k):
    if state["n"] == 0:                   # 1枚目: PNG を保存した直後・登録の前で、親の repair を待つ
        state["n"] += 1
        (root / "saved").write_text("")
        deadline = time.time() + 30
        while not (root / "go").exists() and time.time() < deadline:
            time.sleep(0.02)
    return real_meta(*a, **k)
vr.generation_meta = meta
cfg = Config(raw=yaml.safe_load(cfg_path.read_text(encoding="utf-8")), root=root, path=cfg_path)
cfg.raw["generation"]["retry_backoff_sec"] = 0
generator = ImageGenerator(cfg, RunLogger(cfg.log_path, echo=False), StateStore(cfg.state_path))
entry = StickerEntry(id="001", text="T", action="手", expression="笑顔", category="basic")
if mode == "initial":
    print(vr.run_initial(cfg, entry, 2, generator)["complete"], flush=True)
else:
    vr.generate_variants(cfg, [entry], 2, generator)
    print("done", flush=True)
"""


@pytest.mark.parametrize("mode", ["initial", "plain"])
def test_13_repair_from_another_process_during_generation(client, tmp_config, box, mode):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(PROJECT),
           "OPENAI_API_KEY": "test-dummy-not-used", "OPENAI_BASE_URL": "http://127.0.0.1:9"}
    child = subprocess.Popen([sys.executable, "-c", _CHILD, str(PROJECT), str(tmp_config.path), mode],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                             env=env, cwd=tmp_config.root)
    root = tmp_config.root
    deadline = time.time() + WAIT
    while not (root / "saved").exists() and time.time() < deadline:
        time.sleep(0.02)
    assert (root / "saved").exists()
    report = vr.repair_state(tmp_config)                     # 子プロセスが登録する前に、このプロセスで repair
    during = _record(tmp_config)
    (root / "go").write_text("")
    out, err = child.communicate(timeout=120)
    assert child.returncode == 0, err
    if mode == "initial":
        assert during["variants"] == [] and report["stickers"] == {}    # 実行中の予約番号は取り込まない
    assert _assert_consistent(tmp_config) == ["v001", "v002"]
    assert len((root / "provider_calls.txt").read_text(encoding="utf-8").split()) == 2
    assert not list(root.glob("network-*.txt"))
    if mode == "initial":
        assert "initial_run" not in _record(tmp_config)


# ===========================================================================
# 14: 実行中の GUI ジョブと、repair の API
# ===========================================================================
def test_14_repair_api_during_a_running_job(client, tmp_config, box, monkeypatch):
    saved, release = threading.Event(), threading.Event()
    real_meta = vr.generation_meta
    state = {"n": 0}

    def meta(*args, **kwargs):
        if state["n"] == 0:
            state["n"] += 1
            saved.set()
            assert release.wait(WAIT)
        return real_meta(*args, **kwargs)
    monkeypatch.setattr(vr, "generation_meta", meta)
    r = client.post("/api/variants/initial", json={"ids": ["001"], "count": 2, "expected_total": 2}, headers=GUI)
    assert r.status_code == 200 and saved.wait(WAIT)
    rep = client.post("/api/variants/repair", json={}, headers=GUI)      # ジョブが PNG を保存し、登録する前
    release.set()
    _join_jobs()
    assert rep.status_code == 200
    assert _assert_consistent(tmp_config) == ["v001", "v002"]
    assert "initial_run" not in _record(tmp_config)
    assert client.get("/api/job").get_json()["job"]["status"] == "finished"
