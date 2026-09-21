"""main画像・tab画像の生成と、LINE提出用ZIPの作成。

LINE Creators Market の仕様（2026-09-21 確認）:
  - 1セットのスタンプ数は 8 / 16 / 24 / 32 / 40 のいずれか
  - ZIP内のファイル名は 01.png ... NN.png / main.png / tab.png
  - ZIPは 60MB 以下
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from . import image_processor as ip


class PackageError(Exception):
    """パッケージ作成に失敗した場合に送出されます。"""


@dataclass
class PackageResult:
    """ZIP 1つ分の作成結果。"""

    path: Path
    sticker_count: int
    size_bytes: int
    warnings: list[str] = field(default_factory=list)


# ----------------------------------------------------------------------
# main / tab 画像
# ----------------------------------------------------------------------
def pick_source_image(config, source_id: str = "auto") -> Path:
    """main/tab のもとになる原画を選びます。

    "auto" の場合は、生成済み画像のうち透明でない面積が最も大きいもの
    （＝キャラクターが大きくはっきり写っているもの）を選びます。
    """
    generated = sorted(config.dir_generated.glob("*.png"))
    if not generated:
        raise PackageError(
            f"原画が1枚もありません: {config.dir_generated}\n"
            "先に `python -m src.main generate` を実行してください。"
        )

    if source_id and source_id != "auto":
        target = config.dir_generated / f"{source_id}.png"
        if not target.exists():
            raise PackageError(f"指定された画像が見つかりません: {target}")
        return target

    best: tuple[int, Path] | None = None
    for p in generated:
        try:
            img = ip.load_rgba(p)
        except ip.ImageProcessingError:
            continue
        trimmed = ip.trim_transparent(ip.make_background_transparent(img))
        area = trimmed.width * trimmed.height
        if best is None or area > best[0]:
            best = (area, p)

    if best is None:
        raise PackageError("有効な原画が見つかりませんでした")
    return best[1]


def _build_fixed_size_image(config, kind: str) -> tuple[Path, int]:
    """main または tab の画像を生成します。"""
    if kind == "main":
        size = config.main_size
        margin = int(config.get("main.margin", 10))
        source_id = str(config.get("main.source_id", "auto"))
        out = config.dir_main / "main.png"
    elif kind == "tab":
        size = config.tab_size
        margin = int(config.get("tab.margin", 4))
        source_id = str(config.get("tab.source_id", "auto"))
        out = config.dir_tab / "tab.png"
    else:  # pragma: no cover - 呼び出し側のバグ
        raise PackageError(f"不明な種別: {kind}")

    src = pick_source_image(config, source_id)
    img = ip.make_background_transparent(ip.load_rgba(src))
    canvas = ip.fit_on_canvas(img, size, margin)
    path, size_bytes, _ = ip.save_png(canvas, out, config.max_file_size_bytes)
    return path, size_bytes


def build_main_image(config) -> tuple[Path, int]:
    """output/main/main.png (240x240) を生成します。"""
    return _build_fixed_size_image(config, "main")


def build_tab_image(config) -> tuple[Path, int]:
    """output/tab/tab.png (96x74) を生成します。"""
    return _build_fixed_size_image(config, "tab")


# ----------------------------------------------------------------------
# セット分割
# ----------------------------------------------------------------------
def split_into_valid_sets(count: int, valid_sizes: list[int]) -> tuple[list[int], int]:
    """枚数を LINE が許可するセットサイズへ分割します。

    Returns:
        (各セットの枚数, 余って使えない枚数)

    例: 100枚 / [8,16,24,32,40] -> ([40, 40, 16], 4)
        100 は 8 の倍数ではないため、4枚は申請に使えません。
    """
    sizes = sorted({int(s) for s in valid_sizes if int(s) > 0}, reverse=True)
    if not sizes:
        raise PackageError("valid_set_sizes が空です")

    sets: list[int] = []
    remaining = count
    while remaining > 0:
        candidate = next((s for s in sizes if s <= remaining), None)
        if candidate is None:
            break
        sets.append(candidate)
        remaining -= candidate
    return sets, remaining


# ----------------------------------------------------------------------
# ZIP
# ----------------------------------------------------------------------
def build_zip(
    config,
    sticker_paths: list[Path],
    zip_path: Path,
    *,
    main_path: Path | None = None,
    tab_path: Path | None = None,
) -> PackageResult:
    """スタンプ画像 + main + tab を1つのZIPにまとめます。

    中間ファイル（原画やログ）は含めません。
    """
    warnings: list[str] = []
    valid_sizes = [int(s) for s in config.get("package.valid_set_sizes", [8, 16, 24, 32, 40])]
    if len(sticker_paths) not in valid_sizes:
        warnings.append(
            f"WARNING: スタンプ枚数 {len(sticker_paths)} は LINE の許可枚数 "
            f"{valid_sizes} ではありません。このままでは申請できません。"
        )

    name_fmt = str(config.get("package.sticker_filename_format", "{index:02d}.png"))
    zip_path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for i, p in enumerate(sticker_paths, start=1):
            if not p.exists():
                raise PackageError(f"スタンプ画像が見つかりません: {p}")
            zf.write(p, arcname=name_fmt.format(index=i))
        if main_path and main_path.exists():
            zf.write(main_path, arcname=str(config.get("package.main_filename", "main.png")))
        else:
            warnings.append("WARNING: main.png が含まれていません")
        if tab_path and tab_path.exists():
            zf.write(tab_path, arcname=str(config.get("package.tab_filename", "tab.png")))
        else:
            warnings.append("WARNING: tab.png が含まれていません")

    size_bytes = zip_path.stat().st_size
    max_zip = float(config.get("package.max_zip_size_mb", 60)) * 1024 * 1024
    if size_bytes > max_zip:
        warnings.append(
            f"WARNING: ZIPが {size_bytes / 1024 / 1024:.1f}MB で上限 "
            f"{max_zip / 1024 / 1024:.0f}MB を超えています"
        )

    return PackageResult(zip_path, len(sticker_paths), size_bytes, warnings)


def build_packages(config, entries) -> list[PackageResult]:
    """完成画像を LINE の許可枚数ごとにZIP化します。"""
    final_dir = config.dir_final
    available = [(e, final_dir / f"{e.id}.png") for e in entries]
    ready = [(e, p) for e, p in available if p.exists()]
    missing = [e.id for e, p in available if not p.exists()]

    if not ready:
        raise PackageError(
            f"完成画像が1枚もありません: {final_dir}\n"
            "先に `python -m src.main generate` または `render` を実行してください。"
        )

    main_path = config.dir_main / "main.png"
    tab_path = config.dir_tab / "tab.png"
    if not main_path.exists():
        main_path, _ = build_main_image(config)
    if not tab_path.exists():
        tab_path, _ = build_tab_image(config)

    valid_sizes = [int(s) for s in config.get("package.valid_set_sizes", [8, 16, 24, 32, 40])]
    auto_split = bool(config.get("package.auto_split", True))

    results: list[PackageResult] = []
    if auto_split:
        sets, leftover = split_into_valid_sets(len(ready), valid_sizes)
    else:
        sets, leftover = [len(ready)], 0

    cursor = 0
    for chunk in sets:
        group = ready[cursor : cursor + chunk]
        cursor += chunk
        first, last = group[0][0].id, group[-1][0].id
        zip_path = config.dir_packages / f"line_stickers_{first}_{last}.zip"
        result = build_zip(
            config,
            [p for _, p in group],
            zip_path,
            main_path=main_path,
            tab_path=tab_path,
        )
        if missing:
            result.warnings.append(
                f"NOTE: 完成画像が無いIDをスキップしました: {', '.join(missing)}"
            )
        results.append(result)

    if leftover:
        leftover_ids = [e.id for e, _ in ready[cursor:]]
        results.append(
            PackageResult(
                path=config.dir_packages / "(未パッケージ)",
                sticker_count=leftover,
                size_bytes=0,
                warnings=[
                    f"WARNING: {leftover}枚は LINE の許可枚数 {valid_sizes} に分割できず"
                    f"ZIP化していません: {', '.join(leftover_ids)}"
                ],
            )
        )

    return results


def open_image_size(path: str | Path) -> tuple[int, int]:
    """テスト用の小さなヘルパ。"""
    with Image.open(path) as im:
        return im.size
