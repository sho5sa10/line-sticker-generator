"""セリフに使える日本語フォントの一覧。

GUI のフォント選択と「おまかせ」提案で使います。
フォントのファイルパスは画面から直接受け取らず、ここに載っている ID で指定させます
（任意のファイルを読ませないため）。

プロジェクト直下の fonts/ フォルダに .ttf / .otf / .ttc を置くと、それも一覧に出ます。
（丸ゴシックなど、好きなフリーフォントを追加できます）
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

FONT_EXTS = (".ttf", ".otf", ".ttc")


@dataclass(frozen=True)
class FontInfo:
    id: str
    label: str
    path: str
    index: int = 0
    variation: str = ""
    # 雰囲気のタグ: standard / gentle / impact / cool / elegant / custom
    tags: tuple[str, ...] = field(default_factory=tuple)
    note: str = ""

    def to_dict(self) -> dict:
        return {"id": self.id, "label": self.label, "note": self.note, "tags": list(self.tags)}


# Windows に標準で入っている日本語フォント。存在するものだけが一覧に出ます。
_CATALOG = [
    ("biz-ud-gothic", "BIZ UDゴシック（太字）", "BIZ-UDGothicB.ttc", 0, "",
     ("standard", "cool"), "小さくても読みやすい、スタンプの定番"),
    ("ud-kyokasho", "UDデジタル教科書体（太字）", "UDDigiKyokashoN-B.ttc", 0, "",
     ("gentle",), "手書きのようなやさしい形。かわいい・ゆるいキャラに"),
    ("noto-sans-black", "Noto Sans JP（極太）", "NotoSansJP-VF.ttf", 0, "Black",
     ("impact",), "いちばん太くて目立つ。元気・コミカルなキャラに"),
    ("noto-sans-bold", "Noto Sans JP（太字）", "NotoSansJP-VF.ttf", 0, "Bold",
     ("standard",), "すっきりした太字"),
    ("meiryo", "メイリオ（太字）", "meiryob.ttc", 0, "",
     ("standard",), "やわらかめのゴシック"),
    ("yu-gothic", "游ゴシック（太字）", "YuGothB.ttc", 0, "",
     ("cool",), "細身ですっきり。クールなキャラに"),
    ("noto-serif-black", "Noto Serif JP（極太・明朝）", "NotoSerifJP-VF.ttf", 0, "Black",
     ("elegant",), "太い明朝体。上品・和風のキャラに"),
    ("yu-mincho", "游明朝（太め）", "yumindb.ttf", 0, "",
     ("elegant",), "上品な明朝体（細めなので縁取りを太めに）"),
    ("biz-ud-mincho", "BIZ UD明朝", "BIZ-UDMinchoM.ttc", 0, "",
     ("elegant",), "読みやすい明朝体"),
]


def _system_font_dirs() -> list[Path]:
    dirs = [Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    dirs += [Path("/Library/Fonts"), Path.home() / "Library" / "Fonts",
             Path("/usr/share/fonts"), Path.home() / ".fonts"]
    return dirs


def _find_file(filename: str) -> Path | None:
    for d in _system_font_dirs():
        p = d / filename
        if p.exists():
            return p
    return None


def custom_font_dir(config) -> Path:
    return config.root / "fonts"


def available_fonts(config) -> list[FontInfo]:
    """このPCで使えるフォントの一覧（標準フォント + fonts/ に置いたフォント）。"""
    fonts: list[FontInfo] = []
    for fid, label, filename, index, variation, tags, note in _CATALOG:
        path = _find_file(filename)
        if path:
            fonts.append(FontInfo(fid, label, str(path), index, variation, tags, note))

    folder = custom_font_dir(config)
    if folder.is_dir():
        for p in sorted(folder.iterdir()):
            if p.suffix.lower() in FONT_EXTS:
                fonts.append(FontInfo(f"custom:{p.name}", f"{p.stem}（追加したフォント）",
                                      str(p), 0, "", ("custom",), "fonts/ フォルダに追加したフォント"))
    return fonts


def find_font(config, font_id: str) -> FontInfo | None:
    return next((f for f in available_fonts(config) if f.id == font_id), None)


def current_font_id(config) -> str | None:
    """いまの設定（font.path / index / variation）に当てはまるフォントのID。"""
    from .text_renderer import FontNotFoundError, resolve_font_path

    try:
        path = Path(resolve_font_path(config)).resolve()
    except FontNotFoundError:
        return None
    index = int(config.get("font.index", 0))
    variation = str(config.get("font.variation", "") or "")
    for f in available_fonts(config):
        if Path(f.path).resolve() == path and f.index == index and f.variation == variation:
            return f.id
    return None
