"""STEP 9: GUI の画像生成・再合成・一括退避と、候補の採用（とその巻き戻し）の競合。

generated/<id>.png と final/<id>.png を書き換える処理は、すべてスタンプ単位の鍵
（採用・取り込みと同じ鍵）の中で行い、採用の巻き戻しに消されないことを確かめます。
処理の順序は Event で制御します。画像生成APIは呼びません（生成は差し替えたプロバイダ）。
"""

from __future__ import annotations

import hashlib
import io
import json
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("flask", reason="Flask が未インストールです")

from src import image_generator as ig  # noqa: E402
from src import importer  # noqa: E402
from src import pipeline  # noqa: E402
from src import validator as vd  # noqa: E402
from src import variants as vr  # noqa: E402
from src.csv_loader import StickerEntry  # noqa: E402
from src.providers.base import ImageGenerationProvider  # noqa: E402
from src.text_renderer import TextStyle  # noqa: E402
from tests.test_scoring import sticker_like  # noqa: E402

ENTRY = StickerEntry(id="001", text="了解！", action="敬礼", expression="笑顔", category="basic")
YELLOW, BLUE, PINK, GREEN = (250, 205, 60, 255), (90, 170, 220, 255), (210, 110, 140, 255), (120, 200, 130, 255)
GUI = {"X-Sticker-Client": "1"}
WAIT = 30          # 正しく動けば一瞬で終わる処理の上限（止まったら失敗させるため）


def _png(color) -> bytes:
    buf = io.BytesIO()
    sticker_like(body=color).save(buf, format="PNG")
    return buf.getvalue()


def _sha(path) -> str:
    return hashlib.sha1(Path(path).read_bytes()).hexdigest()


def _state(cfg) -> dict:
    return json.loads(cfg.variants_path.read_text(encoding="utf-8"))


def _leftovers(cfg) -> list[str]:
    root = cfg.root / "output"
    return sorted(str(p.relative_to(root)) for p in root.rglob("*")
                  if p.name.endswith((".tmp", ".lock", ".part")))


class _Gate:
    """生成を「generated に書く直前」で止めるための合図。"""

    def __init__(self):
        self.paused = threading.Event()     # プロバイダが画像を作り終え、書き込み待ちになった
        self.go = threading.Event()         # 書き込みを続けてよい
        self.calls = 0


class _GatedProvider(ImageGenerationProvider):
    """画像を一時ファイルに書いたところで止まる偽のプロバイダ（APIは呼ばない）。"""

    name = "fake"

    def __init__(self, gate: _Gate, data: bytes):
        self.gate, self.data = gate, data

    def generate(self, prompt, reference_image=None, output_path=None):
        self.gate.calls += 1
        self._write(self.data, output_path)
        self.gate.paused.set()
        assert self.gate.go.wait(WAIT), "生成の再開合図が来ませんでした"
        return self.data

    def estimate_cost_usd(self, count):
        return 0.0


class _NG:
    ok = False
    errors = [type("Issue", (), {"message": "NG"})()]
    warnings = []
    issues = errors


@pytest.fixture
def gui(tmp_config, monkeypatch):
    """001 に候補（v001=legacy, v002, v003）があり、v002 を採用中の GUI。"""
    from src.webapp import create_app

    csv_path = tmp_config.root / "data" / "stickers.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text("id,text,action,expression,category\n001,了解！,敬礼,笑顔,basic\n",
                        encoding="utf-8")
    tmp_config.raw["data"]["csv"] = "data/stickers.csv"
    tmp_config.master_image_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_config.master_image_path.write_bytes(_png(YELLOW))
    monkeypatch.setenv("OPENAI_API_KEY", "test-dummy-not-used")    # 偽プロバイダなので通信しない

    (tmp_config.dir_generated / "001.png").write_bytes(_png(YELLOW))
    for color in (BLUE, PINK):
        def reg(state, color=color):
            record = vr.ensure_record(tmp_config, state, "001")
            vid, path = vr.allocate_variant(tmp_config, record, "001")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(_png(color))
            vr.register_variant(tmp_config, record, vid, path, meta={})
        vr.update_sticker(tmp_config, "001", reg)
    vr.adopt(tmp_config, ENTRY, "v002")

    gate = _Gate()
    generated_bytes = _png(GREEN)
    monkeypatch.setattr(ig, "create_provider", lambda config: _GatedProvider(gate, generated_bytes))
    app = create_app(tmp_config)
    app.config["TESTING"] = True
    return app.test_client(), gate, generated_bytes


def _join_job(client) -> dict:
    for t in [t for t in threading.enumerate() if t.name.startswith("job-")]:
        t.join(WAIT)
        assert not t.is_alive(), "ジョブが終わりません（止まっている）"
    return client.get("/api/job").get_json()["job"]


def _run(fn):
    """別スレッドで実行し、(スレッド, 結果) を返します。結果は {"error": 例外 or None}。"""
    out = {"error": None, "done": threading.Event()}

    def target():
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001 - テスト側で確認します
            out["error"] = exc
        finally:
            out["done"].set()
    t = threading.Thread(target=target)
    t.start()
    return t, out


def _final_matches_generated(cfg) -> bool:
    """final が、いまの generated から作った完成画像と同じか（食い違っていないか）。"""
    check = cfg.root / "check_final.png"
    pipeline.render_final(cfg, ENTRY, TextStyle.from_config(cfg), output_path=check)
    same = _sha(check) == _sha(cfg.dir_final / "001.png")
    check.unlink()
    return same


# ===========================================================================
# 9-2 本題: 生成が書き込み直前で止まっている間に採用 → 採用失敗 → 巻き戻し
# ===========================================================================
def test_generation_and_a_failing_adoption_never_interleave(gui, tmp_config, monkeypatch):
    client, gate, generated_bytes = gui
    gen, fin = tmp_config.dir_generated / "001.png", tmp_config.dir_final / "001.png"
    original_a, final_a = _sha(gen), _sha(fin)
    record_before = _state(tmp_config)["stickers"]["001"]

    r = client.post("/api/generate", json={"ids": ["001"], "force": True}, headers=GUI)
    assert r.status_code == 200, r.get_json()
    assert gate.paused.wait(WAIT)                  # 生成は generated に書く直前で止まっている

    validating = threading.Event()
    job_done = threading.Event()

    def validate(*args, **kwargs):
        validating.set()
        gate.go.set()                              # 採用の途中で生成を再開させる
        # 鍵が無ければ、生成はこの間に generated を書き換えてしまう（そのあと巻き戻しが走る）
        job_done.wait(5)
        return _NG()                               # 採用を失敗させ、巻き戻しを起こす
    monkeypatch.setattr(vd, "validate_sticker", validate)
    adopting, adopt = _run(lambda: vr.adopt(tmp_config, ENTRY, "v003"))

    adoption_ran_during_generation = validating.wait(1.0)
    gate.go.set()                                  # 生成を再開（正しければ、採用はここまで待っている）
    job = _join_job(client)
    job_done.set()
    adopting.join(WAIT)
    monkeypatch.undo()

    assert not adopting.is_alive()                 # デッドロックしない
    assert not adoption_ran_during_generation      # 1, 2: 採用は生成が終わるまで待った
    assert isinstance(adopt["error"], vr.AdoptError)
    assert job["status"] == "finished" and job["api_calls"] == 1       # 8: 生成は正常に終わる
    assert _sha(gen) == hashlib.sha1(generated_bytes).hexdigest()      # 3: 生成物が消えていない
    assert _final_matches_generated(tmp_config)                        # 6: final は生成物から
    assert _sha(fin) != final_a
    archived = [_sha(p) for p in importer.archive_dir(tmp_config).glob("001_*.png")]
    assert original_a in archived                                      # 5: 生成前の原画も残る
    assert _state(tmp_config)["stickers"]["001"] == record_before      # 4: 失敗した採用は記録を変えない
    st = vr.get_sticker(tmp_config, "001")
    assert st.adopted == "v002" and st.rollback_failed is None
    assert st.generated_mismatch                  # 生成で差し替わったことは分かる
    assert not list(vr.adopt_backup_root(tmp_config).glob("*"))
    assert _leftovers(tmp_config) == []                                # 7: 一時ファイル・鍵が残らない


# ===========================================================================
# 採用中に生成を始めても、互いに待ち合って止まらない（鍵の順序: スタンプ → 記録）
# ===========================================================================
def test_generation_started_during_an_adoption_waits_without_deadlock(gui, tmp_config, monkeypatch):
    client, gate, generated_bytes = gui
    at_validation, release = threading.Event(), threading.Event()
    real_validate = vd.validate_sticker

    def validate(*args, **kwargs):
        at_validation.set()
        assert release.wait(WAIT)
        return real_validate(*args, **kwargs)
    monkeypatch.setattr(vd, "validate_sticker", validate)
    started = time.monotonic()
    adopting, adopt = _run(lambda: vr.adopt(tmp_config, ENTRY, "v003"))
    assert at_validation.wait(WAIT)                # 採用が鍵を持って途中まで進んでいる

    r = client.post("/api/generate", json={"ids": ["001"], "force": True}, headers=GUI)
    assert r.status_code == 200
    generation_ran_during_adoption = gate.paused.wait(1.0)
    release.set()                                  # 採用を最後まで進める（成功させる）
    adopting.join(WAIT)
    assert gate.paused.wait(WAIT)                  # 採用が終わってから生成が進む
    gate.go.set()
    job = _join_job(client)
    monkeypatch.undo()
    elapsed = time.monotonic() - started

    assert not generation_ran_during_adoption
    assert adopt["error"] is None                  # 採用は成功（鍵の待ち合わせで失敗しない）
    assert elapsed < vr.LOCK_TIMEOUT_SEC           # 9: 鍵の待ち時間切れまで止まっていない
    assert job["status"] == "finished" and job["api_calls"] == 1
    record = _state(tmp_config)["stickers"]["001"]
    assert record["adopted"] == "v003"
    assert record["adopted_file"]["sha1"] == _sha(vr.variant_dir(tmp_config, "001") / "v003.png")
    assert _sha(tmp_config.dir_generated / "001.png") == hashlib.sha1(generated_bytes).hexdigest()
    assert _final_matches_generated(tmp_config)
    assert vr.get_sticker(tmp_config, "001").generated_mismatch       # 生成で差し替わったと分かる
    assert _leftovers(tmp_config) == []


# ===========================================================================
# 再合成（文字のみ）と一括退避も、採用と同時に走らない
# ===========================================================================
def test_rerender_waits_for_the_adoption(gui, tmp_config, monkeypatch):
    client, _gate, _data = gui
    at_validation, release = threading.Event(), threading.Event()
    real_validate, real_render = vd.validate_sticker, pipeline.render_final
    job_renders = []

    def render(*args, **kwargs):
        if threading.current_thread().name.startswith("job-"):
            job_renders.append(at_validation.is_set() and not release.is_set())
        return real_render(*args, **kwargs)

    def validate(*args, **kwargs):
        at_validation.set()
        assert release.wait(WAIT)
        return real_validate(*args, **kwargs)
    monkeypatch.setattr(pipeline, "render_final", render)
    monkeypatch.setattr(vd, "validate_sticker", validate)
    adopting, adopt = _run(lambda: vr.adopt(tmp_config, ENTRY, "v003"))
    assert at_validation.wait(WAIT)

    assert client.post("/api/render", json={"ids": ["001"]}, headers=GUI).status_code == 200
    job_thread = next(t for t in threading.enumerate() if t.name.startswith("job-"))
    job_thread.join(1.0)                           # 鍵が無ければ、この間に再合成してしまう
    release.set()
    adopting.join(WAIT)
    job = _join_job(client)
    monkeypatch.undo()

    assert adopt["error"] is None and job["status"] == "finished"
    assert job_renders == [False]                  # 再合成は採用の途中では一度も走っていない
    assert _final_matches_generated(tmp_config)
    assert not vr.get_sticker(tmp_config, "001").generated_mismatch
    assert _leftovers(tmp_config) == []


def test_bulk_archive_waits_for_a_failing_adoption(gui, tmp_config, monkeypatch):
    client, _gate, _data = gui
    gen, fin = tmp_config.dir_generated / "001.png", tmp_config.dir_final / "001.png"
    original_a, final_a = _sha(gen), _sha(fin)
    at_validation, release = threading.Event(), threading.Event()

    def validate(*args, **kwargs):
        at_validation.set()
        assert release.wait(WAIT)
        return _NG()
    monkeypatch.setattr(vd, "validate_sticker", validate)
    adopting, adopt = _run(lambda: vr.adopt(tmp_config, ENTRY, "v003"))
    assert at_validation.wait(WAIT)

    responses = []
    archiving, archive = _run(lambda: responses.append(
        client.post("/api/generated/archive", json={}, headers=GUI)))
    archived_during_adoption = archive["done"].wait(1.0)
    release.set()
    adopting.join(WAIT)
    archiving.join(WAIT)
    monkeypatch.undo()

    assert not archived_during_adoption            # 採用（巻き戻しを含む）が終わるまで待った
    assert isinstance(adopt["error"], vr.AdoptError) and archive["error"] is None
    assert responses and responses[0].status_code == 200
    assert not gen.exists() and not fin.exists()   # 退避したものが巻き戻しで復活しない
    moved = {p.parent.name: _sha(p) for p in (tmp_config.root / "output" / "archive").glob("2*/*/001.png")}
    assert moved == {"generated": original_a, "final": final_a}      # 採用前の画像が退避された
    assert _leftovers(tmp_config) == []


# ===========================================================================
# 鍵の順序が崩れても、同じプロセス内で永久に止まらない（待ち時間の上限を守る）
# ===========================================================================
def test_opposite_lock_order_in_one_process_times_out_instead_of_hanging(tmp_config):
    """GUI は1つのプロセスの複数スレッドで動くため、スレッド間の待ちにも上限が必要。

    2つのスレッドが2つの鍵を逆の順序で取ると互いに待ち合う。鍵の待ち時間の上限で
    どちらかが LockBusyError になり、もう片方は最後まで進めること（永久に止まらない）。
    """
    first = Path(str(tmp_config.variants_path) + ".order-a.lock")
    second = Path(str(tmp_config.variants_path) + ".order-b.lock")
    both_hold_one = threading.Barrier(2, timeout=WAIT)
    results = {}

    def worker(name, a, b):
        try:
            with vr.file_lock(a, timeout=1.0):
                both_hold_one.wait()                      # 2本とも1つ目の鍵を持った（ここから待ち合う）
                with vr.file_lock(b, timeout=1.0):
                    results[name] = "ok"
        except vr.LockBusyError:
            results[name] = "busy"

    threads = [threading.Thread(target=worker, args=("t1", first, second), daemon=True),
               threading.Thread(target=worker, args=("t2", second, first), daemon=True)]
    started = time.monotonic()
    [t.start() for t in threads]
    [t.join(10) for t in threads]

    assert not any(t.is_alive() for t in threads), "逆順の鍵で永久に止まった（待ち時間の上限が効いていない）"
    assert time.monotonic() - started < 10
    assert "busy" in results.values()                     # 待ち合いは「使用中」として検出される
    assert sorted(results) == ["t1", "t2"]
    assert not first.exists() and not second.exists()     # 鍵ファイルが残らない
