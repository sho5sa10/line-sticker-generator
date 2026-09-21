"""処理ログ (output/generation.log) と進捗状態 (output/state.json)。

エラー時に「どのIDで何が起きたか」を残し、途中から再開できるようにします。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path


class RunLogger:
    """コンソールとログファイルの両方へ書き出すシンプルなロガー。"""

    def __init__(self, log_path: str | Path, echo: bool = True) -> None:
        self.path = Path(log_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.echo = echo

    def _write(self, line: str) -> None:
        stamped = f"{datetime.now().isoformat(timespec='seconds')} {line}"
        with self.path.open("a", encoding="utf-8") as f:
            f.write(stamped + "\n")
        if self.echo:
            print(line, flush=True)

    def event(self, sticker_id: str, event: str, detail: str = "") -> None:
        """`001 IMAGE GENERATED` 形式のイベントを記録します。"""
        line = f"{sticker_id} {event}"
        if detail:
            line += f" - {detail}"
        self._write(line)

    def error(self, sticker_id: str, detail: str) -> None:
        self._write(f"{sticker_id} ERROR")
        self._write(f"{sticker_id} REASON - {detail}")

    def info(self, message: str) -> None:
        self._write(message)

    def warn(self, message: str) -> None:
        self._write(f"WARNING {message}")


class StateStore:
    """IDごとの進捗を JSON で保持し、途中再開を可能にします。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.data: dict = {"stickers": {}}
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                print(
                    f"WARNING: 状態ファイルを読めないため初期化します: {self.path}",
                    file=sys.stderr,
                )
                self.data = {"stickers": {}}
        self.data.setdefault("stickers", {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def set(self, sticker_id: str, status: str, detail: str = "") -> None:
        self.data["stickers"][sticker_id] = {
            "status": status,
            "detail": detail,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        self.save()

    def status(self, sticker_id: str) -> str | None:
        entry = self.data["stickers"].get(sticker_id)
        return entry.get("status") if entry else None

    def failed_ids(self) -> list[str]:
        return sorted(
            sid for sid, v in self.data["stickers"].items() if v.get("status") == "error"
        )
