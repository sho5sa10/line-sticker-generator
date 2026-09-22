"""ZIP生成・main/tab画像・セット分割のテスト。APIは呼びません。"""

from __future__ import annotations

import zipfile

import pytest

from src import image_processor as ip
from src import package_builder as pkg
from src.csv_loader import StickerEntry
from tests.conftest import make_character


def _entries(n: int) -> list[StickerEntry]:
    return [
        StickerEntry(id=f"{i:03d}", text=f"text{i}", action="a", expression="e", category="basic")
        for i in range(1, n + 1)
    ]


def _populate(config, n: int) -> list[StickerEntry]:
    """final/ と generated/ にダミー画像を用意します。"""
    entries = _entries(n)
    for e in entries:
        ip.save_png(make_character((512, 512)), config.dir_generated / f"{e.id}.png")
        sticker = ip.compose_sticker(
            make_character(), None, config.sticker_size, config.margin
        ).image
        ip.save_png(sticker, config.dir_final / f"{e.id}.png", config.max_file_size_bytes)
    return entries


# --- セット分割 ---------------------------------------------------------
def test_split_100_into_valid_sets():
    sets, leftover = pkg.split_into_valid_sets(100, [8, 16, 24, 32, 40])
    assert sets == [40, 40, 16]
    assert leftover == 4  # 100は8の倍数ではないため4枚が余る
    assert sum(sets) + leftover == 100


@pytest.mark.parametrize(
    "count,expected_sets,expected_leftover",
    [
        (40, [40], 0),
        (48, [40, 8], 0),
        (8, [8], 0),
        (5, [], 5),
        (96, [40, 40, 16], 0),
    ],
)
def test_split_variants(count, expected_sets, expected_leftover):
    sets, leftover = pkg.split_into_valid_sets(count, [8, 16, 24, 32, 40])
    assert sets == expected_sets
    assert leftover == expected_leftover


# --- main / tab ---------------------------------------------------------
def test_build_main_and_tab(tmp_config):
    _populate(tmp_config, 3)

    main_path, main_size = pkg.build_main_image(tmp_config)
    assert main_path.exists()
    assert pkg.open_image_size(main_path) == tmp_config.main_size
    assert main_size <= tmp_config.max_file_size_bytes

    tab_path, tab_size = pkg.build_tab_image(tmp_config)
    assert tab_path.exists()
    assert pkg.open_image_size(tab_path) == tmp_config.tab_size
    assert tab_size <= tmp_config.max_file_size_bytes


def test_pick_source_image_explicit(tmp_config):
    _populate(tmp_config, 3)
    assert pkg.pick_source_image(tmp_config, "002").name == "002.png"


def test_pick_source_image_missing_raises(tmp_config):
    _populate(tmp_config, 1)
    with pytest.raises(pkg.PackageError):
        pkg.pick_source_image(tmp_config, "099")


def test_build_main_without_sources_raises(tmp_config):
    with pytest.raises(pkg.PackageError, match="原画"):
        pkg.build_main_image(tmp_config)


# --- ZIP ----------------------------------------------------------------
def test_build_zip_contains_expected_names(tmp_config):
    entries = _populate(tmp_config, 8)
    main_path, _ = pkg.build_main_image(tmp_config)
    tab_path, _ = pkg.build_tab_image(tmp_config)

    zip_path = tmp_config.dir_packages / "test.zip"
    result = pkg.build_zip(
        tmp_config,
        [tmp_config.dir_final / f"{e.id}.png" for e in entries],
        zip_path,
        main_path=main_path,
        tab_path=tab_path,
    )

    assert result.path.exists()
    assert result.sticker_count == 8
    assert result.warnings == []  # 8枚は正規のセットサイズ

    with zipfile.ZipFile(zip_path) as zf:
        names = sorted(zf.namelist())
    assert names == sorted(
        [f"{i:02d}.png" for i in range(1, 9)] + ["main.png", "tab.png"]
    )


def test_build_zip_excludes_intermediate_files(tmp_config):
    entries = _populate(tmp_config, 8)
    (tmp_config.root / "output" / "generation.log").write_text("log", encoding="utf-8")

    zip_path = tmp_config.dir_packages / "clean.zip"
    pkg.build_zip(
        tmp_config,
        [tmp_config.dir_final / f"{e.id}.png" for e in entries],
        zip_path,
        main_path=pkg.build_main_image(tmp_config)[0],
        tab_path=pkg.build_tab_image(tmp_config)[0],
    )
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    assert not any(n.endswith(".log") for n in names)
    assert not any("generated" in n for n in names)


def test_build_zip_warns_on_invalid_count(tmp_config):
    entries = _populate(tmp_config, 3)
    result = pkg.build_zip(
        tmp_config,
        [tmp_config.dir_final / f"{e.id}.png" for e in entries],
        tmp_config.dir_packages / "odd.zip",
        main_path=pkg.build_main_image(tmp_config)[0],
        tab_path=pkg.build_tab_image(tmp_config)[0],
    )
    assert any("許可枚数" in w for w in result.warnings)


def test_build_packages_splits_and_names_files(tmp_config):
    entries = _populate(tmp_config, 48)
    results = pkg.build_packages(tmp_config, entries)

    zipped = [r for r in results if r.size_bytes]
    assert [r.sticker_count for r in zipped] == [40, 8]
    assert zipped[0].path.name == "line_stickers_001_040.zip"
    assert zipped[1].path.name == "line_stickers_041_048.zip"
    for r in zipped:
        assert r.path.exists()


def test_too_few_for_any_set_has_readable_message(tmp_config):
    entries = _populate(tmp_config, 3)
    plan = pkg.plan_packages(tmp_config, entries)
    assert plan.error and "None" not in plan.error
    assert "最低8枚" in plan.error


def test_build_packages_reports_leftover(tmp_config):
    entries = _populate(tmp_config, 10)
    results = pkg.build_packages(tmp_config, entries)
    leftover = [r for r in results if not r.size_bytes]
    assert leftover, "8の倍数でない余りは未パッケージとして報告されるはず"
    assert "2枚" in leftover[0].warnings[0]


def test_summarize_shortens_long_id_lists():
    assert pkg._summarize(["001", "002"]) == "001, 002"
    long_text = pkg._summarize([f"{i:03d}" for i in range(1, 51)])
    assert "ほか42件" in long_text
    assert "計50件" in long_text
    assert len(long_text) < 120


def test_build_packages_without_final_raises(tmp_config):
    with pytest.raises(pkg.PackageError):
        pkg.build_packages(tmp_config, _entries(3))


# --- セットの作り方を選ぶ -------------------------------------------------
@pytest.mark.parametrize(
    "count,set_size,expected_sets,expected_leftover",
    [
        (100, None, [40, 40, 16], 4),   # おまかせ
        (100, 40, [40, 40], 20),
        (100, 32, [32, 32, 32], 4),
        (100, 8, [8] * 12, 4),
        (30, 40, [], 30),                # 足りない
    ],
)
def test_plan_sets(count, set_size, expected_sets, expected_leftover):
    sets, leftover = pkg.plan_sets(count, [8, 16, 24, 32, 40], set_size)
    assert sets == expected_sets
    assert leftover == expected_leftover


def test_plan_sets_rejects_invalid_size():
    with pytest.raises(pkg.PackageError):
        pkg.plan_sets(100, [8, 16, 24, 32, 40], 30)


@pytest.mark.parametrize(
    "count,expected",
    [
        (40, None),
        (8, None),
        (0, "選ばれていません"),
        (37, "あと3枚選ぶと40枚セット、5枚外すと32枚セット"),
        (5, "あと3枚選ぶと8枚セット"),
        (45, "5枚外すと40枚セット"),
    ],
)
def test_selection_hint(count, expected):
    hint = pkg.selection_hint(count, [8, 16, 24, 32, 40])
    if expected is None:
        assert hint is None
    else:
        assert expected in hint


def test_build_packages_with_fixed_size(tmp_config):
    entries = _populate(tmp_config, 20)
    results = pkg.build_packages(tmp_config, entries, set_size=8)
    zipped = [r for r in results if r.size_bytes]
    assert [r.sticker_count for r in zipped] == [8, 8]
    assert [r.path.name for r in zipped] == ["line_stickers_001_008.zip", "line_stickers_009_016.zip"]
    leftover = [r for r in results if not r.size_bytes]
    assert "4枚" in leftover[0].warnings[0]


def test_build_packages_from_selection(tmp_config):
    entries = _populate(tmp_config, 12)
    picked = ["002", "003", "005", "007", "008", "010", "011", "012"]
    results = pkg.build_packages(tmp_config, entries, ids=picked)
    assert len(results) == 1
    r = results[0]
    assert r.sticker_count == 8
    assert r.path.name == "line_stickers_selected_8.zip"
    with zipfile.ZipFile(r.path) as zf:
        assert sorted(n for n in zf.namelist() if n[0].isdigit()) == [f"{i:02d}.png" for i in range(1, 9)]


def test_build_packages_rejects_invalid_selection(tmp_config):
    entries = _populate(tmp_config, 12)
    with pytest.raises(pkg.PackageError, match="あと1枚選ぶと8枚セット"):
        pkg.build_packages(tmp_config, entries, ids=["001", "002", "003", "004", "005", "006", "007"])


def test_old_zips_are_archived_not_deleted(tmp_config):
    entries = _populate(tmp_config, 16)
    pkg.build_packages(tmp_config, entries, set_size=16)
    first = tmp_config.dir_packages / "line_stickers_001_016.zip"
    assert first.exists()

    # 分け方を変えて作り直すと、前回のZIPは archive へ移る（消えない）
    pkg.build_packages(tmp_config, entries, set_size=8)
    current = sorted(p.name for p in tmp_config.dir_packages.glob("*.zip"))
    assert current == ["line_stickers_001_008.zip", "line_stickers_009_016.zip"]
    archived = list((tmp_config.root / "output" / "archive" / "packages").rglob("*.zip"))
    assert [p.name for p in archived] == ["line_stickers_001_016.zip"]


def test_plan_packages_does_not_write_files(tmp_config):
    entries = _populate(tmp_config, 10)
    plan = pkg.plan_packages(tmp_config, entries, set_size=8)
    assert plan.to_dict()["sets"] == [{"count": 8, "first": "001", "last": "008"}]
    assert plan.to_dict()["leftover"] == ["009", "010"]
    assert not list(tmp_config.dir_packages.glob("*.zip"))
