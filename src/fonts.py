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


# ---------------------------------------------------------------------------
# 追加できる無料フォント（Google Fonts / SIL Open Font License）
# ---------------------------------------------------------------------------
# OFL は、フォントを使って作った画像を販売してもかまわないライセンスです。
# ダウンロード元はこの一覧のURLに限ります（任意のURLからは取得しません）。
GOOGLE_FONTS_BASE = "https://raw.githubusercontent.com/google/fonts/main/ofl"


@dataclass(frozen=True)
class FreeFont:
    id: str
    label: str
    category: str      # 手書き / 丸ゴシック / ポップ / 毛筆 / ドット
    folder: str        # google/fonts の ofl/ 以下のフォルダ名
    filename: str
    size_mb: float
    tags: tuple[str, ...]
    note: str

    @property
    def url(self) -> str:
        return f"{GOOGLE_FONTS_BASE}/{self.folder}/{self.filename}"

    @property
    def license_url(self) -> str:
        return f"{GOOGLE_FONTS_BASE}/{self.folder}/OFL.txt"


FREE_FONTS: list[FreeFont] = [
    # --- 手書き ---
    FreeFont("hachi-maru-pop", "はちまるポップ", "手書き", "hachimarupop",
             "HachiMaruPop-Regular.ttf", 4.2, ("handwritten", "gentle"),
             "丸っこくてかわいい手書き。ゆるい・かわいいキャラに"),
    FreeFont("yusei-magic", "油性マジック", "手書き", "yuseimagic",
             "YuseiMagic-Regular.ttf", 3.0, ("handwritten", "impact"),
             "マジックペンで書いたような太い手書き。元気・コミカルなキャラに"),
    FreeFont("yomogi", "よもぎ", "手書き", "yomogi",
             "Yomogi-Regular.ttf", 3.9, ("handwritten", "gentle"),
             "やさしい手書き。細めなので縁取りを太めに"),
    FreeFont("klee-one", "クレー（太め）", "手書き", "kleeone",
             "KleeOne-SemiBold.ttf", 8.5, ("handwritten",),
             "鉛筆で書いたような、きちんとした手書き"),
    FreeFont("zen-kurenaido", "紅道", "手書き", "zenkurenaido",
             "ZenKurenaido-Regular.ttf", 4.1, ("handwritten",),
             "筆ペンで書いたような、くだけた手書き"),
    FreeFont("kiwi-maru", "キウイ丸", "手書き", "kiwimaru",
             "KiwiMaru-Medium.ttf", 4.9, ("handwritten", "gentle"),
             "やわらかい丸字。ほっこりしたキャラに"),
    FreeFont("darumadrop-one", "だるまドロップ", "手書き", "darumadropone",
             "DarumadropOne-Regular.ttf", 0.3, ("handwritten", "impact"),
             "ぽってりした手書き。ファイルが小さく、漢字が入っていない可能性があります"
             "（追加後に足りない文字をチェックします）"),
    # --- 丸ゴシック ---
    FreeFont("zen-maru-gothic", "Zen丸ゴシック（極太）", "丸ゴシック", "zenmarugothic",
             "ZenMaruGothic-Black.ttf", 3.5, ("gentle", "rounded"),
             "角の丸いゴシック。読みやすさとかわいさの両立"),
    # --- ポップ ---
    FreeFont("mochiy-pop", "モッチーポップ", "ポップ", "mochiypopone",
             "MochiyPopOne-Regular.ttf", 4.9, ("impact", "pop"),
             "もちっとしたポップ体。にぎやかなキャラに"),
    FreeFont("dela-gothic", "デラゴシック", "ポップ", "delagothicone",
             "DelaGothicOne-Regular.ttf", 2.4, ("impact",),
             "とても太いゴシック。インパクト重視"),
    FreeFont("rocknroll", "ロックンロール", "ポップ", "rocknrollone",
             "RocknRollOne-Regular.ttf", 2.6, ("impact", "pop"),
             "丸みのあるポップな太字"),
    FreeFont("potta-one", "ポッタ", "ポップ", "pottaone",
             "PottaOne-Regular.ttf", 4.7, ("impact", "handwritten"),
             "筆で書いたようなポップ体"),
    # --- 毛筆 ---
    FreeFont("yuji-boku", "佑字 朴", "毛筆", "yujiboku",
             "YujiBoku-Regular.ttf", 8.1, ("elegant", "handwritten"),
             "毛筆の字。和風のキャラに"),
    # --- ドット ---
    FreeFont("dotgothic16", "ドットゴシック16", "ドット", "dotgothic16",
             "DotGothic16-Regular.ttf", 2.0, ("pixel",),
             "レトロゲーム風のドット文字"),
]
FREE_FONT_CATEGORIES = ["手書き", "丸ゴシック", "ポップ", "毛筆", "ドット"]


def free_font(font_id: str) -> FreeFont | None:
    return next((f for f in FREE_FONTS if f.id == font_id), None)


def _free_font_by_filename(name: str) -> FreeFont | None:
    return next((f for f in FREE_FONTS if f.filename == name), None)


class FontInstallError(Exception):
    """フォントの追加に失敗した場合に送出されます。"""


def install_free_font(config, font_id: str, *, timeout: float = 60.0, http_get=None) -> Path:
    """一覧にある無料フォントを fonts/ にダウンロードします（ライセンス文も一緒に保存）。

    Args:
        http_get: テスト用の差し替え口。(url, timeout) を受け取り bytes を返す関数。
    """
    ff = free_font(font_id)
    if ff is None:
        raise FontInstallError(f"一覧にないフォントです: {font_id}")

    if http_get is None:
        import httpx

        def http_get(url: str, timeout: float) -> bytes:
            r = httpx.get(url, timeout=timeout, follow_redirects=True)
            r.raise_for_status()
            return r.content

    folder = custom_font_dir(config)
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / ff.filename
    try:
        data = http_get(ff.url, timeout)
        license_text = http_get(ff.license_url, timeout)
    except Exception as exc:  # noqa: BLE001 - 通信エラーの種類は問わず理由を返す
        raise FontInstallError(f"ダウンロードできませんでした: {exc}") from exc

    # 中身が本当にフォントか確かめてから保存します（壊れたファイルを残さない）。
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(data)
    try:
        from PIL import ImageFont

        ImageFont.truetype(str(tmp), 20).getbbox("あ")
    except Exception as exc:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        raise FontInstallError(f"フォントとして読み込めませんでした: {exc}") from exc
    tmp.replace(dest)
    (folder / f"{Path(ff.filename).stem}-OFL.txt").write_bytes(license_text)
    return dest


def missing_chars(font_path: str, texts, index: int = 0) -> list[str]:
    """セリフに使われている文字のうち、フォントに入っていない文字の一覧。

    入っていない文字は「□」などで描かれるため、事前に知らせます。
    """
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype(font_path, 40, index=index)

    def glyph(ch: str) -> bytes:
        img = Image.new("L", (64, 64), 0)
        ImageDraw.Draw(img).text((8, 8), ch, font=font, fill=255)
        return img.tobytes()

    # 存在しないことが確実な文字の描かれ方（□や空白）を「無い文字」の見本にします。
    notdef = glyph("\U0010FFFD")
    blank = bytes(64 * 64)
    missing: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for ch in text:
            if ch in seen or ch.isspace():
                continue
            seen.add(ch)
            g = glyph(ch)
            if g == notdef or g == blank:
                missing.append(ch)
    return missing


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
            if p.suffix.lower() not in FONT_EXTS:
                continue
            ff = _free_font_by_filename(p.name)
            if ff:  # 「フォントを増やす」で追加した無料フォント
                fonts.append(FontInfo(ff.id, f"{ff.label}（{ff.category}）", str(p), 0, "",
                                      ff.tags, ff.note))
            else:   # 自分で fonts/ に置いたフォント
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
