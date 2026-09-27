"""候補の採番のテスト（Phase 7 STEP 2a）。

記録（variants.json のスタンプ1件分）があって候補が0件（variants=[]）のとき、
番号の予約（next_seq）や実行中の状態を失わず、番号を重複・再利用しないことを確かめます。
同時実行は、同じプロセスのスレッドと、別々のプロセスの両方で確かめます。
画像生成APIは呼びません（偽のプロバイダ。通信も遮断し、試みがあれば失敗にします）。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from src import image_generator as ig
from src import providers
from src import variants as vr
from src.csv_loader import StickerEntry
from src.image_generator import ImageGenerator
from src.logger import RunLogger, StateStore
from src.providers import openai_provider
from src.providers.base import ImageGenerationProvider, ProviderError
from tests.conftest import make_character

PROJECT = Path(__file__).resolve().parents[1]
WAIT = 30


class FakeProvider(ImageGenerationProvider):
    """呼び出し回数を数える偽のプロバイダ。gate があれば、全員が呼び出しに入るまで待ちます。"""

    name = "fake"

    def __init__(self, fail_on: set[int] | None = None, gate: threading.Barrier | None = None) -> None:
        self.calls = 0
        self.fail_on = fail_on or set()
        self.gate = gate
        self.lock = threading.Lock()

    def generate(self, prompt, reference_image=None, output_path=None):
        with self.lock:
            self.calls += 1
            n = self.calls
        if n in self.fail_on:
            raise ProviderError("偽の失敗")
        if self.gate is not None:
            self.gate.wait()        # 全員が番号を予約し終えてから画像を返す（記録前の重なりを作る）
        buf = io.BytesIO()
        make_character((256, 256), color=(20 * n % 255, 90, 160, 255)).save(buf, format="PNG")
        data = buf.getvalue()
        self._write(data, output_path)
        return data

    def estimate_cost_usd(self, count: int) -> float:
        return 0.0


@pytest.fixture(autouse=True)
def no_real_provider(monkeypatch):
    """実プロバイダ・通信に届かないようにし、通信の試みを数えます（最後に 0 件を確かめる）。"""
    monkeypatch.setenv("OPENAI_API_KEY", "test-dummy-not-used")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9")
    attempts = []

    def connect(self, address):
        attempts.append(address)
        raise OSError(f"テスト中の通信は禁止です: {address}")

    def real_provider(*args, **kwargs):
        raise AssertionError("実プロバイダが呼ばれました")
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(openai_provider.OpenAIImageProvider, "generate", real_provider)
    monkeypatch.setattr(ig, "create_provider", real_provider)          # 偽物は _provider に直接入れる
    monkeypatch.setattr(providers, "create_provider", real_provider)
    yield
    assert attempts == []


def _entry(sid="001"):
    return StickerEntry(id=sid, text="了解！", action="敬礼", expression="笑顔", category="basic")


def _generator(cfg, provider):
    master = cfg.master_image_path
    if not master.exists():
        master.parent.mkdir(parents=True, exist_ok=True)
        make_character((256, 256)).save(master)
    gen = ImageGenerator(cfg, RunLogger(cfg.log_path, echo=False), StateStore(cfg.state_path))
    gen._provider = provider
    gen.config.raw["generation"]["retry_backoff_sec"] = 0
    return gen


def _record(cfg, sid="001") -> dict:
    return json.loads(cfg.variants_path.read_text(encoding="utf-8"))["stickers"][sid]


def _ids(cfg, sid="001") -> list[str]:
    return [v["variant_id"] for v in _record(cfg, sid)["variants"]]


def _empty_record(cfg, sid="001", **fields):
    """候補0件の記録を置きます（番号の予約後に失敗した跡など）。"""
    def put(state):
        state.setdefault("stickers", {})[sid] = {
            "adopted": None, "adopted_at": None, "variants": [], **fields}
    vr.update(cfg, put)


def _sha1(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


# --- Test 1 ---------------------------------------------------------------
def test_empty_record_keeps_its_next_seq(tmp_config):
    _empty_record(tmp_config, next_seq=5)
    provider = FakeProvider()
    results = vr.generate_variants(tmp_config, [_entry()], 1, _generator(tmp_config, provider))

    assert [(r["variant_id"], r["status"]) for r in results] == [("v005", "generated")]
    assert _ids(tmp_config) == ["v005"]
    assert _record(tmp_config)["next_seq"] == 6
    assert provider.calls == 1


# --- Test 2 ---------------------------------------------------------------
def test_generating_into_an_empty_record_does_not_replace_the_record(tmp_config):
    run = {"since": "2026-01-01T00:00:00.000000", "count": 2, "done": [], "reserved": ["v001", "v002"],
           "owner": None}
    _empty_record(tmp_config, next_seq=3, regen_run=run,
                  initial_run={"count": 1, "done": [], "reserved": ["v002"], "owner": None},
                  future_field={"kept": True})
    vr.generate_variants(tmp_config, [_entry()], 1, _generator(tmp_config, FakeProvider()))

    record = _record(tmp_config)
    assert _ids(tmp_config) == ["v003"]                  # 予約済みの v001・v002 は使わない
    assert record["regen_run"] == run                    # 実行中の状態・未知の項目を消さない
    assert record["initial_run"] == {"count": 1, "done": [], "reserved": ["v002"], "owner": None}
    assert record["future_field"] == {"kept": True}


def test_ensure_record_returns_the_existing_empty_record(tmp_config):
    state = {"stickers": {"001": {"adopted": None, "variants": [], "next_seq": 7, "x": 1}}}
    existing = state["stickers"]["001"]
    record = vr.ensure_record(tmp_config, state, "001")
    assert record is existing and state["stickers"]["001"] is existing
    assert record["next_seq"] == 7 and record["x"] == 1 and record["variants"] == []
    assert vr.allocate_variant(tmp_config, record, "001")[0] == "v007"


def test_ensure_record_creates_a_record_only_when_there_is_none(tmp_config):
    state = {"stickers": {}}
    record = vr.ensure_record(tmp_config, state, "001")
    assert record == {"adopted": None, "adopted_at": None, "next_seq": 1, "variants": []}
    assert state["stickers"]["001"] is record


def test_empty_record_with_a_legacy_image_keeps_its_fields(tmp_config):
    """原画（generated/<id>.png）だけがある候補0件の記録: 原画を候補にしても、予約と他の項目は残す。"""
    make_character((256, 256)).save(tmp_config.dir_generated / "001.png")
    _empty_record(tmp_config, next_seq=4, future_field="kept")
    vr.generate_variants(tmp_config, [_entry()], 1, _generator(tmp_config, FakeProvider()))

    record = _record(tmp_config)
    assert _ids(tmp_config) == ["v001", "v004"]          # 原画の v001 と、予約の続きの v004
    assert record["adopted"] == "v001" and record["future_field"] == "kept"
    assert record["next_seq"] == 5


# --- Test 3 / 4（スレッド）--------------------------------------------------
@pytest.mark.parametrize("pre", ["no_record", "empty_record"])
@pytest.mark.parametrize("n", [2, 5])
def test_first_candidates_generated_together_in_threads_get_unique_numbers(tmp_config, n, pre):
    if pre == "empty_record":
        _empty_record(tmp_config, next_seq=1)
    provider = FakeProvider(gate=threading.Barrier(n, timeout=WAIT))
    generators = [_generator(tmp_config, provider) for _ in range(n)]
    results = []

    def run(gen):
        results.extend(vr.generate_variants(tmp_config, [_entry()], 1, gen))
    threads = [threading.Thread(target=run, args=(g,)) for g in generators]
    [t.start() for t in threads]
    [t.join(WAIT) for t in threads]

    got = sorted(r["variant_id"] for r in results)
    assert got == [f"v{i:03d}" for i in range(1, n + 1)]            # v001, v001 にならない
    assert all(r["status"] == "generated" for r in results)
    assert sorted(_ids(tmp_config)) == got                          # 記録にも重複が無い
    assert sorted(p.name for p in vr.variant_dir(tmp_config, "001").glob("v*.png")) == \
        [f"{g}.png" for g in got]
    assert provider.calls == n
    assert _record(tmp_config)["next_seq"] == n + 1


# --- Test 3 / 4（別プロセス）------------------------------------------------
_CHILD = r"""
import io, os, socket, sys, time
from pathlib import Path
import yaml
sys.path.insert(0, sys.argv[1])
os.environ["OPENAI_API_KEY"] = "test-dummy-not-used"
os.environ["OPENAI_BASE_URL"] = "http://127.0.0.1:9"
cfg_path, gate, n = Path(sys.argv[2]), Path(sys.argv[3]), int(sys.argv[4])

def connect(self, address):
    (gate / f"network-{os.getpid()}").write_text(str(address))
    raise OSError("通信は禁止")
socket.socket.connect = connect
from src import image_generator as ig, providers, variants as vr
from src.providers import openai_provider
from src.config import Config
from src.csv_loader import StickerEntry
from src.image_generator import ImageGenerator
from src.logger import RunLogger, StateStore
from src.providers.base import ImageGenerationProvider
from tests.conftest import make_character

def real_provider(*a, **k):
    raise AssertionError("実プロバイダが呼ばれました")
openai_provider.OpenAIImageProvider.generate = real_provider

class Fake(ImageGenerationProvider):
    name = "fake"
    def generate(self, prompt, reference_image=None, output_path=None):
        (gate / f"call-{os.getpid()}").write_text(str(output_path))
        deadline = time.time() + 30
        while len(list(gate.glob("call-*"))) < n and time.time() < deadline:
            time.sleep(0.02)        # 全プロセスが番号を予約し終えてから画像を返す
        buf = io.BytesIO()
        make_character((256, 256), color=(os.getpid() % 255, 90, 160, 255)).save(buf, format="PNG")
        data = buf.getvalue()
        self._write(data, output_path)
        return data
    def estimate_cost_usd(self, count):
        return 0.0

ig.create_provider = lambda config: Fake()
providers.create_provider = lambda config: Fake()
cfg = Config(raw=yaml.safe_load(cfg_path.read_text(encoding="utf-8")), root=cfg_path.parent.parent, path=cfg_path)
cfg.raw["generation"]["retry_backoff_sec"] = 0
while not (gate / "go").exists():
    time.sleep(0.01)
generator = ImageGenerator(cfg, RunLogger(cfg.log_path, echo=False), StateStore(cfg.state_path))
entry = StickerEntry(id="001", text="了解！", action="敬礼", expression="笑顔", category="basic")
for r in vr.generate_variants(cfg, [entry], 1, generator):
    print(r["variant_id"], r["status"])
"""


@pytest.mark.parametrize("pre", ["no_record", "empty_record"])
@pytest.mark.parametrize("n", [2, 5])
def test_first_candidates_generated_together_in_processes_get_unique_numbers(tmp_config, n, pre):
    if pre == "empty_record":
        _empty_record(tmp_config, next_seq=1)
    master = tmp_config.master_image_path
    master.parent.mkdir(parents=True, exist_ok=True)
    make_character((256, 256)).save(master)
    gate = tmp_config.root / "gate"
    gate.mkdir()
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(PROJECT),
           "OPENAI_API_KEY": "test-dummy-not-used", "OPENAI_BASE_URL": "http://127.0.0.1:9"}
    procs = [subprocess.Popen([sys.executable, "-c", _CHILD, str(PROJECT), str(tmp_config.path), str(gate), str(n)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                              env=env) for _ in range(n)]
    (gate / "go").write_text("")
    outputs = []
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, err
        outputs.extend(line.split() for line in out.splitlines() if line.strip())

    got = sorted(vid for vid, _status in outputs)
    assert got == [f"v{i:03d}" for i in range(1, n + 1)], outputs      # v001, v001 にならない
    assert all(status == "generated" for _vid, status in outputs)
    assert sorted(_ids(tmp_config)) == got
    assert len(list(gate.glob("call-*"))) == n                            # 1プロセス1回だけ
    assert not list(gate.glob("network-*"))                               # 通信の試みは無い
    assert _record(tmp_config)["next_seq"] == n + 1


# --- Test 5 ---------------------------------------------------------------
def test_number_of_a_failed_generation_is_not_reused(tmp_config):
    provider = FakeProvider(fail_on={1})
    gen = _generator(tmp_config, provider)
    first = vr.generate_variants(tmp_config, [_entry()], 1, gen)
    assert [(r["variant_id"], r["status"]) for r in first] == [("v001", "error")]
    assert _record(tmp_config)["next_seq"] == 2                # 失敗しても予約は残る

    second = vr.generate_variants(tmp_config, [_entry()], 1, gen)
    assert [(r["variant_id"], r["status"]) for r in second] == [("v002", "generated")]
    assert _ids(tmp_config) == ["v002"]
    assert provider.calls == 2


# --- Test 6 ---------------------------------------------------------------
@pytest.mark.parametrize("pre", ["no_record", "empty_record"])
@pytest.mark.parametrize("complete", [True, False])
def test_number_with_a_part_file_is_not_reused(tmp_config, pre, complete):
    if pre == "empty_record":
        _empty_record(tmp_config, next_seq=1)
    folder = vr.variant_dir(tmp_config, "001")
    folder.mkdir(parents=True, exist_ok=True)
    part = folder / "v001.png.part"
    if complete:
        make_character((256, 256)).save(part, format="PNG")      # 課金済みの可能性がある画像
    else:
        part.write_bytes(b"\x89PNG\r\n\x1a\nhalf")
    before = _sha1(part)
    results = vr.generate_variants(tmp_config, [_entry()], 1, _generator(tmp_config, FakeProvider()))

    assert [(r["variant_id"], r["status"]) for r in results] == [("v002", "generated")]
    assert _ids(tmp_config) == ["v002"]
    assert part.exists() and _sha1(part) == before                # 上書き・削除しない
    assert not (folder / "v001.png").exists()


def test_allocate_skips_a_part_file_even_below_next_seq(tmp_config):
    folder = vr.variant_dir(tmp_config, "001")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "v003.png.part").write_bytes(b"partial")
    record = {"variants": [], "next_seq": 3}
    assert vr.allocate_variant(tmp_config, record, "001")[0] == "v004"
    assert record["next_seq"] == 5


# --- Test 7 ---------------------------------------------------------------
@pytest.mark.parametrize("pre", ["no_record", "empty_record"])
def test_next_seq_never_goes_back_after_registration(tmp_config, pre):
    if pre == "empty_record":
        _empty_record(tmp_config, next_seq=1)
    seen = []

    def on_event(sid, variant_id, status, detail):
        seen.append(_record(tmp_config)["next_seq"])
    vr.generate_variants(tmp_config, [_entry()], 3, _generator(tmp_config, FakeProvider()), on_event=on_event)

    assert _ids(tmp_config) == ["v001", "v002", "v003"]
    assert seen == [2, 3, 4]                              # 1枚目の登録後に 1 へ戻らない
