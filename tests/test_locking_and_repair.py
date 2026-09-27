"""Phase 5b 最終修正：ロック・指紋・古いロックの回復・BOM・修復の回帰テスト。

内部の関数を差し替えて通すのではなく、実際のファイル・別プロセス・同期させた競合で
「壊したら必ず失敗する」ことを確かめます。画像生成APIは呼びません。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

from src import importer
from src import pipeline
from src import validator as vd
from src import variants as vr
from src.csv_loader import StickerEntry
from src.text_renderer import TextStyle
from tests.test_scoring import sticker_like

ENTRY = StickerEntry(id="001", text="了解！", action="敬礼", expression="笑顔", category="basic")
YELLOW, BLUE, PINK, BLACK = (250, 205, 60, 255), (90, 170, 220, 255), (210, 110, 140, 255), (30, 30, 30, 255)
PROJECT = Path(__file__).resolve().parents[1]


def _sha(path) -> str:
    return hashlib.sha1(Path(path).read_bytes()).hexdigest()


def _png(color) -> bytes:
    buf = io.BytesIO()
    sticker_like(body=color).save(buf, format="PNG")
    return buf.getvalue()


def _state(cfg) -> dict:
    return json.loads(cfg.variants_path.read_text(encoding="utf-8-sig"))


def _readable(path) -> bool:
    try:
        with Image.open(path) as img:
            img.load()
        return True
    except Exception:  # noqa: BLE001
        return False


def _leftovers(cfg) -> list[str]:
    root = cfg.root / "output"
    return sorted(str(p.relative_to(root)) for p in root.rglob("*")
                  if p.name.endswith((".tmp", ".lock")) or ".tmp" in p.name)


def _generate(cfg, sid, color):
    def reg(state):
        record = vr.ensure_record(cfg, state, sid)
        vid, path = vr.allocate_variant(cfg, record, sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_png(color))
        vr.register_variant(cfg, record, vid, path, meta={})
        return vid
    return vr.update_sticker(cfg, sid, reg)


def _with_candidates(cfg):
    (cfg.dir_generated / "001.png").write_bytes(_png(YELLOW))
    _generate(cfg, "001", BLUE)
    _generate(cfg, "001", PINK)


def _env():
    return {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(PROJECT)}


_CHILD = r"""
import json, os, sys, time
from pathlib import Path
import yaml
sys.path.insert(0, sys.argv[1])
from src import importer, variants as vr
from src.config import Config
from src.csv_loader import StickerEntry
from src.text_renderer import TextStyle
cfg_path = Path(sys.argv[2])
cfg = Config(raw=yaml.safe_load(cfg_path.read_text(encoding="utf-8")), root=cfg_path.parent.parent, path=cfg_path)
mode, arg = sys.argv[3], (sys.argv[4] if len(sys.argv) > 4 else "")
entry = StickerEntry(id="001", text="了解！", action="敬礼", expression="笑顔", category="basic")
if mode == "import":
    r = importer.import_image(cfg, entry, Path(arg).read_bytes(), TextStyle.from_config(cfg))
    print("ok" if r.ok else "ng:" + r.message)
elif mode == "hold":                     # 状態の鍵を取り、合図があるまで持ち続ける
    with vr.state_lock(cfg):
        print("locked", flush=True)
        sys.stdin.readline()
    print("released")
elif mode == "die":                      # 鍵を持ったまま強制終了（Ctrl+C やクラッシュ相当）
    with vr.state_lock(cfg):
        os._exit(9)
elif mode == "append":                   # 合図のファイルを待ってから、鍵の中で記録に追記
    go = Path(arg)
    while not go.exists():
        time.sleep(0.005)
    def mutate(state):
        log = state.setdefault("defaults", {}).setdefault("log", [])
        start = time.time()
        time.sleep(0.2)                  # 鍵の中にいる時間を広げ、重なりがあれば検出できるように
        log.append([os.getpid(), start, time.time()])
    vr.update(cfg, mutate, timeout=30)
    print("ok")
"""


def _child(cfg, mode, arg="", **kwargs):
    return subprocess.Popen([sys.executable, "-c", _CHILD, str(PROJECT), str(cfg.path), mode, arg],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", env=_env(), **kwargs)


# ===========================================================================
# M-1: generated が書き込み途中のとき、v001 を作らない（採用の鍵で取り込みと直列化）
# ===========================================================================
def test_v001_is_not_copied_from_a_half_written_generated(tmp_config, monkeypatch):
    gen = tmp_config.dir_generated / "001.png"
    gen.write_bytes(_png(YELLOW))                          # 記録の無い legacy のスタンプ
    half_written, release = threading.Event(), threading.Event()
    real_save = Image.Image.save

    def slow_save(self, fp, *args, **kwargs):
        # 取り込みが generated を書く途中で止まる（前半だけ書いた状態を明示的に作る）
        if isinstance(fp, (str, Path)) and Path(fp) == gen:
            buf = io.BytesIO()
            real_save(self, buf, *args, **kwargs)
            data = buf.getvalue()
            with open(fp, "wb") as f:
                f.write(data[: len(data) // 2])
                f.flush()
                half_written.set()
                release.wait(20)
                f.write(data[len(data) // 2:])
            return None
        return real_save(self, fp, *args, **kwargs)
    monkeypatch.setattr(Image.Image, "save", slow_save)

    importing = threading.Thread(target=lambda: importer.import_image(
        tmp_config, ENTRY, _png(BLACK), TextStyle.from_config(tmp_config)))
    importing.start()
    assert half_written.wait(20)

    rating_done, errors = threading.Event(), []

    def rate():
        try:
            vr.set_rating(tmp_config, "001", "v001", 3)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            rating_done.set()
    rating = threading.Thread(target=rate)
    rating.start()
    finished_while_half_written = rating_done.wait(1.0)   # 正しければ、取り込みが終わるまで待たされる
    release.set()
    importing.join(30)
    rating.join(30)
    monkeypatch.undo()

    assert not finished_while_half_written
    assert errors == []
    v001 = vr.variant_dir(tmp_config, "001") / "v001.png"
    assert _readable(v001) and _readable(gen)
    assert _sha(v001) == _sha(gen)                          # 書き終わった画像と完全一致
    record = _state(tmp_config)["stickers"]["001"]
    assert record["adopted"] == "v001"
    assert record["adopted_file"]["sha1"] == _sha(gen)
    assert not vr.get_sticker(tmp_config, "001").generated_mismatch
    assert _leftovers(tmp_config) == []


# ===========================================================================
# M-3 (2-A): 採用の途中で別プロセスが取り込み → 採用は失敗して巻き戻し
#            取り込みは採用（巻き戻しを含む）が終わるまで待ち、巻き戻しに消されない
# ===========================================================================
class _NG:
    ok = False
    errors = [type("Issue", (), {"message": "NG"})()]
    warnings = []


def test_import_waits_for_a_failing_adoption_and_survives_its_rollback(tmp_config, monkeypatch):
    _with_candidates(tmp_config)
    vr.adopt(tmp_config, ENTRY, "v002")                    # 原画 A（= v002）を採用中
    gen, fin = tmp_config.dir_generated / "001.png", tmp_config.dir_final / "001.png"
    original_a, final_a = _sha(gen), _sha(fin)
    record_before = _state(tmp_config)["stickers"]["001"]
    imported = tmp_config.root / "incoming.png"
    imported.write_bytes(_png(BLACK))

    at_validation, proceed = threading.Event(), threading.Event()

    def validate(*args, **kwargs):          # 採用の途中（原画・完成画像を書き換えた後）で止めてから NG
        at_validation.set()
        proceed.wait(30)
        return _NG()
    monkeypatch.setattr(vd, "validate_sticker", validate)
    adopt_errors = []
    adopting = threading.Thread(target=lambda: adopt_errors.extend(
        [e for e in [_try(lambda: vr.adopt(tmp_config, ENTRY, "v003"))] if e]))
    adopting.start()
    assert at_validation.wait(20)

    proc = _child(tmp_config, "import", str(imported))     # 別プロセスの取り込み
    try:
        proc.wait(timeout=2.0)
        finished_during_adoption = True
    except subprocess.TimeoutExpired:
        finished_during_adoption = False
    proceed.set()
    adopting.join(60)
    out, err = proc.communicate(timeout=120)
    monkeypatch.undo()

    assert not finished_during_adoption                     # 採用が終わるまで待たされた
    assert [type(e).__name__ for e in adopt_errors] == ["AdoptError"]
    assert out.strip().endswith("ok"), err
    assert _sha(gen) == hashlib.sha1(imported.read_bytes()).hexdigest() or _readable(gen)
    assert _sha(gen) != original_a                          # 取り込んだ画像が巻き戻しで消されていない
    assert _sha(fin) != final_a                             # 完成画像も取り込んだ画像から作り直されている
    archived = [_sha(p) for p in importer.archive_dir(tmp_config).glob("001_*.png")]
    assert original_a in archived                           # 取り込み前の原画は退避に残る
    record_after = _state(tmp_config)["stickers"]["001"]
    assert record_after == record_before                    # 失敗した採用は記録を変えない
    st = vr.get_sticker(tmp_config, "001")
    assert st.adopted == "v002" and st.generated_mismatch  # 取り込みで差し替わったと分かる
    assert st.rollback_failed is None
    assert not list(vr.adopt_backup_root(tmp_config).glob("*"))
    assert _leftovers(tmp_config) == []


def _try(fn):
    try:
        fn()
    except BaseException as exc:  # noqa: BLE001
        return exc
    return None


# ===========================================================================
# M-3 (2-B): 指紋は「採用を始めた時点の候補」から取る
# ===========================================================================
def test_fingerprint_comes_from_the_candidate_even_if_generated_changes_midway(tmp_config, monkeypatch):
    _with_candidates(tmp_config)
    vr.adopt(tmp_config, ENTRY, "v002")
    gen = tmp_config.dir_generated / "001.png"
    other = _png(BLACK)
    real_render = pipeline.render_final

    def render(*args, **kwargs):
        # 採用の鍵を取らない書き込み（「AIで作り直す」など）が、採用の途中で原画を B にした
        gen.write_bytes(other)
        return real_render(*args, **kwargs)
    monkeypatch.setattr(pipeline, "render_final", render)
    vr.adopt(tmp_config, ENTRY, "v003")
    monkeypatch.undo()

    candidate = vr.variant_dir(tmp_config, "001") / "v003.png"
    record = _state(tmp_config)["stickers"]["001"]
    assert record["adopted"] == "v003"
    assert record["adopted_file"]["sha1"] == _sha(candidate)          # = SHA(A)
    assert record["adopted_file"]["sha1"] != hashlib.sha1(other).hexdigest()
    assert vr.get_sticker(tmp_config, "001").generated_mismatch      # B への差し替えに気付ける


# ===========================================================================
# M-2: 強制終了で残った鍵を、プロセスが生きているかで判定して回復する
# ===========================================================================
def _lock_file(cfg) -> Path:
    return Path(str(cfg.variants_path) + ".lock")


def test_live_lock_holder_keeps_others_waiting(tmp_config):
    holder = _child(tmp_config, "hold", stdin=subprocess.PIPE)
    assert holder.stdout.readline().strip() == "locked"
    try:
        started = time.monotonic()
        with pytest.raises(vr.VariantError, match="使用中"):
            vr.update(tmp_config, lambda s: None, timeout=1.0)
        assert time.monotonic() - started >= 0.9           # 生きている持ち主の鍵は外さない
        assert _lock_file(tmp_config).exists()
    finally:
        holder.stdin.write("\n")
        holder.stdin.flush()
        holder.communicate(timeout=30)
    vr.update(tmp_config, lambda s: s.setdefault("defaults", {}).update(after=1))
    assert _state(tmp_config)["defaults"]["after"] == 1


def test_lock_left_by_a_killed_process_is_recovered_at_once(tmp_config):
    proc = _child(tmp_config, "die")
    proc.communicate(timeout=60)
    assert proc.returncode == 9 and _lock_file(tmp_config).exists()   # 鍵が残っている

    started = time.monotonic()
    vr.update(tmp_config, lambda s: s.setdefault("defaults", {}).update(recovered=True), timeout=5)
    assert time.monotonic() - started < 3                   # 120 秒待たずに回復する
    assert _state(tmp_config)["defaults"]["recovered"] is True
    assert not _lock_file(tmp_config).exists()


def test_lock_of_a_reused_pid_is_treated_as_stale(tmp_config):
    """持ち主の PID が別のプロセスに再利用されていても、起動時刻が違えば古い鍵とみなす。"""
    proc = _child(tmp_config, "die")
    proc.communicate(timeout=60)
    info = json.loads(_lock_file(tmp_config).read_text(encoding="utf-8"))
    info["pid"] = os.getpid()                               # 生きている別のプロセス（自分）の PID
    _lock_file(tmp_config).write_text(json.dumps(info), encoding="utf-8")
    vr.update(tmp_config, lambda s: None, timeout=5)


def test_stale_lock_is_broken_by_only_one_of_many_processes(tmp_config):
    """古い鍵を複数のプロセスが同時に見つけても、鍵の中に入れるのは常に1つだけ。"""
    proc = _child(tmp_config, "die")
    proc.communicate(timeout=60)
    assert _lock_file(tmp_config).exists()
    go = tmp_config.root / "go"
    procs = [_child(tmp_config, "append", str(go)) for _ in range(4)]
    time.sleep(0.5)                                         # 全員が合図を待つ状態にしてから一斉に開始
    go.write_text("go")
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0 and out.strip() == "ok", err

    log = sorted(_state(tmp_config)["defaults"]["log"], key=lambda x: x[1])
    assert len(log) == 4                                    # 誰の書き込みも消えていない
    for (_, _, end), (_, start, _) in zip(log, log[1:]):
        assert start >= end                                 # 鍵の中にいた時間が重ならない
    assert not _lock_file(tmp_config).exists()


# ===========================================================================
# M-4: BOM 付き UTF-8 の JSON を正常として扱う
# ===========================================================================
def _write_with_bom(cfg):
    text = cfg.variants_path.read_text(encoding="utf-8")
    cfg.variants_path.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))


def test_bom_state_is_valid_and_keeps_everything(tmp_config):
    _with_candidates(tmp_config)
    vr.adopt(tmp_config, ENTRY, "v002")
    vr.set_rating(tmp_config, "001", "v003", 5)
    vr.set_verdict(tmp_config, "001", "v003", "rejected")
    _write_with_bom(tmp_config)

    assert vr.state_status(tmp_config) == vr.STATE_OK
    st = vr.get_sticker(tmp_config, "001")
    assert st.adopted == "v002" and st.find("v003").human_rating == 5
    report = vr.repair_state(tmp_config)                    # 修復は不要で、何も変えない
    assert report["kind"] == "ok" and not report["changed"]
    assert tmp_config.variants_path.read_bytes().startswith(b"\xef\xbb\xbf")

    vr.set_rating(tmp_config, "001", "v001", 2)             # 書き込むと BOM なしの UTF-8 になる
    raw = tmp_config.variants_path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    st = vr.get_sticker(tmp_config, "001")
    assert st.adopted == "v002" and st.find("v003").verdict == "rejected"
    assert st.find("v003").human_rating == 5 and st.find("v001").human_rating == 2


@pytest.mark.parametrize("content, expected", [
    (b'{"stickers": {}}', "ok"),
    (b'\xef\xbb\xbf{"stickers": {}}', "ok"),
    (b"", "corrupt"),
    (b"\xef\xbb\xbf", "corrupt"),
    (b'{"stickers": ', "corrupt"),
    (b'\xef\xbb\xbf{"stickers": ', "corrupt"),
    (b"\xff\xfe{\x00}\x00", "corrupt"),
])
def test_state_classification(tmp_config, content, expected):
    tmp_config.variants_path.write_bytes(content)
    assert vr.state_status(tmp_config) == expected


# ===========================================================================
# 修復：表示と実際の変更を一致させる
# ===========================================================================
def _repair_cli(cfg):
    p = subprocess.run([sys.executable, "-m", "src.variants", "--config", str(cfg.path), "repair"],
                       cwd=PROJECT, capture_output=True, text=True, encoding="utf-8", env=_env(),
                       timeout=120)
    return p.returncode, p.stdout, p.stderr


def test_repair_on_a_healthy_state_does_not_write(tmp_config):
    _with_candidates(tmp_config)
    vr.set_rating(tmp_config, "001", "v002", 4)
    before = tmp_config.variants_path.read_bytes()
    mtime = tmp_config.variants_path.stat().st_mtime_ns
    rc, out, err = _repair_cli(tmp_config)
    assert rc == 0, err
    assert "変更はありません" in out
    assert tmp_config.variants_path.read_bytes() == before
    assert tmp_config.variants_path.stat().st_mtime_ns == mtime


def test_repair_adds_only_missing_candidates_and_says_so(tmp_config):
    _with_candidates(tmp_config)
    extra = vr.variant_dir(tmp_config, "001") / "v007.png"
    extra.write_bytes(_png(BLACK))
    rc, out, err = _repair_cli(tmp_config)
    assert rc == 0, err
    assert "v007" in out and "追加" in out
    assert [v["variant_id"] for v in _state(tmp_config)["stickers"]["001"]["variants"]][-1] == "v007"


def test_repair_when_the_state_file_is_missing_and_nothing_to_recover(tmp_config):
    rc, out, err = _repair_cli(tmp_config)
    assert rc == 0, err
    assert "記録ファイルがありません" in out
    assert not tmp_config.variants_path.exists()           # 作らない


def test_repair_when_the_state_file_is_missing_rebuilds_from_candidates(tmp_config):
    _with_candidates(tmp_config)
    tmp_config.variants_path.unlink()
    rc, out, err = _repair_cli(tmp_config)
    assert rc == 0, err
    assert "記録ファイルがありません" in out and "作り直しました" in out
    assert [v.variant_id for v in vr.get_sticker(tmp_config, "001").variants] == ["v001", "v002", "v003"]


def test_repair_with_only_a_temp_file_does_not_adopt_it(tmp_config):
    _with_candidates(tmp_config)
    leftover = tmp_config.variants_path.with_name(".variants.json.abcd1234.tmp")
    tmp_config.variants_path.replace(leftover)
    kept = leftover.read_bytes()
    rc, out, err = _repair_cli(tmp_config)
    assert rc == 1
    assert ".variants.json.abcd1234.tmp" in out
    assert not tmp_config.variants_path.exists()           # 一時ファイルの中身を勝手に採用しない
    assert leftover.read_bytes() == kept


def test_repair_of_a_corrupt_state_reports_the_quarantine(tmp_config):
    _with_candidates(tmp_config)
    tmp_config.variants_path.write_text("{壊れ", encoding="utf-8")
    rc, out, err = _repair_cli(tmp_config)
    assert rc == 0, err
    assert "退避しました" in out
    assert [p.read_text(encoding="utf-8") for p in
            tmp_config.variants_path.parent.glob("variants.json.corrupt-*")] == ["{壊れ"]


# ===========================================================================
# Low: 巻き戻しの失敗を一覧でも知らせる
# ===========================================================================
def test_rollback_failure_is_reported_in_the_sticker_list(tmp_config):
    pytest.importorskip("flask")
    from src.webapp import create_app

    csv_path = tmp_config.root / "data" / "stickers.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text("id,text,action,expression,category\n001,了解！,敬礼,笑顔,basic\n",
                        encoding="utf-8")
    tmp_config.raw["data"]["csv"] = "data/stickers.csv"
    _with_candidates(tmp_config)

    def mark(state):
        state["stickers"]["001"]["rollback_failed"] = {"at": "x", "detail": "d", "backup": "b"}
    vr.update(tmp_config, mark)
    rows = create_app(tmp_config).test_client().get("/api/state").get_json()["stickers"]
    assert rows[0]["rollback_failed"] is True


# ===========================================================================
# H-4 の確実な再現: 読み取り中のファイルを置き換えられること（Windows）
# ===========================================================================
@pytest.mark.skipif(os.name != "nt", reason="Windows のファイル共有の挙動を確かめるテスト")
def test_state_can_be_saved_while_another_reader_holds_it_open(tmp_config):
    """GUI の一覧取得などが記録を開いている最中でも、保存は失敗しない（タイミングに依存しない）。"""
    import ctypes
    from ctypes import wintypes

    _with_candidates(tmp_config)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    # 他の読み取り手と同じく「読み取り・書き込み・削除の共有」を許して開いたまま保持する
    handle = kernel32.CreateFileW(str(tmp_config.variants_path), 0x80000000, 0x7, None, 3, 0x80, None)
    assert handle and handle != wintypes.HANDLE(-1).value
    try:
        vr.set_rating(tmp_config, "001", "v002", 5)
    finally:
        kernel32.CloseHandle(handle)
    assert vr.get_sticker(tmp_config, "001").find("v002").human_rating == 5
    assert _leftovers(tmp_config) == []
