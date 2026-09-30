"""処理ログ (output/generation.log) と進捗状態 (output/state.json)。

エラー時に「どのIDで何が起きたか」を残し、途中から再開できるようにします。
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

# 保存の置き換えが PermissionError のときの再試行の間隔（秒）。Windows では、別の処理（GUI と CLI）が
# state.json を読んでいる瞬間に置き換えられないことがあるため、少し待ってやり直します（無限には待ちません）
_REPLACE_RETRY_DELAYS = (0.01, 0.025, 0.05, 0.1, 0.2, 0.4)


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
        """state.json を読みます。読めない・形が違う場合は、警告を出して空の状態から始めます。

        進捗の記録なので、壊れていても生成などの本体の処理は止めません
        （variants.json のように保存を拒否して退避することはしません）。
        """
        if self.path.exists():
            try:
                # BOM 付き UTF-8（メモ帳などで保存）も読みます。書き込みは BOM なしです
                data = json.loads(self.path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError, RecursionError):
                # ValueError は JSON の構文エラーのほか、UTF-8 として読めない・桁数の多すぎる整数も含みます。
                # 入れ子が深すぎる JSON は RecursionError になります
                self._warn("状態ファイルを読めないため初期化します")
                data = {}
            if not isinstance(data, dict):
                self._warn("状態ファイルの形が違うため初期化します")
                data = {}
            if not isinstance(data.get("stickers", {}), dict):
                self._warn("状態ファイルの stickers の形が違うため空にします")
                data["stickers"] = {}
            try:
                # 読めても保存できない中身（入れ子が深すぎて書き出しが RecursionError になる等）は持ち込みません。
                # そのままだと、生成（API の呼び出し）の後の set() で保存に失敗するためです。ファイルは書きません
                self._serialize(data)
            except (ValueError, RecursionError):
                self._warn("状態ファイルを保存できる形で読めないため初期化します")
                data = {}
            self.data = data
        self.data.setdefault("stickers", {})

    def _warn(self, message: str) -> None:
        print(f"WARNING: {message}: {self.path}", file=sys.stderr)

    @staticmethod
    def _serialize(data: dict) -> str:
        """保存する形（save と load の確認で同じものを使います）。"""
        return json.dumps(data, ensure_ascii=False, indent=2)

    def save(self) -> None:
        """一時ファイルに書いてから置き換えます（書き込みの途中で終わっても、壊れた state.json を残さないため）。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            payload = self._serialize(self.data)
        except RecursionError:
            # 字下げ付きの書き出しは Python の再帰で行われ、呼び出し位置が深いほど浅い入れ子で上限に達します
            # （load の確認より深い位置から保存すると、境界の入れ子だけここで失敗します）。字下げなしの書き出しは
            # C の実装で呼び出し位置に左右されず、読み込めた中身は書き出せるため、そちらで保存します（中身は同じ JSON）
            payload = json.dumps(self.data, ensure_ascii=False)
        fd, tmp_name = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp")
        tmp = Path(tmp_name)
        replaced = False
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            for delay in (*_REPLACE_RETRY_DELAYS, None):
                try:
                    os.replace(tmp, self.path)
                    break
                except PermissionError:
                    if delay is None:
                        raise
                    time.sleep(delay)
            replaced = True
        finally:
            if not replaced:
                with contextlib.suppress(OSError):   # 後始末の失敗より、元の失敗を伝えます
                    tmp.unlink(missing_ok=True)

    def set(self, sticker_id: str, status: str, detail: str = "") -> None:
        self.data["stickers"][sticker_id] = {
            "status": status,
            "detail": detail,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        self.save()

    def status(self, sticker_id: str) -> str | None:
        entry = self.data["stickers"].get(sticker_id)
        return entry.get("status") if isinstance(entry, dict) else None   # 形の違う項目は「記録なし」

    def failed_ids(self) -> list[str]:
        return sorted(
            sid for sid, v in self.data["stickers"].items()
            if isinstance(v, dict) and v.get("status") == "error"
        )
