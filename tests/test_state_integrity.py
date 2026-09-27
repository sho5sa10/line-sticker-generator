"""Phase 5b 再修正：独立レビューで再現した問題の回帰テスト。

評価処理と他の操作が同時に走っても variants.json が壊れず、どの変更も消えないこと、
採用に失敗しても原画・完成画像・記録が元に戻ること、などを固定します。
画像生成APIは呼びません。
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from src import importer
from src import pipeline
from src import scoring
from src import validator as vd
from src import variants as vr
from src.csv_loader import StickerEntry
from tests.test_scoring import sticker_like

ENTRY = StickerEntry(id="001", text="了解！", action="敬礼", expression="笑顔", category="basic")
ENTRY2 = StickerEntry(id="002", text="OK", action="手を挙げる", expression="笑顔", category="basic")
YELLOW, BLUE, PINK, GREEN = (250, 205, 60, 255), (90, 170, 220, 255), (210, 110, 140, 255), (120, 200, 130, 255)
PROJECT = Path(__file__).resolve().parents[1]


def _sha(path) -> str:
    return hashlib.sha1(Path(path).read_bytes()).hexdigest()


def _state(cfg) -> dict:
    return json.loads(cfg.variants_path.read_text(encoding="utf-8"))


def _item(cfg, sid, vid) -> dict:
    return next(v for v in _state(cfg)["stickers"][sid]["variants"] if v["variant_id"] == vid)


def _add_candidates(cfg, sid="001", colors=(BLUE, PINK)):
    """generated（legacy）＋候補を、正式な経路（update）で用意します。"""
    sticker_like(body=YELLOW).save(cfg.dir_generated / f"{sid}.png", format="PNG")
    for color in colors:
        _generate_one(cfg, sid, color)


def _generate_one(cfg, sid="001", color=GREEN) -> str:
    def reg(state):
        record = vr.ensure_record(cfg, state, sid)
        vid, path = vr.allocate_variant(cfg, record, sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        sticker_like(body=color).save(path, format="PNG")
        vr.register_variant(cfg, record, vid, path, meta={})
        return vid
    return vr.update(cfg, reg)


def _run_in_thread(fn, *args):
    errors = []

    def target():
        try:
            fn(*args)
        except BaseException as exc:  # noqa: BLE001 - テスト側で確認します
            errors.append(exc)
    t = threading.Thread(target=target)
    t.start()
    t.join(30)
    return errors


def _while_measuring(monkeypatch, action):
    """評価の計算中（＝古い状態を読んだあと、保存する前）に action を1回だけ実行します。"""
    original = scoring.measure
    done = []

    def measure(path):
        if not done:
            done.append(1)
            errors = _run_in_thread(action)
            assert not errors, errors
        return original(path)
    monkeypatch.setattr(scoring, "measure", measure)


@pytest.fixture
def web(tmp_config):
    """CSVを用意したテスト用クライアント（001 / 002）。"""
    pytest.importorskip("flask", reason="Flask が未インストールです")
    from src.webapp import create_app

    csv_path = tmp_config.root / "data" / "stickers.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text("id,text,action,expression,category\n001,了解！,敬礼,笑顔,basic\n"
                        "002,OK,手を挙げる,笑顔,basic\n", encoding="utf-8")
    tmp_config.raw["data"]["csv"] = "data/stickers.csv"
    app = create_app(tmp_config)
    app.config["TESTING"] = True
    return app.test_client()


def _assert_state_readable(cfg):
    assert vr.state_status(cfg) == vr.STATE_OK
    json.loads(cfg.variants_path.read_text(encoding="utf-8"))     # JSONとして読める


# ===========================================================================
# CR-1 / H-1: 評価処理も「鍵 → 読み直し → 自分の変更だけ → 原子的保存」を守る
# ===========================================================================
def test_a_two_scorings_at_once_keep_both_results(tmp_config, monkeypatch):
    _add_candidates(tmp_config)
    barrier = threading.Barrier(2)
    original = scoring.measure

    def measure(path):
        out = original(path)
        barrier.wait(timeout=10)          # 2本とも計算を終えてから保存させる
        return out
    monkeypatch.setattr(scoring, "measure", measure)
    threads = [threading.Thread(target=scoring.score_variant, args=(tmp_config, "001", v))
               for v in ("v002", "v003")]
    [t.start() for t in threads]
    [t.join(30) for t in threads]

    _assert_state_readable(tmp_config)
    assert _item(tmp_config, "001", "v002").get("derived_scores")
    assert _item(tmp_config, "001", "v003").get("derived_scores")


def test_b_verdict_during_scoring_is_kept(tmp_config, monkeypatch):
    _add_candidates(tmp_config)
    _while_measuring(monkeypatch, lambda: vr.set_verdict(tmp_config, "001", "v003", "rejected"))
    scoring.score_all(tmp_config, ["001"], force=True)

    _assert_state_readable(tmp_config)
    assert _item(tmp_config, "001", "v003")["verdict"] == "rejected"
    assert _item(tmp_config, "001", "v002").get("derived_scores")


def test_b2_rating_during_scoring_is_kept(tmp_config, monkeypatch):
    _add_candidates(tmp_config)
    _while_measuring(monkeypatch, lambda: vr.set_rating(tmp_config, "001", "v001", 5))
    scoring.score_all(tmp_config, ["001"], force=True)
    assert _item(tmp_config, "001", "v001")["human_rating"] == 5


def test_c_adoption_during_scoring_is_not_rolled_back(tmp_config, monkeypatch):
    _add_candidates(tmp_config)
    vr.adopt(tmp_config, ENTRY, "v002")
    _while_measuring(monkeypatch, lambda: vr.adopt(tmp_config, ENTRY, "v003"))
    scoring.score_variant(tmp_config, "001", "v001")

    st = vr.get_sticker(tmp_config, "001")
    assert st.adopted == "v003"
    assert not st.generated_mismatch
    assert _sha(tmp_config.dir_generated / "001.png") == _sha(st.find("v003").path(tmp_config))


def test_d_candidate_generated_during_scoring_is_kept(tmp_config, monkeypatch):
    _add_candidates(tmp_config)
    _while_measuring(monkeypatch, lambda: _generate_one(tmp_config, "001"))
    scoring.score_all(tmp_config, ["001"], force=True)

    ids = [v["variant_id"] for v in _state(tmp_config)["stickers"]["001"]["variants"]]
    assert ids == ["v001", "v002", "v003", "v004"]
    on_disk = sorted(p.stem for p in vr.variant_dir(tmp_config, "001").glob("v*.png"))
    assert on_disk == ids                                 # 記録から落ちた候補が無い


def test_e_scoring_one_sticker_does_not_undo_adoption_of_another(tmp_config, monkeypatch):
    _add_candidates(tmp_config, "001")
    _add_candidates(tmp_config, "002", colors=(BLUE,))
    _while_measuring(monkeypatch, lambda: vr.adopt(tmp_config, ENTRY2, "v002"))
    scoring.score_all(tmp_config, ["001"], force=True)

    st = vr.get_sticker(tmp_config, "002")
    assert st.adopted == "v002"
    assert _state(tmp_config)["stickers"]["002"].get("adopted_file")
    assert not st.generated_mismatch


def test_score_ignores_result_when_candidate_file_changed(tmp_config, monkeypatch):
    """計算中に候補の参照先が変わったら、古い画像の結果は書き込みません。"""
    _add_candidates(tmp_config)

    def repoint():
        def mutate(state):
            item = next(v for v in state["stickers"]["001"]["variants"] if v["variant_id"] == "v002")
            item["file"] = "output/variants/001/v003.png"
        vr.update(tmp_config, mutate)
    _while_measuring(monkeypatch, repoint)
    with pytest.raises(scoring.ScoringError, match="変更された"):
        scoring.score_variant(tmp_config, "001", "v002")
    assert "derived_scores" not in _item(tmp_config, "001", "v002")


def test_score_still_refuses_corrupt_state(tmp_config):
    sticker_like().save(tmp_config.dir_generated / "001.png", format="PNG")
    tmp_config.variants_path.write_text("{壊れ", encoding="utf-8")
    with pytest.raises(vr.StateCorruptError):
        scoring.score_all(tmp_config, ["001"])
    with pytest.raises(vr.StateCorruptError):
        scoring.score_variant(tmp_config, "001", "v001")
    assert tmp_config.variants_path.read_text(encoding="utf-8") == "{壊れ"


def test_scoring_module_never_saves_a_stale_state():
    """評価処理が load() した状態を直接 save() する書き方に戻っていないこと。"""
    source = (PROJECT / "src" / "scoring.py").read_text(encoding="utf-8")
    assert "vr.save(" not in source
    assert "vr.load(" not in source


# --- 原子的保存：一時ファイルを共有しない -------------------------------------
def test_save_uses_a_unique_temp_file_each_time(tmp_config, monkeypatch):
    names = []
    original = vr._replace_file

    def spy(src, dst):
        names.append(Path(src).name)
        return original(src, dst)
    monkeypatch.setattr(vr, "_replace_file", spy)
    vr.save(tmp_config, {"stickers": {}})
    vr.save(tmp_config, {"stickers": {}})

    assert len(names) == 2 and names[0] != names[1]
    assert all(n != "variants.json.tmp" for n in names)
    leftovers = [p.name for p in tmp_config.variants_path.parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_failed_save_removes_its_temp_file_and_keeps_the_old_state(tmp_config, monkeypatch):
    vr.save(tmp_config, {"stickers": {"001": {"variants": [], "memo": "keep"}}})
    before = tmp_config.variants_path.read_text(encoding="utf-8")

    def fail(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(vr, "_replace_file", fail)
    with pytest.raises(OSError):
        vr.save(tmp_config, {"stickers": {}})
    assert tmp_config.variants_path.read_text(encoding="utf-8") == before
    assert [p for p in tmp_config.variants_path.parent.iterdir() if p.name.endswith(".tmp")] == []


# --- 同時実行（スレッド）: 評価も含めた全種類を一定時間まわす -------------------
def test_g_h_mixed_concurrent_operations_keep_state_valid(tmp_config):
    _add_candidates(tmp_config, "001", colors=(BLUE, PINK, GREEN))
    _add_candidates(tmp_config, "002", colors=(BLUE,))
    stop = threading.Event()
    errors, last = [], {}
    alternate = itertools.cycle(["v003", "v002"])          # 採用する候補を交互に（時刻に依存しない）

    def loop(fn):
        while not stop.is_set():
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}")

    def rate():
        n = (last.get("n", 0) % 5) + 1
        vr.set_rating(tmp_config, "001", "v004", n)
        last["n"] = n

    def verdict():
        v = "rejected" if last.get("v") != "rejected" else "regen"
        vr.set_verdict(tmp_config, "002", "v002", v)
        last["v"] = v

    ops = [lambda: scoring.score_all(tmp_config, ["001", "002"], force=True),
           lambda: scoring.score_variant(tmp_config, "001", "v002"),
           rate, verdict,
           lambda: vr.adopt(tmp_config, ENTRY, next(alternate)),
           lambda: _generate_one(tmp_config, "002")]
    threads = [threading.Thread(target=loop, args=(op,)) for op in ops]
    [t.start() for t in threads]
    time.sleep(4)
    stop.set()
    [t.join(60) for t in threads]

    assert errors == []
    _assert_state_readable(tmp_config)
    data = _state(tmp_config)
    ids = [v["variant_id"] for v in data["stickers"]["002"]["variants"]]
    on_disk = sorted(p.stem for p in vr.variant_dir(tmp_config, "002").glob("v*.png"))
    assert ids == on_disk and len(ids) == len(set(ids))
    assert _item(tmp_config, "001", "v004")["human_rating"] == last["n"]
    assert _item(tmp_config, "002", "v002")["verdict"] == last["v"]
    st = vr.get_sticker(tmp_config, "001")
    assert not st.generated_mismatch
    assert _sha(tmp_config.dir_generated / "001.png") == _sha(st.find(st.adopted).path(tmp_config))
    leftovers = [p.name for p in tmp_config.variants_path.parent.iterdir()
                 if p.name.endswith((".tmp", ".lock"))]
    assert leftovers == []


# --- 別プロセス ---------------------------------------------------------------
_WORKER = r"""
import sys
sys.path.insert(0, sys.argv[1])
from src import scoring, variants as vr
from src.config import Config
from src.csv_loader import StickerEntry
import yaml
from pathlib import Path
cfg_path = Path(sys.argv[2])
raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
cfg = Config(raw=raw, root=cfg_path.parent.parent, path=cfg_path)
mode, n = sys.argv[3], int(sys.argv[4])
entry = StickerEntry(id="001", text="了解！", action="敬礼", expression="笑顔", category="basic")
for k in range(n):
    if mode == "score":
        scoring.score_all(cfg, ["001"], force=True)
    elif mode.startswith("rate:"):
        vr.set_rating(cfg, "001", mode[5:], (k % 5) + 1)
    elif mode == "adopt":
        vr.adopt(cfg, entry, "v002" if k % 2 else "v003")
print("ok")
"""


def _spawn(cfg, mode, n):
    return subprocess.Popen(
        [sys.executable, "-c", _WORKER, str(PROJECT), str(cfg.path), mode, str(n)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"})


def test_f_scoring_rating_and_adoption_from_separate_processes(tmp_config):
    _add_candidates(tmp_config, colors=(BLUE, PINK, GREEN))
    procs = [_spawn(tmp_config, "score", 6), _spawn(tmp_config, "rate:v001", 15),
             _spawn(tmp_config, "rate:v004", 15), _spawn(tmp_config, "adopt", 4)]
    outs = [p.communicate(timeout=240) for p in procs]
    for (out, err), p in zip(outs, procs):
        assert p.returncode == 0, err
        assert out.strip().endswith("ok")

    _assert_state_readable(tmp_config)
    assert _item(tmp_config, "001", "v001")["human_rating"] == 5     # 15回目は (14 % 5) + 1
    assert _item(tmp_config, "001", "v004")["human_rating"] == 5
    assert _item(tmp_config, "001", "v002").get("derived_scores")
    st = vr.get_sticker(tmp_config, "001")
    assert st.adopted in ("v002", "v003") and not st.generated_mismatch


# ===========================================================================
# H-3 / M1: 採用に失敗したら、原画・完成画像・記録・退避・採用状態をすべて元に戻す
# ===========================================================================
def _snapshot(cfg):
    gen = cfg.dir_generated / "001.png"
    fin = cfg.dir_final / "001.png"
    return {
        "generated": (_sha(gen), gen.stat().st_mtime_ns) if gen.exists() else None,
        "final": _sha(fin) if fin.exists() else None,
        "state": cfg.variants_path.read_text(encoding="utf-8") if cfg.variants_path.exists() else None,
        "archive": sorted(p.name for p in importer.archive_dir(cfg).glob("*.png")),
        "adopted": vr.get_sticker(cfg, "001").adopted if vr.get_sticker(cfg, "001") else None,
    }


def _raise(exc):
    def fail(*args, **kwargs):
        raise exc
    return fail


def _fail_for_candidates(original):
    """候補（variants/ 配下）からのコピーだけを失敗させます（採用前の控えは作れる）。"""
    def copyfile(src, dst, *args, **kwargs):
        if "variants" in Path(src).parts:
            raise OSError("容量不足")
        return original(src, dst, *args, **kwargs)
    return copyfile


class _NG:
    ok = False
    errors = [type("Issue", (), {"message": "NG"})()]
    warnings = []
    issues = errors


FAILURES = {
    "archive": lambda mp: mp.setattr(importer, "archive_existing", _raise(PermissionError("書けない"))),
    "candidate_copy": lambda mp: mp.setattr(vr.shutil, "copyfile", _fail_for_candidates(vr.shutil.copyfile)),
    "render_final": lambda mp: mp.setattr(pipeline, "render_final", _raise(RuntimeError("合成失敗"))),
    "validator": lambda mp: mp.setattr(vd, "validate_sticker", lambda *a, **k: _NG()),
    "state_save": lambda mp: mp.setattr(vr, "update", _raise(vr.VariantError("鍵を取れない"))),
    "ctrl_c": lambda mp: mp.setattr(pipeline, "render_final", _raise(KeyboardInterrupt())),
}


@pytest.mark.parametrize("stage", list(FAILURES))
def test_failed_adoption_restores_everything(tmp_config, monkeypatch, stage):
    _add_candidates(tmp_config)
    vr.adopt(tmp_config, ENTRY, "v002")
    before = _snapshot(tmp_config)

    FAILURES[stage](monkeypatch)
    with pytest.raises(BaseException):
        vr.adopt(tmp_config, ENTRY, "v003")
    monkeypatch.undo()

    after = _snapshot(tmp_config)
    assert after == before                 # 原画（中身と更新日時）・完成画像・記録・退避・採用状態
    assert not vr.get_sticker(tmp_config, "001").generated_mismatch
    leftovers = [p.name for p in tmp_config.variants_path.parent.iterdir()
                 if p.name.endswith((".tmp", ".lock"))]
    assert leftovers == []
    assert not list(vr.adopt_backup_root(tmp_config).glob("*"))       # 一時退避も残さない


def test_generated_replace_failure_restores_everything(tmp_config, monkeypatch):
    """候補を generated へ置く最後の置き換えだけが失敗した場合。"""
    _add_candidates(tmp_config)
    vr.adopt(tmp_config, ENTRY, "v002")
    before = _snapshot(tmp_config)
    target = tmp_config.dir_generated / "001.png"
    original = vr._replace_file

    def replace(src, dst):
        if Path(dst) == target and "v003" not in str(src) and Path(src).suffix == ".tmp":
            raise OSError("置き換え失敗")
        return original(src, dst)
    monkeypatch.setattr(vr, "_replace_file", replace)
    with pytest.raises(OSError):
        vr.adopt(tmp_config, ENTRY, "v003")
    monkeypatch.undo()
    assert _snapshot(tmp_config) == before


def test_archive_failure_never_deletes_the_adopted_image(tmp_config, monkeypatch):
    _add_candidates(tmp_config)
    vr.adopt(tmp_config, ENTRY, "v002")
    current = _sha(tmp_config.dir_generated / "001.png")
    monkeypatch.setattr(importer, "archive_existing", _raise(PermissionError("書けない")))
    with pytest.raises(PermissionError):
        vr.adopt(tmp_config, ENTRY, "v003")
    assert _sha(tmp_config.dir_generated / "001.png") == current


def test_first_adoption_failure_leaves_nothing_behind(tmp_config, monkeypatch):
    """採用前に原画も完成画像も無かった場合は、失敗後も無いまま。"""
    vr.update(tmp_config, lambda s: None)
    _generate_one(tmp_config, "001", BLUE)
    monkeypatch.setattr(vd, "validate_sticker", lambda *a, **k: _NG())
    with pytest.raises(vr.AdoptError):
        vr.adopt(tmp_config, ENTRY, "v001")
    assert not (tmp_config.dir_generated / "001.png").exists()
    assert not (tmp_config.dir_final / "001.png").exists()
    assert vr.get_sticker(tmp_config, "001").adopted is None


def test_rollback_failure_is_reported_and_backup_is_kept(tmp_config, monkeypatch, capsys):
    """巻き戻し自体が失敗したら、成功したことにせず、はっきり知らせて一時退避を残す。"""
    _add_candidates(tmp_config)
    vr.adopt(tmp_config, ENTRY, "v002")
    monkeypatch.setattr(vd, "validate_sticker", lambda *a, **k: _NG())    # final は書き換わる
    monkeypatch.setattr(vr, "_restore_file", _raise(OSError("戻せない")))
    with pytest.raises(vr.AdoptError) as info:
        vr.adopt(tmp_config, ENTRY, "v003")
    monkeypatch.undo()

    notes = " ".join(getattr(info.value, "__notes__", []))
    assert "巻き戻しに失敗" in notes
    assert "ERROR" in capsys.readouterr().err
    backups = list(vr.adopt_backup_root(tmp_config).glob("*"))
    assert backups                                               # 手で戻せるように残す
    st = vr.get_sticker(tmp_config, "001")
    assert st.rollback_failed                                    # 記録にも残る
    assert st.to_dict()["rollback_failed"]
    vr.adopt(tmp_config, ENTRY, "v002")                          # 採用し直せば解消
    assert not vr.get_sticker(tmp_config, "001").rollback_failed


# ===========================================================================
# H-4: Windows の一時的な読み書き競合（PermissionError）を「壊れている」にしない
# ===========================================================================
def _flaky_read(monkeypatch, cfg, failures):
    """variants.json の読み取りを、最初の failures 回だけ PermissionError にします。"""
    original = vr._read_file_bytes
    count = {"n": 0}

    def read_bytes(path):
        if Path(path) == cfg.variants_path and count["n"] < failures:
            count["n"] += 1
            raise PermissionError(13, "アクセスが拒否されました", str(path))
        return original(path)
    monkeypatch.setattr(vr, "_read_file_bytes", read_bytes)
    return count


def test_transient_read_errors_are_retried(tmp_config, monkeypatch):
    _add_candidates(tmp_config)
    vr.set_rating(tmp_config, "001", "v002", 4)
    _flaky_read(monkeypatch, tmp_config, failures=3)
    assert vr.state_status(tmp_config) == vr.STATE_OK
    _flaky_read(monkeypatch, tmp_config, failures=3)
    assert vr.get_sticker(tmp_config, "001").find("v002").human_rating == 4


def test_persistent_read_error_is_not_reported_as_corrupt(tmp_config, monkeypatch):
    _add_candidates(tmp_config)
    before = tmp_config.variants_path.read_bytes()
    _flaky_read(monkeypatch, tmp_config, failures=10_000)
    monkeypatch.setattr(vr, "IO_RETRY_DELAYS", (0.001, 0.001))
    with pytest.raises(vr.StateReadError):
        vr.state_status(tmp_config)
    with pytest.raises(vr.StateReadError):
        vr.set_rating(tmp_config, "001", "v002", 1)
    with pytest.raises(vr.StateReadError):
        vr.adopt(tmp_config, ENTRY, "v002")
    assert not issubclass(vr.StateReadError, vr.StateCorruptError)
    monkeypatch.undo()
    assert tmp_config.variants_path.read_bytes() == before     # 何も書いていない
    assert vr.state_status(tmp_config) == vr.STATE_OK


def test_transient_replace_errors_are_retried(tmp_config, monkeypatch):
    original = vr._replace_file
    count = {"n": 0}

    def replace(src, dst):
        if count["n"] < 3:
            count["n"] += 1
            raise PermissionError(13, "アクセスが拒否されました")
        return original(src, dst)
    monkeypatch.setattr(vr, "_replace_file", replace)
    vr.save(tmp_config, {"stickers": {"001": {"variants": []}}})
    assert count["n"] == 3 and vr.state_status(tmp_config) == vr.STATE_OK


def test_api_reports_busy_state_as_503_not_corrupt(web, tmp_config, monkeypatch):
    _add_candidates(tmp_config)
    _flaky_read(monkeypatch, tmp_config, failures=10_000)
    monkeypatch.setattr(vr, "IO_RETRY_DELAYS", (0.001,))
    r = web.get("/api/variants/001")
    assert r.status_code == 503 and r.get_json()["retry"] is True
    r = web.post("/api/variants/001/v002/rating", json={"rating": 3},
                 headers={"X-Sticker-Client": "1"})
    assert r.status_code == 503
    assert "壊れて" not in r.get_json()["error"]


def test_reading_while_saving_does_not_fail(tmp_config):
    """読み取りと保存を並行させても、保存も読み取りも失敗しない（Windows の共有違反を含む）。"""
    _add_candidates(tmp_config)
    stop = threading.Event()
    read_errors, write_errors = [], []

    def reader():
        while not stop.is_set():
            try:
                assert vr.state_status(tmp_config) == vr.STATE_OK
                vr.get_sticker(tmp_config, "001")
            except Exception as exc:  # noqa: BLE001
                read_errors.append(repr(exc))

    readers = [threading.Thread(target=reader) for _ in range(2)]
    [t.start() for t in readers]
    try:
        for k in range(150):
            try:
                vr.set_rating(tmp_config, "001", "v001", (k % 5) + 1)
            except Exception as exc:  # noqa: BLE001
                write_errors.append(repr(exc))
    finally:
        stop.set()
        [t.join(30) for t in readers]
    assert write_errors == [] and read_errors == []
    assert _item(tmp_config, "001", "v001")["human_rating"] == 5


def test_save_that_stays_blocked_reports_busy_and_keeps_the_file(tmp_config, monkeypatch):
    _add_candidates(tmp_config)
    before = tmp_config.variants_path.read_bytes()
    monkeypatch.setattr(vr, "IO_RETRY_DELAYS", (0.001,))
    monkeypatch.setattr(vr, "_replace_file", _raise(PermissionError(13, "使用中")))
    with pytest.raises(vr.StateWriteError):
        vr.set_rating(tmp_config, "001", "v002", 2)
    monkeypatch.undo()
    assert tmp_config.variants_path.read_bytes() == before
    assert [p for p in tmp_config.variants_path.parent.iterdir() if p.name.endswith(".tmp")] == []


# ===========================================================================
# C1: legacy v001 の実体化は「一時ファイル → 確認 → 置き換え」。半端なファイルを使わない
# ===========================================================================
def _legacy_only(cfg, color=YELLOW):
    sticker_like(body=color).save(cfg.dir_generated / "001.png", format="PNG")
    return _sha(cfg.dir_generated / "001.png")


def test_c1_copy_failure_midway_leaves_no_partial_v001(tmp_config, monkeypatch):
    original = _legacy_only(tmp_config)

    def half_copy(src, dst, *args, **kwargs):
        data = Path(src).read_bytes()
        Path(dst).write_bytes(data[: len(data) // 2])
        raise OSError("容量不足")
    monkeypatch.setattr(vr.shutil, "copyfile", half_copy)
    with pytest.raises(OSError):
        vr.set_rating(tmp_config, "001", "v001", 3)
    monkeypatch.undo()

    folder = vr.variant_dir(tmp_config, "001")
    assert not (folder / "v001.png").exists()
    assert not folder.exists() or [p.name for p in folder.iterdir()] == []    # 一時ファイルも無い
    assert not tmp_config.variants_path.exists()                             # 記録も作らない

    vr.set_rating(tmp_config, "001", "v001", 3)                              # やり直せる
    assert _sha(folder / "v001.png") == original


def test_c1_broken_leftover_v001_is_rematerialized(tmp_config):
    original = _legacy_only(tmp_config)
    folder = vr.variant_dir(tmp_config, "001")
    folder.mkdir(parents=True)
    data = (tmp_config.dir_generated / "001.png").read_bytes()
    (folder / "v001.png").write_bytes(data[: len(data) // 2])                # 以前の版の半端なコピー

    vr.set_rating(tmp_config, "001", "v001", 3)
    assert _sha(folder / "v001.png") == original
    assert vr.adopt(tmp_config, ENTRY, "v001")["validation_ok"] is True


def test_c1_existing_different_image_is_never_overwritten(tmp_config):
    """記録を失ったあとなど、置き場に別の画像の v001.png がある場合は上書きしない。"""
    original = _legacy_only(tmp_config)
    other = _generate_file(tmp_config, "001", "v001", BLUE)
    other_sha = _sha(other)

    vr.set_rating(tmp_config, "001", _legacy_id(tmp_config), 2)
    assert _sha(other) == other_sha                                          # 消さない
    st = vr.get_sticker(tmp_config, "001")
    legacy = st.find(st.adopted)
    assert legacy.source == "legacy" and legacy.variant_id != "v001"
    assert _sha(legacy.path(tmp_config)) == original


def _generate_file(cfg, sid, vid, color):
    path = vr.variant_dir(cfg, sid) / f"{vid}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    sticker_like(body=color).save(path, format="PNG")
    return path


def _legacy_id(cfg):
    return vr.get_sticker(cfg, "001").variants[0].variant_id


def test_c1_concurrent_materialization_threads(tmp_config):
    original = _legacy_only(tmp_config)
    ops = [lambda: vr.set_rating(tmp_config, "001", "v001", 4),
           lambda: vr.set_verdict(tmp_config, "001", "v001", "regen"),
           lambda: vr.adopt(tmp_config, ENTRY, "v001"),
           lambda: scoring.score_all(tmp_config, ["001"])] * 2
    errors = []
    threads = [threading.Thread(target=lambda op=op: errors.extend(_run_in_thread(op))) for op in ops]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    assert errors == []
    record = _state(tmp_config)["stickers"]["001"]
    assert [v["variant_id"] for v in record["variants"]] == ["v001"]
    assert _sha(vr.variant_dir(tmp_config, "001") / "v001.png") == original
    assert record["variants"][0]["human_rating"] == 4 and record["variants"][0]["verdict"] in ("regen", "adopted")


def test_c1_concurrent_materialization_processes(tmp_config):
    original = _legacy_only(tmp_config)
    procs = [_spawn(tmp_config, "rate:v001", 3) for _ in range(4)]
    for p in procs:
        out, err = p.communicate(timeout=240)
        assert p.returncode == 0, err
    record = _state(tmp_config)["stickers"]["001"]
    assert [v["variant_id"] for v in record["variants"]] == ["v001"]
    assert _sha(vr.variant_dir(tmp_config, "001") / "v001.png") == original


def test_c1_round_trips_always_return_to_the_original(tmp_config):
    _add_candidates(tmp_config)
    original = _sha(vr.variant_dir(tmp_config, "001") / "v001.png")
    for _ in range(3):
        for vid in ("v002", "v001", "v003", "v001"):
            vr.adopt(tmp_config, ENTRY, vid)
            if vid == "v001":
                assert _sha(tmp_config.dir_generated / "001.png") == original
    assert _sha(vr.variant_dir(tmp_config, "001") / "v001.png") == original


# ===========================================================================
# STEP 7: legacy の記録にも原画の目印を付け、差し替えに気付けるようにする
# ===========================================================================
def test_legacy_record_has_a_fingerprint_and_detects_replacement(tmp_config):
    original = _legacy_only(tmp_config)
    vr.set_rating(tmp_config, "001", "v001", 4)
    stamp = _state(tmp_config)["stickers"]["001"]["adopted_file"]
    assert stamp["sha1"] == original
    assert not vr.get_sticker(tmp_config, "001").generated_mismatch

    time.sleep(0.02)
    sticker_like(body=GREEN).save(tmp_config.dir_generated / "001.png", format="PNG")
    assert vr.get_sticker(tmp_config, "001").generated_mismatch


def test_reading_legacy_still_creates_nothing(tmp_config):
    _legacy_only(tmp_config)
    vr.get_sticker(tmp_config, "001")
    vr.list_all(tmp_config, ["001"])
    assert not tmp_config.variants_path.exists()
    assert not vr.variant_dir(tmp_config, "001").exists()


# ===========================================================================
# STEP 6: 採用と取り込みが同じ原画を奪い合わない
# ===========================================================================
def test_import_waits_for_adoption_and_is_detected_afterwards(tmp_config, monkeypatch):
    import io
    from src.text_renderer import TextStyle

    _add_candidates(tmp_config)
    vr.adopt(tmp_config, ENTRY, "v002")
    buf = io.BytesIO()
    sticker_like(body=(30, 30, 30, 255)).save(buf, format="PNG")
    started, imported, results = threading.Event(), threading.Event(), []
    original_validate = vd.validate_sticker
    importing = []

    def run_import():
        results.append(importer.import_image(
            tmp_config, ENTRY, buf.getvalue(), TextStyle.from_config(tmp_config)))
        imported.set()

    def validate_while_importing(*args, **kwargs):
        if not started.is_set():
            started.set()
            importing.append(threading.Thread(target=run_import))
            importing[0].start()
            # 鍵が無ければ取り込みはここで終わってしまう。終わらないこと（待たされていること）を確かめる
            assert not imported.wait(0.5)
        return original_validate(*args, **kwargs)
    monkeypatch.setattr(vd, "validate_sticker", validate_while_importing)
    vr.adopt(tmp_config, ENTRY, "v003")
    monkeypatch.undo()
    importing[0].join(30)

    record = _state(tmp_config)["stickers"]["001"]
    v003 = _sha(vr.variant_dir(tmp_config, "001") / "v003.png")
    assert record["adopted"] == "v003"
    assert record["adopted_file"]["sha1"] == v003          # 目印は採用した候補のもの
    assert results and results[0].ok                       # 取り込みは採用のあとに行われた
    assert _sha(tmp_config.dir_generated / "001.png") != v003
    assert vr.get_sticker(tmp_config, "001").generated_mismatch   # 取り込みで差し替わったと分かる


# ===========================================================================
# C2 周辺: 退避名の一意化・修復コマンド・画面での表示
# ===========================================================================
def _run_module(*args):
    """python -m src.variants を別プロセスで実行します（出力は UTF-8 で受け取る）。"""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run([sys.executable, "-m", "src.variants", *args], cwd=PROJECT, env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=120)


def _all_pngs(cfg) -> dict:
    return {str(p.relative_to(cfg.root)): _sha(p) for p in (cfg.root / "output").rglob("*.png")}


def test_quarantine_twice_in_the_same_second_keeps_both(tmp_config):
    tmp_config.variants_path.write_text("{A", encoding="utf-8")
    first = vr.quarantine_corrupt_state(tmp_config)
    tmp_config.variants_path.write_text("{B", encoding="utf-8")
    second = vr.quarantine_corrupt_state(tmp_config)
    assert first != second
    kept = sorted(p.read_text(encoding="utf-8")
                  for p in tmp_config.variants_path.parent.glob("variants.json.corrupt-*"))
    assert kept == ["{A", "{B"]


def test_corrupt_message_names_a_command_that_exists(tmp_config):
    assert "python -m src.variants repair" in vr.corrupt_message(tmp_config)
    out = _run_module("--help")
    assert out.returncode == 0 and "repair" in out.stdout


def test_repair_command_recovers_candidates_after_corruption(tmp_config):
    """壊れた記録を退避し、候補フォルダから記録を作り直す（採用中は generated と同じ候補）。"""
    _add_candidates(tmp_config)
    vr.adopt(tmp_config, ENTRY, "v003")
    broken = tmp_config.variants_path.read_text(encoding="utf-8")[:-40]
    tmp_config.variants_path.write_text(broken, encoding="utf-8")
    images = _all_pngs(tmp_config)

    out = _run_module("--config", str(tmp_config.path), "repair")
    assert out.returncode == 0, out.stderr

    kept = list(tmp_config.variants_path.parent.glob("variants.json.corrupt-*"))
    assert [p.read_text(encoding="utf-8") for p in kept] == [broken]      # 壊れたファイルは残す
    st = vr.get_sticker(tmp_config, "001")
    assert [v.variant_id for v in st.variants] == ["v001", "v002", "v003"]
    assert st.adopted == "v003" and not st.generated_mismatch
    assert _all_pngs(tmp_config) == images                                 # 画像は消さない・変えない
    assert vr.adopt(tmp_config, ENTRY, "v001")["validation_ok"] is True    # そのまま使える


def test_repair_on_a_healthy_state_only_adds_missing_candidates(tmp_config):
    _add_candidates(tmp_config)
    vr.set_rating(tmp_config, "001", "v002", 5)
    vr.set_verdict(tmp_config, "001", "v003", "rejected")
    before = _state(tmp_config)["stickers"]["001"]
    _generate_file(tmp_config, "001", "v007", GREEN)                       # 記録に無い画像
    (vr.variant_dir(tmp_config, "001") / "v008.png").write_bytes(b"not a png")

    report = vr.repair_state(tmp_config)
    assert report["quarantined"] is None
    assert report["stickers"]["001"]["added"] == ["v007"]
    assert any("v008.png" in p for p in report["unreadable"])
    after = _state(tmp_config)["stickers"]["001"]
    assert after["adopted"] == before["adopted"]
    assert _item(tmp_config, "001", "v002")["human_rating"] == 5
    assert _item(tmp_config, "001", "v003")["verdict"] == "rejected"
    assert [v["variant_id"] for v in after["variants"]] == ["v001", "v002", "v003", "v007"]
    assert _generate_one(tmp_config, "001") == "v009"                      # 番号は重複しない


def test_repair_when_generated_matches_no_candidate(tmp_config):
    _generate_file(tmp_config, "001", "v001", BLUE)
    original = _legacy_only(tmp_config, YELLOW)                           # 記録なし・別の原画
    tmp_config.variants_path.write_text("{壊れ", encoding="utf-8")
    vr.repair_state(tmp_config)
    st = vr.get_sticker(tmp_config, "001")
    adopted = st.find(st.adopted)
    assert adopted.source == "legacy" and _sha(adopted.path(tmp_config)) == original
    assert st.find("v001").source == "recovered"


def test_gui_shows_corruption_and_can_repair(web, tmp_config):
    _add_candidates(tmp_config)
    tmp_config.variants_path.write_text("{壊れ", encoding="utf-8")
    assert web.get("/api/state").get_json()["variants_state"] == "corrupt"
    assert web.get("/api/variants").get_json()["state_status"] == "corrupt"

    r = web.post("/api/variants/repair", json={}, headers={"X-Sticker-Client": "1"})
    assert r.status_code == 200
    assert r.get_json()["variants_state"] == "ok"
    assert web.get("/api/state").get_json()["variants_state"] == "ok"
    r = web.post("/api/variants/repair", data="x", content_type="text/plain",
                 headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


# ===========================================================================
# GET も Host を確認する（DNS rebinding で記録・画像を読ませない）
# ===========================================================================
@pytest.mark.parametrize("host, expected", [
    ("127.0.0.1:8765", 200), ("localhost:8765", 200), ("[::1]:8765", 200), ("[::1]", 200),
    ("192.168.1.20:8765", 200),                  # --host 0.0.0.0 で IP から開く場合は読める
    ("evil.example:8765", 403), ("localhost.evil.example", 403), ("127.0.0.1.nip.io:8765", 403),
])
def test_reads_check_the_host_name(web, tmp_config, host, expected):
    _add_candidates(tmp_config)
    for path in ("/api/variants", "/api/state", "/img/variants/001/v002.png"):
        assert web.get(path, headers={"Host": host}).status_code == expected, path


def test_ip_host_can_read_but_not_write(web, tmp_config):
    _add_candidates(tmp_config)
    r = web.post("/api/variants/001/v002/rating", json={"rating": 3},
                 headers={"Host": "192.168.1.20:8765"})
    assert r.status_code == 403


def test_foreign_forwarded_host_is_rejected(web, tmp_config):
    assert web.get("/api/state", headers={"X-Forwarded-Host": "evil.example"}).status_code == 403
    assert web.get("/api/state", headers={"X-Forwarded-Host": "localhost"}).status_code == 200
