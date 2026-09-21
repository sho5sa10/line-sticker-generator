"""セリフCSVの読み込みとバリデーション。"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

REQUIRED_COLUMNS = ("id", "text", "action", "expression", "category")


class CsvLoadError(Exception):
    """CSVが読めない/形式が不正な場合に送出されます。"""


@dataclass(frozen=True)
class StickerEntry:
    """スタンプ1件分のデータ。"""

    id: str
    text: str
    action: str
    expression: str
    category: str

    @property
    def index(self) -> int:
        """ID を整数として返します（"001" -> 1）。"""
        return int(self.id)


def load_stickers(path: str | Path) -> list[StickerEntry]:
    """CSVを読み込んで StickerEntry のリストを返します。

    - UTF-8 / UTF-8(BOM) の両方を受け付けます。
    - 必須カラムの欠落、ID重複、空のIDやtextはエラーにします。
    """
    p = Path(path)
    if not p.exists():
        raise CsvLoadError(f"CSVが見つかりません: {p}")

    with p.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
        missing = [c for c in REQUIRED_COLUMNS if c not in header]
        if missing:
            raise CsvLoadError(
                f"CSVに必須カラムがありません: {', '.join(missing)} (実際: {', '.join(header)})"
            )

        entries: list[StickerEntry] = []
        seen: set[str] = set()
        for lineno, row in enumerate(reader, start=2):
            sid = (row.get("id") or "").strip()
            text = (row.get("text") or "").strip()
            if not sid and not text:
                continue  # 空行は無視
            if not sid:
                raise CsvLoadError(f"{p}:{lineno} id が空です")
            if not text:
                raise CsvLoadError(f"{p}:{lineno} text が空です (id={sid})")
            if not sid.isdigit():
                raise CsvLoadError(f"{p}:{lineno} id は数字にしてください (id={sid})")
            if sid in seen:
                raise CsvLoadError(f"{p}:{lineno} id が重複しています: {sid}")
            seen.add(sid)

            entries.append(
                StickerEntry(
                    id=sid,
                    text=text,
                    action=(row.get("action") or "").strip(),
                    expression=(row.get("expression") or "").strip(),
                    category=(row.get("category") or "").strip() or "misc",
                )
            )

    if not entries:
        raise CsvLoadError(f"CSVにデータ行がありません: {p}")
    return entries


def filter_entries(
    entries: Sequence[StickerEntry],
    *,
    ids: Iterable[str] | None = None,
    start: int | None = None,
    end: int | None = None,
    limit: int | None = None,
) -> list[StickerEntry]:
    """ID指定・範囲指定・件数上限で絞り込みます。

    Args:
        ids: "001" や "1" のようなID（ゼロ埋め有無どちらでも可）。
        start / end: ID を整数とみなした範囲（両端を含む）。
        limit: 先頭から何件まで処理するか。
    """
    result = list(entries)

    if ids:
        wanted = {str(int(i)) for i in ids if str(i).strip()}
        result = [e for e in result if str(e.index) in wanted]
        unknown = wanted - {str(e.index) for e in result}
        if unknown:
            raise CsvLoadError(f"CSVに存在しないIDが指定されました: {', '.join(sorted(unknown))}")

    if start is not None:
        result = [e for e in result if e.index >= start]
    if end is not None:
        result = [e for e in result if e.index <= end]

    if limit is not None and limit >= 0:
        result = result[:limit]
    return result
