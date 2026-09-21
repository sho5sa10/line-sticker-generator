"""販売状況の記録（src/sales.py）のテスト。"""

from __future__ import annotations

import pytest

from src import sales


def test_default_is_unsold(tmp_config):
    assert sales.load_sales(tmp_config) == {}


def test_set_and_clear_status(tmp_config):
    got = sales.set_status(tmp_config, ["001", "002"], "selling")
    assert got == {"001": "selling", "002": "selling"}
    got = sales.set_status(tmp_config, ["002"], "review")
    assert got == {"001": "selling", "002": "review"}
    got = sales.set_status(tmp_config, ["001"], "")
    assert got == {"002": "review"}
    assert sales.load_sales(tmp_config) == {"002": "review"}


def test_unknown_status_is_rejected(tmp_config):
    with pytest.raises(sales.SalesError):
        sales.set_status(tmp_config, ["001"], "sold-out")


def test_broken_file_is_treated_as_empty(tmp_config):
    path = sales.sales_path(tmp_config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken", encoding="utf-8")
    assert sales.load_sales(tmp_config) == {}
    assert sales.set_status(tmp_config, ["003"], "selling") == {"003": "selling"}
