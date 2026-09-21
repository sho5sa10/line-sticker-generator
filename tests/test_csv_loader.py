"""CSV読み込みと範囲指定のテスト。"""

from __future__ import annotations

import pytest

from src.csv_loader import CsvLoadError, filter_entries, load_stickers


def test_load_dummy_csv(dummy_csv):
    entries = load_stickers(dummy_csv)
    assert len(entries) == 3
    assert entries[0].id == "001"
    assert entries[0].text == "了解！"
    assert entries[0].index == 1
    assert entries[2].category == "misc"


def test_project_csv_is_valid(real_config):
    """同梱のセリフCSVが読めること。

    件数はGUIの「セリフ編集」で変わるので固定しません（同梱時は100件）。
    """
    entries = load_stickers(real_config.csv_path)
    assert entries
    assert all(e.action for e in entries)
    assert all(e.expression for e in entries)
    assert len({e.id for e in entries}) == len(entries)  # ID重複なし


@pytest.fixture
def csv100(tmp_path):
    """範囲指定のテスト用に、001〜100 の100件のCSVを作ります。"""
    p = tmp_path / "stickers100.csv"
    rows = ["id,text,action,expression,category"]
    rows += [f"{i:03d},セリフ{i},ポーズ{i},表情{i},basic" for i in range(1, 101)]
    p.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return p


def test_load_100_entries(csv100):
    entries = load_stickers(csv100)
    assert len(entries) == 100
    assert entries[0].id == "001"
    assert entries[-1].id == "100"


def test_missing_file():
    with pytest.raises(CsvLoadError):
        load_stickers("does-not-exist.csv")


def test_missing_column(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text("id,text\n001,やあ\n", encoding="utf-8")
    with pytest.raises(CsvLoadError, match="必須カラム"):
        load_stickers(p)


def test_duplicate_id(tmp_path):
    p = tmp_path / "dup.csv"
    p.write_text(
        "id,text,action,expression,category\n"
        "001,A,a,e,basic\n"
        "001,B,a,e,basic\n",
        encoding="utf-8",
    )
    with pytest.raises(CsvLoadError, match="重複"):
        load_stickers(p)


def test_empty_text(tmp_path):
    p = tmp_path / "empty.csv"
    p.write_text("id,text,action,expression,category\n001,,a,e,basic\n", encoding="utf-8")
    with pytest.raises(CsvLoadError, match="text"):
        load_stickers(p)


def test_utf8_bom_is_accepted(tmp_path):
    p = tmp_path / "bom.csv"
    p.write_text(
        "id,text,action,expression,category\n001,了解！,敬礼,笑顔,basic\n",
        encoding="utf-8-sig",
    )
    entries = load_stickers(p)
    assert entries[0].id == "001"


# --- 範囲指定 -----------------------------------------------------------
def test_filter_by_range(csv100):
    entries = load_stickers(csv100)
    selected = filter_entries(entries, start=1, end=40)
    assert len(selected) == 40
    assert selected[0].id == "001"
    assert selected[-1].id == "040"


def test_filter_by_id(csv100):
    entries = load_stickers(csv100)
    assert [e.id for e in filter_entries(entries, ids=["001"])] == ["001"]
    # ゼロ埋めなしでも一致すること
    assert [e.id for e in filter_entries(entries, ids=["7"])] == ["007"]


def test_filter_by_limit(csv100):
    entries = load_stickers(csv100)
    assert len(filter_entries(entries, limit=5)) == 5


def test_filter_unknown_id_raises(csv100):
    entries = load_stickers(csv100)
    with pytest.raises(CsvLoadError, match="存在しないID"):
        filter_entries(entries, ids=["999"])
