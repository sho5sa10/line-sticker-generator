"""テキストの保存を「一時ファイルに書いてから置き換える」形にしたこと（Phase 6-C N-2）。

- 中身（バイト列）は今までの Path.write_text / open("w", newline="") と同じ（改行・文字コード・最後の改行）
- 失敗したとき（fsync・置き換え）は、元のファイルが変わらず、一時ファイルが残らず、元の例外がそのまま届く
- 置き換えの PermissionError だけを、決まった間隔で6回までやり直す（それ以外はやり直さない）
- 権限（POSIX）は、既存のファイルの権限を引き継ぎ、新しいファイルは今までと同じ 0o666 & ~umask

テストはすべて tmp_path の中だけで行い、data/・prompts/・output/ の実物には触れません。
"""

from __future__ import annotations

import csv
import io
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from src import atomic_write as aw
from src import character_profile as cprof
from src import listing as listing_mod
from src import sales as sales_mod
from src.config import Config
from src.csv_loader import REQUIRED_COLUMNS, CsvLoadError, StickerEntry, load_stickers, save_stickers
from tests.test_stability import GUI_HEADERS

DELAYS = (0.01, 0.025, 0.05, 0.1, 0.2, 0.4)
_real_replace = os.replace
_real_fsync = os.fsync


def _temps(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir() if p.name.endswith(".tmp"))


def _old_write(path: Path, text: str, newline: str | None = None) -> bytes:
    """今までの書き方（Path.write_text）で書いたバイト列。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline=newline)
    return path.read_bytes()


def _old_csv(path: Path, entries) -> bytes:
    """今までの save_stickers の書き方（open("w", newline="") と DictWriter）で書いたバイト列。"""
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(REQUIRED_COLUMNS))
        writer.writeheader()
        for e in entries:
            writer.writerow({"id": e.id, "text": e.text, "action": e.action,
                             "expression": e.expression, "category": e.category})
    return path.read_bytes()


class _Replace:
    """os.replace の代わり。対象のファイル名への置き換えだけを、決めた例外で失敗させます（ほかは本物）。"""

    def __init__(self, name: str, errors):
        self.name = name
        self.errors = list(errors)
        self.calls = 0

    def __call__(self, src, dst, *args, **kwargs):
        if Path(dst).name == self.name:
            self.calls += 1
            if self.errors:
                raise self.errors.pop(0)
        return _real_replace(src, dst, *args, **kwargs)


@pytest.fixture
def sleeps(monkeypatch):
    """再試行の待ちを記録します（実際には待ちません）。"""
    waited: list[float] = []
    monkeypatch.setattr(aw, "time", SimpleNamespace(sleep=waited.append))
    return waited


# ===========================================================================
# A. 正常系
# ===========================================================================
def test_creates_a_new_file(tmp_path):
    target = tmp_path / "new.txt"
    aw.atomic_write_text(target, "はじめて\n")
    assert target.read_text(encoding="utf-8") == "はじめて\n"
    assert _temps(tmp_path) == []


def test_replaces_an_existing_file(tmp_path):
    target = tmp_path / "x.txt"
    target.write_text("古い中身がもっと長い\n", encoding="utf-8")
    aw.atomic_write_text(target, "新しい\n")
    assert target.read_text(encoding="utf-8") == "新しい\n"
    assert _temps(tmp_path) == []


def test_accepts_str_path_and_writes_unicode_without_bom(tmp_path):
    target = tmp_path / "u.txt"
    text = "日本語・絵文字🐱・結合文字が゙・全角ＡＢＣ\n"
    aw.atomic_write_text(str(target), text)
    data = target.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf")
    assert data.decode("utf-8") == text.replace("\n", os.linesep)


@pytest.mark.parametrize("text", ["一行目\n二行目\n", "一行目\n二行目", "", "\n", "a\r\nb\rc\n"])
@pytest.mark.parametrize("newline", [None, "", "\n", "\r\n"])
def test_bytes_match_write_text(tmp_path, text, newline):
    """改行（LF・CRLF・最後の改行あり/なし）を、write_text とまったく同じバイト列で書きます。"""
    expected = _old_write(tmp_path / "old" / "f.txt", text, newline)
    target = tmp_path / "new" / "f.txt"
    target.parent.mkdir()
    aw.atomic_write_text(target, text, newline=newline)
    assert target.read_bytes() == expected


def test_lf_and_crlf_are_written_as_requested(tmp_path):
    lf, crlf = tmp_path / "lf.txt", tmp_path / "crlf.txt"
    aw.atomic_write_text(lf, "a\nb\n", newline="\n")
    aw.atomic_write_text(crlf, "a\nb\n", newline="\r\n")
    assert lf.read_bytes() == b"a\nb\n"
    assert crlf.read_bytes() == b"a\r\nb\r\n"


@pytest.mark.skipif(os.name != "nt", reason="Windows の既定の改行（CRLF）の確認")
def test_default_newline_on_windows_is_crlf_without_double_cr(tmp_path):
    """C ランタイムの変換（O_BINARY なし）と重なって \\r\\r\\n にならないこと。"""
    target = tmp_path / "w.txt"
    aw.atomic_write_text(target, "a\nb\n")
    assert target.read_bytes() == b"a\r\nb\r\n"


def test_temp_file_is_created_next_to_the_target(tmp_path, monkeypatch):
    seen: list[list[str]] = []

    def fsync(fd):
        seen.append(_temps(tmp_path))
        return _real_fsync(fd)

    monkeypatch.setattr(aw.os, "fsync", fsync)
    target = tmp_path / "data.json"
    aw.atomic_write_text(target, "{}")
    assert len(seen) == 1 and len(seen[0]) == 1
    assert seen[0][0].startswith(".data.json.")                     # 同じフォルダの、隠しの一時ファイル
    assert _temps(tmp_path) == []


def test_temp_name_collision_uses_another_name(tmp_path, monkeypatch):
    """一時ファイルの名前がぶつかったら、別の名前で作り直します（既にあるファイルは壊しません）。"""
    names = iter(["aaaa", "bbbb"])
    monkeypatch.setattr(aw, "uuid", SimpleNamespace(uuid4=lambda: SimpleNamespace(hex=next(names))))
    taken = tmp_path / ".t.txt.aaaa.tmp"
    taken.write_bytes(b"someone else")
    target = tmp_path / "t.txt"
    aw.atomic_write_text(target, "ok")
    assert target.read_text(encoding="utf-8") == "ok"
    assert taken.read_bytes() == b"someone else"
    assert not (tmp_path / ".t.txt.bbbb.tmp").exists()


def test_missing_folder_raises_file_not_found_like_write_text(tmp_path):
    target = tmp_path / "no_such_dir" / "f.txt"
    with pytest.raises(FileNotFoundError):
        aw.atomic_write_text(target, "x")
    assert not target.parent.exists()                                 # フォルダは作りません


def test_writes_through_a_symlink_like_write_text(tmp_path):
    real = tmp_path / "real.txt"
    real.write_text("old", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("シンボリックリンクを作れない環境です")
    aw.atomic_write_text(link, "new")
    assert link.is_symlink()                                          # リンクそのものは置き換えない
    assert real.read_text(encoding="utf-8") == "new"


# ===========================================================================
# B. 今までの書き方とバイト単位で同じ（6系統）
# ===========================================================================
PROFILE = {"kind": "動物", "animal": "ねこ", "colors": ["白", "茶色"]}
CSV_ENTRIES = [
    StickerEntry(id="001", text="了解！", action="敬礼する", expression="笑顔", category="basic"),
    StickerEntry(id="002", text="カンマ, と \"引用符\" を含む", action="手を振る",
                 expression="照れ笑い", category="thanks"),
    StickerEntry(id="003", text="セルの中で\n改行する", action="首をかしげる",
                 expression="真顔", category="misc"),
    StickerEntry(id="004", text="CRLF\r\nも入る", action="", expression="", category="misc"),
]


@pytest.mark.parametrize("name, text", [
    ("character_master_ja.txt", "20代の女性。\n茶色のボブヘア。" + "\n"),
    ("character_profile.json",
     json.dumps(cprof.normalize(PROFILE), ensure_ascii=False, indent=2) + "\n"),
    ("overrides.yaml",
     "# このファイルは GUI から自動生成されます。\n"
     "# sticker_config.yaml の値をここで上書きします。手動編集も可能です。\n"
     + yaml.safe_dump({"font": {"size": 48, "path": "フォント/日本語.ttf"}},
                      allow_unicode=True, sort_keys=False)),
    ("sales.json", json.dumps({"stickers": {"001": {"status": "review", "updated": "2026-10-01T00:00:00"}}},
                              ensure_ascii=False, indent=2)),
    ("listing.json", json.dumps({"title_ja": "ねこの毎日", "desc_ja": "説明\n二行目"},
                                ensure_ascii=False, indent=2)),
])
def test_same_bytes_as_write_text(tmp_path, name, text):
    expected = _old_write(tmp_path / "old" / name, text)
    target = tmp_path / "new" / name
    target.parent.mkdir()
    aw.atomic_write_text(target, text)
    assert target.read_bytes() == expected


def test_csv_text_written_with_empty_newline_matches_open_w(tmp_path):
    expected = _old_csv(tmp_path / "old.csv", CSV_ENTRIES)
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=list(REQUIRED_COLUMNS))
    writer.writeheader()
    for e in CSV_ENTRIES:
        writer.writerow({k: getattr(e, k) for k in REQUIRED_COLUMNS})
    target = tmp_path / "new.csv"
    aw.atomic_write_text(target, buf.getvalue(), newline="")
    assert target.read_bytes() == expected


# --- 呼び出し元の実物で確かめます（書き込みの手段だけを変え、中身は変えていないこと） ----------
def test_save_stickers_bytes_unchanged(tmp_path):
    expected = _old_csv(tmp_path / "old.csv", CSV_ENTRIES)
    target = tmp_path / "data" / "stickers.csv"
    assert save_stickers(target, CSV_ENTRIES) == target              # 戻り値も同じ
    data = target.read_bytes()
    assert data == expected
    assert b"\r\r\n" not in data and not data.startswith(b"\xef\xbb\xbf")
    assert load_stickers(target) == CSV_ENTRIES                     # 読み戻しても同じ
    assert _temps(target.parent) == []


def test_save_stickers_keeps_bak_and_validation_order(tmp_path):
    target = tmp_path / "stickers.csv"
    save_stickers(target, CSV_ENTRIES[:1], backup=False)
    before = target.read_bytes()
    save_stickers(target, CSV_ENTRIES)
    assert (tmp_path / "stickers.csv.bak").read_bytes() == before   # 上書きの前の中身を .bak へ

    # 検証で失敗しても、.bak は検証の前に作られ（今までどおり）、CSV は変わらない
    current = target.read_bytes()
    dup = [CSV_ENTRIES[0], CSV_ENTRIES[0]]
    with pytest.raises(CsvLoadError):
        save_stickers(target, dup)
    assert target.read_bytes() == current
    assert (tmp_path / "stickers.csv.bak").read_bytes() == current
    assert _temps(tmp_path) == []


def test_save_profile_bytes_unchanged(tmp_path):
    expected = _old_write(tmp_path / "old.json",
                          json.dumps(cprof.normalize(PROFILE), ensure_ascii=False, indent=2) + "\n")
    target = tmp_path / "prompts" / "character_profile.json"
    assert cprof.save_profile(target, PROFILE) == target
    assert target.read_bytes() == expected
    assert cprof.load_profile(target) == cprof.normalize(PROFILE)


def test_save_overrides_bytes_unchanged(tmp_path):
    cfg_path = tmp_path / "config" / "sticker_config.yaml"
    cfg = Config(raw={"font": {"size": 40}}, root=tmp_path, path=cfg_path)
    path = cfg.save_overrides({"font.size": 48, "font.path": "fonts/日本語.ttf"})
    assert path == cfg_path.parent / "overrides.yaml"
    assert cfg.raw["font"] == {"size": 48, "path": "fonts/日本語.ttf"}   # 実行中の設定にも反映
    expected = _old_write(
        tmp_path / "old.yaml",
        "# このファイルは GUI から自動生成されます。\n"
        "# sticker_config.yaml の値をここで上書きします。手動編集も可能です。\n"
        + yaml.safe_dump({"font": {"size": 48, "path": "fonts/日本語.ttf"}},
                         allow_unicode=True, sort_keys=False))
    assert path.read_bytes() == expected

    cfg.save_overrides({"font.gap": 4})                               # 読んで足す（今までどおり）
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["font"] == {
        "size": 48, "path": "fonts/日本語.ttf", "gap": 4}


def test_sales_bytes_unchanged(tmp_path):
    config = SimpleNamespace(root=tmp_path)
    result = sales_mod.set_status(config, ["002", "001"], "review")
    assert result == sales_mod.load_sales(config)                    # 戻り値も今までどおり
    path = sales_mod.sales_path(config)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert list(saved["stickers"]) == ["001", "002"]
    expected = _old_write(tmp_path / "old.json",
                          json.dumps(saved, ensure_ascii=False, indent=2))   # 最後の改行なし
    assert path.read_bytes() == expected
    with pytest.raises(sales_mod.SalesError):
        sales_mod.set_status(config, ["001"], "bogus")
    assert path.read_bytes() == expected


def test_listing_bytes_unchanged(tmp_path):
    config = SimpleNamespace(root=tmp_path)
    clean = listing_mod.save_listing(config, {"title_ja": "  ねこの毎日 ", "desc_ja": "説明"})
    assert clean["title_ja"] == "ねこの毎日"
    expected = _old_write(tmp_path / "old.json", json.dumps(clean, ensure_ascii=False, indent=2))
    assert listing_mod.listing_path(config).read_bytes() == expected
    assert listing_mod.load_listing(config) == clean


# ===========================================================================
# C. fsync の失敗
# ===========================================================================
@pytest.mark.parametrize("error", [OSError(5, "I/O error"), KeyboardInterrupt()])
def test_fsync_failure_keeps_the_original(tmp_path, monkeypatch, error):
    target = tmp_path / "keep.json"
    target.write_bytes(b'{"old": true}')

    def fsync(fd):
        raise error

    monkeypatch.setattr(aw.os, "fsync", fsync)
    with pytest.raises(type(error)) as excinfo:
        aw.atomic_write_text(target, '{"new": "書きかけ"}')
    assert excinfo.value is error                                     # 元の例外そのもの
    assert target.read_bytes() == b'{"old": true}'                    # 書きかけは対象にならない
    assert _temps(tmp_path) == []                                     # 一時ファイルは消す


def test_fsync_failure_on_a_new_file_leaves_nothing(tmp_path, monkeypatch):
    def fsync(fd):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(aw.os, "fsync", fsync)
    with pytest.raises(OSError):
        aw.atomic_write_text(tmp_path / "new.txt", "x")
    assert list(tmp_path.iterdir()) == []


def test_original_is_intact_while_writing_and_fsync_comes_before_replace(tmp_path, monkeypatch):
    """書いている途中（fsync の時点）でも元のファイルはそのまま。fsync してから置き換えます。"""
    target = tmp_path / "keep.json"
    target.write_bytes(b'{"old": true}')
    events: list[tuple[str, bytes]] = []

    def fsync(fd):
        events.append(("fsync", target.read_bytes()))
        return _real_fsync(fd)

    def replace(src, dst, *args, **kwargs):
        events.append(("replace", target.read_bytes()))
        return _real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(aw.os, "fsync", fsync)
    monkeypatch.setattr(aw.os, "replace", replace)
    aw.atomic_write_text(target, '{"new": true}')
    assert events == [("fsync", b'{"old": true}'), ("replace", b'{"old": true}')]
    assert target.read_bytes() == b'{"new": true}'


def test_encoding_error_is_not_swallowed_and_keeps_the_original(tmp_path):
    """OSError 以外（文字コードの変換）も、そのまま届きます。"""
    target = tmp_path / "a.txt"
    target.write_bytes(b"original")
    with pytest.raises(UnicodeEncodeError):
        aw.atomic_write_text(target, "日本語", encoding="ascii")
    assert target.read_bytes() == b"original"
    assert _temps(tmp_path) == []


# ===========================================================================
# D. os.replace の失敗
# ===========================================================================
def test_replace_failure_keeps_the_original(tmp_path, monkeypatch, sleeps):
    target = tmp_path / "keep.txt"
    target.write_bytes(b"original")
    error = OSError(5, "I/O error")
    fake = _Replace("keep.txt", [error])
    monkeypatch.setattr(aw.os, "replace", fake)
    with pytest.raises(OSError) as excinfo:
        aw.atomic_write_text(target, "changed")
    assert excinfo.value is error
    assert target.read_bytes() == b"original"
    assert _temps(tmp_path) == []


def test_keyboard_interrupt_during_replace_propagates(tmp_path, monkeypatch, sleeps):
    target = tmp_path / "keep.txt"
    target.write_bytes(b"original")
    interrupt = KeyboardInterrupt()
    fake = _Replace("keep.txt", [interrupt])
    monkeypatch.setattr(aw.os, "replace", fake)
    with pytest.raises(KeyboardInterrupt) as excinfo:
        aw.atomic_write_text(target, "changed")
    assert excinfo.value is interrupt
    assert fake.calls == 1 and sleeps == []                           # やり直さない
    assert target.read_bytes() == b"original"
    assert _temps(tmp_path) == []


# ===========================================================================
# E. PermissionError のときだけ、決まった間隔でやり直す
# ===========================================================================
def test_permission_error_is_retried_then_succeeds(tmp_path, monkeypatch, sleeps):
    target = tmp_path / "busy.txt"
    target.write_bytes(b"original")
    fake = _Replace("busy.txt", [PermissionError(13, "locked")] * 3)
    monkeypatch.setattr(aw.os, "replace", fake)
    aw.atomic_write_text(target, "saved")
    assert fake.calls == 4
    assert sleeps == list(DELAYS[:3])
    assert target.read_text(encoding="utf-8") == "saved"
    assert _temps(tmp_path) == []


def test_permission_error_on_every_attempt_raises_the_last_one(tmp_path, monkeypatch, sleeps):
    target = tmp_path / "busy.txt"
    target.write_bytes(b"original")
    errors = [PermissionError(13, f"locked {i}") for i in range(7)]
    fake = _Replace("busy.txt", errors)
    monkeypatch.setattr(aw.os, "replace", fake)
    with pytest.raises(PermissionError) as excinfo:
        aw.atomic_write_text(target, "saved")
    assert excinfo.value is errors[-1]                                # 最後の PermissionError そのもの
    assert fake.calls == 7                                            # 1回 + 再試行6回
    assert sleeps == list(DELAYS)
    assert target.read_bytes() == b"original"
    assert _temps(tmp_path) == []


def test_retry_delays_match_state_store():
    from src import logger
    assert aw._REPLACE_RETRY_DELAYS == DELAYS == logger._REPLACE_RETRY_DELAYS


# ===========================================================================
# F. PermissionError 以外はやり直さない
# ===========================================================================
@pytest.mark.parametrize("error", [OSError(5, "I/O error"), FileNotFoundError(2, "gone"),
                                   IsADirectoryError(21, "dir")])
def test_other_os_errors_are_not_retried(tmp_path, monkeypatch, sleeps, error):
    target = tmp_path / "x.txt"
    target.write_bytes(b"original")
    fake = _Replace("x.txt", [error])
    monkeypatch.setattr(aw.os, "replace", fake)
    with pytest.raises(type(error)) as excinfo:
        aw.atomic_write_text(target, "changed")
    assert excinfo.value is error
    assert fake.calls == 1 and sleeps == []
    assert target.read_bytes() == b"original"
    assert _temps(tmp_path) == []


# ===========================================================================
# G. 権限
# ===========================================================================
@pytest.mark.skipif(os.name == "nt", reason="POSIX の権限の確認（Windows の chmod は読み取り専用の属性だけ）")
def test_existing_file_mode_is_kept_on_posix(tmp_path):
    target = tmp_path / "m.txt"
    target.write_text("old", encoding="utf-8")
    os.chmod(target, 0o640)
    aw.atomic_write_text(target, "new")
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o640


@pytest.mark.skipif(os.name == "nt", reason="POSIX の権限の確認（Windows の chmod は読み取り専用の属性だけ）")
def test_new_file_mode_follows_umask_like_write_text(tmp_path):
    umask = os.umask(0o022)
    os.umask(umask)
    aw.atomic_write_text(tmp_path / "a.txt", "x")
    (tmp_path / "b.txt").write_text("x", encoding="utf-8")
    mode = stat.S_IMODE(os.stat(tmp_path / "a.txt").st_mode)
    assert mode == 0o666 & ~umask == stat.S_IMODE(os.stat(tmp_path / "b.txt").st_mode)


def test_new_file_mode_matches_write_text(tmp_path):
    """どの OS でも、新しいファイルの権限（Windows では読み取り専用かどうか）は write_text と同じ。"""
    aw.atomic_write_text(tmp_path / "a.txt", "x")
    (tmp_path / "b.txt").write_text("x", encoding="utf-8")
    assert os.stat(tmp_path / "a.txt").st_mode == os.stat(tmp_path / "b.txt").st_mode


@pytest.mark.skipif(os.name != "nt", reason="Windows では属性を付け直さないことの確認")
def test_windows_does_not_chmod(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(aw.os, "chmod", lambda *a, **k: calls.append(a))
    target = tmp_path / "w.txt"
    target.write_text("old", encoding="utf-8")
    aw.atomic_write_text(target, "new")
    assert calls == [] and target.read_text(encoding="utf-8") == "new"


# ===========================================================================
# 呼び出し元: 保存に失敗しても元のファイルが残り、例外の分類・HTTP の応答は今までどおり
# ===========================================================================
@pytest.fixture
def web500(tmp_config):
    """例外を HTTP 500 として返す（本番と同じ）テスト用クライアント。"""
    from src.webapp import create_app

    csv_path = tmp_config.root / "data" / "stickers.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text("id,text,action,expression,category\n001,了解！,敬礼,笑顔,basic\n",
                        encoding="utf-8")
    tmp_config.raw["data"]["csv"] = "data/stickers.csv"
    app = create_app(tmp_config)
    app.config.update(TESTING=False, PROPAGATE_EXCEPTIONS=False)
    return app.test_client()


@pytest.mark.parametrize("method, url, body, rel", [
    ("post", "/api/master/prompt", {"prompt_ja": "新しい説明"}, "prompts/character_master_ja.txt"),
    ("post", "/api/master/prompt", {"prompt_ja": "説明", "profile": PROFILE},
     "prompts/character_profile.json"),
    ("post", "/api/stickers", {"stickers": [{"id": "001", "text": "新しい"}]}, "data/stickers.csv"),
    ("patch", "/api/stickers/001", {"text": "新しい"}, "data/stickers.csv"),
    ("post", "/api/settings/font", {"size": 50}, "config/overrides.yaml"),
    ("post", "/api/sales", {"ids": ["001"], "status": "review"}, "data/sales.json"),
    ("put", "/api/listing", {"listing": {"title_ja": "新しい"}}, "data/listing.json"),
])
def test_api_save_failure_is_500_and_keeps_the_file(web500, tmp_config, monkeypatch, sleeps,
                                                    method, url, body, rel):
    target = tmp_config.root / rel
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"font:\n  size: 40\n" if target.suffix == ".yaml" else b"{}")
    before = target.read_bytes()
    fake = _Replace(target.name, [OSError(5, "I/O error")])
    monkeypatch.setattr(aw.os, "replace", fake)
    res = getattr(web500, method)(url, json=body, headers=GUI_HEADERS)
    assert res.status_code == 500                                     # 今までどおり（OSError は 500）
    assert fake.calls == 1
    assert target.read_bytes() == before
    assert _temps(target.parent) == []


def test_api_csv_validation_error_is_still_400(web500, tmp_config):
    target = tmp_config.root / "data" / "stickers.csv"
    before = target.read_bytes()
    res = web500.post("/api/stickers", json={"stickers": [{"id": "x1", "text": "a"}]},
                      headers=GUI_HEADERS)
    assert res.status_code == 400
    assert target.read_bytes() == before
