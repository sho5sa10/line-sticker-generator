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


def test_load_real_csv_has_100_entries(real_config):
    entries = load_stickers(real_config.csv_path)
    assert len(entries) == 100
    assert entries[0].id == "001"
    assert entries[-1].id == "100"
    # 全件に action / expression が設定されていること
    assert all(e.action for e in entries)
    assert all(e.expression for e in entries)
    # ID重複なし
    assert len({e.id for e in entries}) == 100


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
def test_filter_by_range(real_config):
    entries = load_stickers(real_config.csv_path)
    selected = filter_entries(entries, start=1, end=40)
    assert len(selected) == 40
    assert selected[0].id == "001"
    assert selected[-1].id == "040"


def test_filter_by_id(real_config):
    entries = load_stickers(real_config.csv_path)
    assert [e.id for e in filter_entries(entries, ids=["001"])] == ["001"]
    # ゼロ埋めなしでも一致すること
    assert [e.id for e in filter_entries(entries, ids=["7"])] == ["007"]


def test_filter_by_limit(real_config):
    entries = load_stickers(real_config.csv_path)
    assert len(filter_entries(entries, limit=5)) == 5


def test_filter_unknown_id_raises(real_config):
    entries = load_stickers(real_config.csv_path)
    with pytest.raises(CsvLoadError, match="存在しないID"):
        filter_entries(entries, ids=["999"])
