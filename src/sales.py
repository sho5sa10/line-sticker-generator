"""スタンプごとの販売状況（未販売 / 申請中 / 販売中）を記録します。

LINE Creators Market の状況は自動では取得できないので、GUI で自分で付けた印を保存します。
保存先は data/sales.json（Git の管理対象外）。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

# "" は未販売（記録なし）
STATUSES = ("", "review", "selling")
STATUS_LABELS = {"": "未販売", "review": "申請中", "selling": "販売中"}


class SalesError(ValueError):
    """不正な状況が指定されたときに送出されます。"""


def sales_path(config) -> Path:
    return config.root / "data" / "sales.json"


def load_sales(config) -> dict[str, str]:
    """{スタンプID: 状況} を返します。未販売のものは含みません。"""
    path = sales_path(config)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    stickers = data.get("stickers", {}) if isinstance(data, dict) else {}
    return {
        str(sid): str(info.get("status", ""))
        for sid, info in stickers.items()
        if isinstance(info, dict) and info.get("status") in STATUSES[1:]
    }


def set_status(config, ids, status: str) -> dict[str, str]:
    """指定したスタンプの状況を変えて保存し、全体の {ID: 状況} を返します。"""
    if status not in STATUSES:
        raise SalesError(f"不明な販売状況です: {status}")
    path = sales_path(config)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        data = {}
    stickers = data.get("stickers", {}) if isinstance(data, dict) else {}
    now = datetime.now().isoformat(timespec="seconds")
    for sid in ids:
        sid = str(sid)
        if status:
            stickers[sid] = {"status": status, "updated": now}
        else:
            stickers.pop(sid, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"stickers": dict(sorted(stickers.items()))}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return load_sales(config)
