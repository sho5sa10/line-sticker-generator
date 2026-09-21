"""完成画像が LINE Creators Market の仕様を満たすか検証します。

確認日 2026-09-21 / https://creator.line.me/ja/guideline/sticker/
  - スタンプ画像: 最大 370x320 px / PNG / RGB(RGBA) / 透過 / 1MB以下 / 縦横は偶数
  - メイン画像:   240x240 px
  - タブ画像:     96x74 px
  - 余白:         画像端とコンテンツの間に 10px 程度
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from .image_processor import ALPHA_THRESHOLD

SEVERITY_ERROR = "ERROR"
SEVERITY_WARNING = "WARNING"


@dataclass
class Issue:
    """検証で見つかった問題1件。"""

    path: str
    severity: str
    code: str
    message: str

    def __str__(self) -> str:
        return f"[{self.severity}] {Path(self.path).name}: {self.message}"


@dataclass
class ValidationReport:
    """1ファイル分の検証結果。"""

    path: str
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == SEVERITY_ERROR]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == SEVERITY_WARNING]

    @property
    def ok(self) -> bool:
        return not self.errors


def _add(report: ValidationReport, severity: str, code: str, message: str) -> None:
    report.issues.append(Issue(path=report.path, severity=severity, code=code, message=message))


def content_bbox(img: Image.Image) -> tuple[int, int, int, int] | None:
    """不透明なコンテンツの外接矩形を返します。"""
    if img.mode != "RGBA":
        return (0, 0, img.width, img.height)
    alpha = img.getchannel("A")
    return alpha.point(lambda v: 255 if v > ALPHA_THRESHOLD else 0).getbbox()


def measure_margins(img: Image.Image) -> tuple[int, int, int, int] | None:
    """(左, 上, 右, 下) の余白px。コンテンツが無い場合は None。"""
    bbox = content_bbox(img)
    if not bbox:
        return None
    left, top, right, bottom = bbox
    return left, top, img.width - right, img.height - bottom


def validate_image(
    path: str | Path,
    *,
    max_width: int,
    max_height: int,
    margin: int,
    max_file_size_bytes: int,
    require_transparency: bool = True,
    require_even: bool = True,
    exact_size: bool = False,
) -> ValidationReport:
    """1枚の画像を検証します。"""
    p = Path(path)
    report = ValidationReport(path=str(p))

    if not p.exists():
        _add(report, SEVERITY_ERROR, "missing", "ファイルが存在しません")
        return report

    size_bytes = p.stat().st_size
    if size_bytes == 0:
        _add(report, SEVERITY_ERROR, "empty_file", "ファイルサイズが0です")
        return report

    try:
        with Image.open(p) as im:
            im.load()  # 破損検知のため実データまで読み込みます
            fmt = (im.format or "").upper()
            mode = im.mode
            width, height = im.size
            rgba = im.convert("RGBA")
    except Exception as exc:  # noqa: BLE001 - 破損画像は種類が多岐に渡る
        _add(report, SEVERITY_ERROR, "corrupt", f"画像が破損しています: {exc}")
        return report

    # --- 形式 ---
    if fmt != "PNG":
        _add(report, SEVERITY_ERROR, "format", f"PNG形式ではありません (実際: {fmt or '不明'})")
    if mode not in ("RGB", "RGBA", "P"):
        _add(report, SEVERITY_ERROR, "color_mode", f"カラーモードが不正です (実際: {mode})")

    # --- サイズ ---
    if exact_size:
        if (width, height) != (max_width, max_height):
            _add(
                report,
                SEVERITY_ERROR,
                "dimensions",
                f"サイズが {max_width}x{max_height} ではありません (実際: {width}x{height})",
            )
    elif width > max_width or height > max_height:
        _add(
            report,
            SEVERITY_ERROR,
            "dimensions",
            f"サイズが上限 {max_width}x{max_height} を超えています (実際: {width}x{height})",
        )

    if require_even and (width % 2 or height % 2):
        _add(
            report,
            SEVERITY_ERROR,
            "odd_dimensions",
            f"縦横は偶数である必要があります (実際: {width}x{height})",
        )

    # --- 容量 ---
    if size_bytes > max_file_size_bytes:
        _add(
            report,
            SEVERITY_ERROR,
            "file_size",
            f"容量超過: {size_bytes / 1024 / 1024:.2f}MB > "
            f"{max_file_size_bytes / 1024 / 1024:.2f}MB",
        )

    # --- 透過 ---
    alpha = rgba.getchannel("A")
    alpha_min, alpha_max = alpha.getextrema()
    if require_transparency and alpha_min > ALPHA_THRESHOLD:
        _add(report, SEVERITY_ERROR, "no_transparency", "背景が透過されていません")

    # --- 中身が空でないか ---
    if alpha_max <= ALPHA_THRESHOLD:
        _add(report, SEVERITY_ERROR, "blank", "画像が空です（全ピクセルが透明）")
        return report

    # --- 余白 / 画像端への接触 ---
    margins = measure_margins(rgba)
    if margins is None:
        _add(report, SEVERITY_ERROR, "blank", "コンテンツが検出できません")
    else:
        left, top, right, bottom = margins
        if min(margins) <= 0:
            _add(
                report,
                SEVERITY_ERROR,
                "edge_contact",
                f"コンテンツが画像端に接触しています (L{left} T{top} R{right} B{bottom})",
            )
        elif min(margins) < margin:
            _add(
                report,
                SEVERITY_WARNING,
                "insufficient_margin",
                f"WARNING: insufficient margin - "
                f"最小余白 {min(margins)}px < 推奨 {margin}px (L{left} T{top} R{right} B{bottom})",
            )

    return report


def validate_sticker(path: str | Path, config) -> ValidationReport:
    """通常スタンプ画像を設定に従って検証します。"""
    w, h = config.sticker_size
    return validate_image(
        path,
        max_width=w,
        max_height=h,
        margin=config.margin,
        max_file_size_bytes=config.max_file_size_bytes,
        require_even=bool(config.get("sticker.require_even_dimensions", True)),
    )


def validate_main(path: str | Path, config) -> ValidationReport:
    w, h = config.main_size
    return validate_image(
        path,
        max_width=w,
        max_height=h,
        margin=int(config.get("main.margin", 10)),
        max_file_size_bytes=config.max_file_size_bytes,
        exact_size=True,
    )


def validate_tab(path: str | Path, config) -> ValidationReport:
    w, h = config.tab_size
    return validate_image(
        path,
        max_width=w,
        max_height=h,
        margin=int(config.get("tab.margin", 4)),
        max_file_size_bytes=config.max_file_size_bytes,
        exact_size=True,
    )


def validate_all(config, entries=None) -> list[ValidationReport]:
    """final/main/tab をまとめて検証します。"""
    reports: list[ValidationReport] = []

    final_dir = config.dir_final
    if entries is not None:
        paths = [final_dir / f"{e.id}.png" for e in entries]
    else:
        paths = sorted(final_dir.glob("*.png"))
    for p in paths:
        reports.append(validate_sticker(p, config))

    main_png = config.dir_main / "main.png"
    if main_png.exists():
        reports.append(validate_main(main_png, config))
    tab_png = config.dir_tab / "tab.png"
    if tab_png.exists():
        reports.append(validate_tab(tab_png, config))

    return reports
