"""進捗の記録（output/state.json・StateStore）の異常系（Phase 6-A）。

state.json は進捗の記録（ログ）なので、壊れていても生成・取り込みなどの本体の処理を止めません。
読めない・形が違う場合は警告を出して空の状態から始め、保存は一時ファイルに書いてから置き換えます
（書き込みの途中で終わっても、壊れた state.json を残さないため）。
"""

from __future__ import annotations

import io
import json
import os

import pytest

from src import logger as lg
from src import variants as vr
from src.logger import StateStore
from tests.conftest import make_character
from tests.test_candidate_numbering import FakeProvider, _entry, _generator
from tests.test_stability import GUI_HEADERS, web  # noqa: F401 - web はフィクスチャ

GOOD = {"stickers": {"001": {"status": "error", "detail": "生成に失敗しました", "updated_at": "x"}}}

# 読めるが保存できない深さの入れ子（json.loads は通り、保存の json.dumps(indent=2) が RecursionError になる）。
# 深さの上限は環境で変わるため、テストはこの前提を確かめてから使います（_loadable_but_unsavable）
LOADABLE_DEPTH = 1500
DEEP_BUT_LOADABLE = {
    "other": '{"stickers": {}, "other": ' + "[" * LOADABLE_DEPTH + "]" * LOADABLE_DEPTH + "}",
    "sticker-entry": '{"stickers": {"001": ' + "[" * LOADABLE_DEPTH + "]" * LOADABLE_DEPTH + "}}",
}


def _write(path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _leftovers(path) -> list[str]:
    """保存の一時ファイル（.state.json.*.tmp）の残り。"""
    return sorted(p.name for p in path.parent.iterdir()
                  if p.is_file() and p.name.startswith(f".{path.name}.") and p.name.endswith(".tmp"))


# ===========================================================================
# load: 読めない state.json は、警告を出して空の状態から始める（例外にしない）
# ===========================================================================
UNREADABLE = {
    "empty": b"",
    "syntax-error": b"{",
    "deep-nesting": ("[" * 100000 + "]" * 100000).encode(),
    "4301-digits": ('{"stickers": {}, "n": ' + "9" * 4301 + "}").encode(),
    "invalid-utf8": b'{"stickers": {}, "x": "\xff"}',
    "shift-jis": json.dumps(GOOD, ensure_ascii=False).encode("cp932"),
    "cut-inside-a-multibyte-char": json.dumps(GOOD, ensure_ascii=False).encode("utf-8")[:60],
    "root-list": b"[]",
    "root-int": b"123",
    "root-str": b'"abc"',
    "root-true": b"true",
    "root-null": b"null",
}


@pytest.mark.parametrize("name", list(UNREADABLE))
def test_unreadable_state_starts_empty_with_a_warning(tmp_config, capsys, name):
    _write(tmp_config.state_path, UNREADABLE[name])
    store = StateStore(tmp_config.state_path)
    assert store.data == {"stickers": {}}
    assert "WARNING" in capsys.readouterr().err
    assert store.status("001") is None and store.failed_ids() == []
    store.set("002", "generated")                                      # そのまま使える
    assert json.loads(tmp_config.state_path.read_text(encoding="utf-8"))["stickers"]["002"]["status"] == "generated"


def test_cut_inside_a_multibyte_char_is_really_invalid_utf8():
    """上の「多バイト文字の途中で切れた書き込み」が、実際に UTF-8 として読めないこと（テストの前提）。"""
    with pytest.raises(UnicodeDecodeError):
        UNREADABLE["cut-inside-a-multibyte-char"].decode("utf-8")


def test_missing_state_starts_empty_without_a_warning(tmp_config, capsys):
    store = StateStore(tmp_config.state_path)
    assert store.data == {"stickers": {}} and capsys.readouterr().err == ""


def test_normal_state_is_read(tmp_config, capsys):
    _write(tmp_config.state_path, json.dumps(GOOD, ensure_ascii=False).encode("utf-8"))
    store = StateStore(tmp_config.state_path)
    assert store.status("001") == "error" and store.failed_ids() == ["001"]
    assert capsys.readouterr().err == ""


def test_utf8_with_bom_is_read(tmp_config, capsys):
    """BOM 付き UTF-8（メモ帳などで保存）も読み、記録を捨てない。書き直すときは BOM なし。"""
    _write(tmp_config.state_path, b"\xef\xbb\xbf" + json.dumps(GOOD, ensure_ascii=False).encode("utf-8"))
    store = StateStore(tmp_config.state_path)
    assert store.status("001") == "error" and capsys.readouterr().err == ""
    store.set("002", "generated")
    raw = tmp_config.state_path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert set(json.loads(raw.decode("utf-8"))["stickers"]) == {"001", "002"}


# ===========================================================================
# load: 読めても保存できない中身は持ち込まない
# ===========================================================================
def _loadable_but_unsavable(text: str) -> bool:
    """この環境で、json.loads は通り、保存の形（indent=2）での書き出しが失敗するか（テストの前提）。"""
    try:
        data = json.loads(text)
    except RecursionError:
        return False
    try:
        json.dumps(data, ensure_ascii=False, indent=2)
    except RecursionError:
        return True
    return False


@pytest.mark.parametrize("where", list(DEEP_BUT_LOADABLE))
def test_loadable_but_unsavable_state_starts_empty(tmp_config, capsys, where):
    text = DEEP_BUT_LOADABLE[where]
    assert _loadable_but_unsavable(text)                              # 前提（深さ 1500 はこの境界の中）
    _write(tmp_config.state_path, text.encode("utf-8"))
    store = StateStore(tmp_config.state_path)
    assert store.data == {"stickers": {}}
    assert "WARNING" in capsys.readouterr().err
    store.set("002", "generated")                                     # 保存で RecursionError にならない
    assert StateStore(tmp_config.state_path).status("002") == "generated"


# ===========================================================================
# save: 読み込んだ位置より深い呼び出しから保存しても、読めた中身は保存できる
# ===========================================================================
# 字下げ付きの書き出しは Python の再帰なので、呼び出し位置が深いほど浅い入れ子で上限に達します。
# 境界の深さは環境で変わるため、テストの中で実測します（下の数はテストの条件で、本番の値ではありません）
EXTRA_FRAMES = 50        # 保存を、読み込みより何段深い呼び出しから行うか
UPLOAD_SCAN = 100        # 取り込み API で試す、境界から下の深さの幅


def _deeper(frames: int, fn):
    return fn() if frames == 0 else _deeper(frames - 1, fn)


def _nested_text(key: str, depth: int) -> str:
    nested = "[" * depth + "]" * depth
    if key == "other":
        return '{"stickers": {}, "other": ' + nested + "}"
    return '{"stickers": {"001": ' + nested + "}}"


def _kept(store: StateStore, key: str) -> bool:
    return "other" in store.data if key == "other" else "001" in store.data["stickers"]


def _deepest_kept(path, key: str) -> int:
    """load がそのまま保持する（保存できると確かめた）いちばん深い入れ子。この環境・この呼び出し位置で実測します。"""
    lo, hi = 1, 4000
    while lo < hi:
        mid = (lo + hi + 1) // 2
        _write(path, _nested_text(key, mid).encode("utf-8"))
        lo, hi = (mid, hi) if _kept(StateStore(path), key) else (lo, mid - 1)
    return lo


@pytest.mark.parametrize("key", ["other", "sticker-entry"])
def test_deep_state_is_saved_from_a_deeper_call(tmp_config, key):
    depth = _deepest_kept(tmp_config.state_path, key)
    text = _nested_text(key, depth)
    parsed = json.loads(text)
    # 前提: この深さは、深い位置からだと字下げ付きでは書き出せず、字下げなしなら書き出せる
    with pytest.raises(RecursionError):
        _deeper(EXTRA_FRAMES, lambda: json.dumps(parsed, ensure_ascii=False, indent=2))
    _deeper(EXTRA_FRAMES, lambda: json.dumps(parsed, ensure_ascii=False))

    _write(tmp_config.state_path, text.encode("utf-8"))
    store = StateStore(tmp_config.state_path)
    assert _kept(store, key)
    _deeper(EXTRA_FRAMES, lambda: store.set("002", "generated"))      # RecursionError にならない
    saved = json.loads(tmp_config.state_path.read_text(encoding="utf-8"))
    assert saved["stickers"]["002"]["status"] == "generated"         # 追加した記録
    if key == "other":                                                # 深い中身も、ほかの記録も失わない
        assert saved["other"] == parsed["other"]
    else:
        assert saved["stickers"]["001"] == parsed["stickers"]["001"]
    assert _leftovers(tmp_config.state_path) == []


def test_normal_state_is_saved_with_indentation(tmp_config):
    """ふだんは従来どおり字下げ付き（indent=2）で保存する（字下げなしは、書き出せないときだけ）。"""
    store = StateStore(tmp_config.state_path)
    store.set("001", "generated", "生成しました")
    assert tmp_config.state_path.read_text(encoding="utf-8") == json.dumps(store.data, ensure_ascii=False, indent=2)


# ===========================================================================
# load: stickers と項目の形
# ===========================================================================
def test_state_without_stickers_gets_an_empty_dict(tmp_config, capsys):
    _write(tmp_config.state_path, b'{"other": 1}')
    store = StateStore(tmp_config.state_path)
    assert store.data == {"other": 1, "stickers": {}} and capsys.readouterr().err == ""


@pytest.mark.parametrize("stickers", [[], None, "abc", 5, True], ids=["list", "null", "str", "int", "bool"])
def test_stickers_of_a_wrong_type_become_empty(tmp_config, capsys, stickers):
    _write(tmp_config.state_path, json.dumps({"stickers": stickers, "other": 1}).encode())
    store = StateStore(tmp_config.state_path)
    assert store.data == {"stickers": {}, "other": 1}          # ほかの項目は残す
    assert "WARNING" in capsys.readouterr().err
    assert store.status("001") is None and store.failed_ids() == []
    store.set("001", "generated")
    assert store.status("001") == "generated"


def test_entries_of_a_wrong_type_are_treated_as_no_record(tmp_config):
    _write(tmp_config.state_path, b'{"stickers": {"001": "x", "002": null, "003": {"status": "error"}}}')
    store = StateStore(tmp_config.state_path)
    assert store.status("001") is None and store.status("002") is None
    assert store.failed_ids() == ["003"]
    store.set("001", "generated")                                  # 上書きできる
    assert store.status("001") == "generated"


# ===========================================================================
# save: 一時ファイルに書いてから置き換える
# ===========================================================================
def test_save_writes_readable_json_and_leaves_no_temp_file(tmp_config):
    store = StateStore(tmp_config.state_path)
    store.set("001", "generated", "生成しました")
    assert json.loads(tmp_config.state_path.read_text(encoding="utf-8"))["stickers"]["001"]["detail"] == "生成しました"
    assert _leftovers(tmp_config.state_path) == []


def test_save_replaces_the_old_file(tmp_config):
    _write(tmp_config.state_path, json.dumps(GOOD, ensure_ascii=False).encode("utf-8"))
    store = StateStore(tmp_config.state_path)
    store.set("001", "generated")
    assert StateStore(tmp_config.state_path).status("001") == "generated"


def test_save_never_writes_the_state_file_directly(tmp_config, monkeypatch):
    """書き込みの途中（fsync の時点）でも、state.json は前の中身のまま（途中の中身は一時ファイルにだけある）。"""
    old = json.dumps(GOOD, ensure_ascii=False).encode("utf-8")
    _write(tmp_config.state_path, old)
    store = StateStore(tmp_config.state_path)
    seen = []
    real_fsync = os.fsync

    def checking_fsync(fd):
        seen.append(tmp_config.state_path.read_bytes())
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", checking_fsync)
    store.set("001", "generated")
    assert seen == [old]
    assert StateStore(tmp_config.state_path).status("001") == "generated"


def test_failed_replace_keeps_the_old_file_and_removes_the_temp_file(tmp_config, monkeypatch):
    old = json.dumps(GOOD, ensure_ascii=False).encode("utf-8")
    _write(tmp_config.state_path, old)
    store = StateStore(tmp_config.state_path)

    def failing_replace(src, dst):
        raise OSError("disk error")

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(OSError):                                  # 保存の失敗は、これまでどおり呼び出し元へ
        store.set("001", "generated")
    assert tmp_config.state_path.read_bytes() == old
    assert _leftovers(tmp_config.state_path) == []


def test_replace_is_retried_while_another_process_reads_the_file(tmp_config, monkeypatch):
    """Windows で別の処理が読んでいる瞬間の PermissionError は、少し待ってやり直す。"""
    store = StateStore(tmp_config.state_path)
    real_replace = os.replace
    calls = []

    def busy_then_ok(src, dst):
        calls.append(1)
        if len(calls) <= 2:
            raise PermissionError("in use")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", busy_then_ok)
    store.set("001", "generated")
    assert len(calls) == 3 and StateStore(tmp_config.state_path).status("001") == "generated"
    assert _leftovers(tmp_config.state_path) == []


def test_replace_retries_are_exhausted_then_the_error_is_raised(tmp_config, monkeypatch):
    """PermissionError が続けば、決まった回数だけやり直したあと、最後の例外を呼び出し元へ返す
    （前の state.json は残り、一時ファイルも残さない）。"""
    old = json.dumps(GOOD, ensure_ascii=False).encode("utf-8")
    _write(tmp_config.state_path, old)
    store = StateStore(tmp_config.state_path)
    calls, sleeps = [], []

    def always_busy(src, dst):
        calls.append(1)
        raise PermissionError(f"in use #{len(calls)}")

    monkeypatch.setattr(os, "replace", always_busy)
    monkeypatch.setattr(lg.time, "sleep", sleeps.append)              # 待たずに回数だけ数えます
    with pytest.raises(PermissionError) as excinfo:
        store.set("001", "generated")
    attempts = len(lg._REPLACE_RETRY_DELAYS) + 1                      # 最初の1回 + やり直し
    assert len(calls) == attempts and str(excinfo.value) == f"in use #{attempts}"   # 最後の例外
    assert sleeps == list(lg._REPLACE_RETRY_DELAYS)
    assert tmp_config.state_path.read_bytes() == old
    assert _leftovers(tmp_config.state_path) == []


# ===========================================================================
# 本体の処理を止めない（生成・候補の生成・取り込み）
# ===========================================================================
@pytest.mark.parametrize("data", [UNREADABLE["cut-inside-a-multibyte-char"], b'{"stickers": []}',
                                  b'{"stickers": null}', b'{"stickers": "abc"}', b'{"stickers": 123}',
                                  b'{"stickers": true}', DEEP_BUT_LOADABLE["other"].encode("utf-8")],
                         ids=["unreadable", "stickers-list", "stickers-null", "stickers-str", "stickers-int",
                              "stickers-bool", "deep-but-loadable"])
def test_generation_continues_with_a_broken_state(tmp_config, data):
    """壊れた state.json でも生成でき（API は通常どおり 1 回）、生成後の記録（state.set）でも落ちない。"""
    _write(tmp_config.state_path, data)
    provider = FakeProvider()
    result = _generator(tmp_config, provider).generate_one(_entry())
    assert provider.calls == 1 and result.status == "generated"
    assert (tmp_config.dir_generated / "001.png").exists()
    assert StateStore(tmp_config.state_path).status("001") == "generated"


def test_variant_generation_continues_and_does_not_touch_a_broken_state(tmp_config):
    data = UNREADABLE["deep-nesting"]
    _write(tmp_config.state_path, data)
    provider = FakeProvider()
    results = vr.generate_variants(tmp_config, [_entry()], 1, _generator(tmp_config, provider))
    assert provider.calls == 1 and [r["status"] for r in results] == ["generated"]
    assert tmp_config.state_path.read_bytes() == data              # 候補の生成は state.json に触れない


@pytest.mark.parametrize("data", [b"[]", DEEP_BUT_LOADABLE["other"].encode("utf-8")],
                         ids=["root-list", "deep-but-loadable"])
def test_upload_works_with_a_broken_state(web, tmp_config, data):  # noqa: F811 - web はフィクスチャ
    """以前は StateStore の例外が _import_context（読み込み）や取り込みの記録（保存）から漏れて HTTP 500 になっていた。"""
    _write(tmp_config.state_path, data)
    buf = io.BytesIO()
    make_character((512, 512)).save(buf, format="PNG")
    r = web.post("/api/stickers/001/upload", headers=GUI_HEADERS,
                 data={"file": (io.BytesIO(buf.getvalue()), "001.png")},
                 content_type="multipart/form-data")
    assert r.status_code == 200
    assert StateStore(tmp_config.state_path).status("001") is not None


def test_upload_near_the_deepest_loadable_state(web, tmp_config):  # noqa: F811 - web はフィクスチャ
    """取り込みでは、読み込み（_import_context）より深い位置で保存（import_image → set）します。
    保持できる境界の付近の深さをすべて試し、どれでも HTTP 500 にならず、保存した state.json が読めること。"""
    depth = _deepest_kept(tmp_config.state_path, "other")
    buf = io.BytesIO()
    make_character((256, 256)).save(buf, format="PNG")
    for n in range(depth - UPLOAD_SCAN, depth + 2):
        _write(tmp_config.state_path, _nested_text("other", n).encode("utf-8"))
        r = web.post("/api/stickers/001/upload", headers=GUI_HEADERS,
                     data={"file": (io.BytesIO(buf.getvalue()), "001.png")},
                     content_type="multipart/form-data")
        assert r.status_code == 200, n
        assert StateStore(tmp_config.state_path).status("001") is not None, n
