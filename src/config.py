"""設定ファイル(YAML)と .env の読み込み。

APIキーは必ず .env / 環境変数から読み込みます。コードにも YAML にも書きません。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "sticker_config.yaml"


class ConfigError(Exception):
    """設定が不正な場合に送出されます。"""


OVERRIDES_FILENAME = "overrides.yaml"


def _deep_get(data: dict, dotted: str, default: Any = None) -> Any:
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def _deep_merge(base: dict, patch: dict) -> dict:
    """patch を base へ再帰的に重ねた新しい辞書を返します。"""
    merged = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def set_dotted(data: dict, dotted: str, value: Any) -> None:
    """"font.size" のようなキーへ値を書き込みます（中間の辞書は自動作成）。"""
    node = data
    parts = dotted.split(".")
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = value


@dataclass
class Config:
    """YAML設定 + 環境変数をまとめて扱うオブジェクト。"""

    raw: dict = field(default_factory=dict)
    root: Path = PROJECT_ROOT
    path: Path = DEFAULT_CONFIG_PATH

    # ---------- 取得ヘルパ ----------
    def get(self, dotted: str, default: Any = None) -> Any:
        return _deep_get(self.raw, dotted, default)

    def resolve(self, dotted: str, default: str | None = None) -> Path:
        """設定値をプロジェクトルート基準の絶対パスに解決します。"""
        value = self.get(dotted, default)
        if value in (None, ""):
            raise ConfigError(f"設定 '{dotted}' が空です")
        p = Path(str(value))
        return p if p.is_absolute() else (self.root / p)

    # ---------- よく使う値 ----------
    @property
    def sticker_size(self) -> tuple[int, int]:
        return int(self.get("sticker.width", 370)), int(self.get("sticker.height", 320))

    @property
    def margin(self) -> int:
        return int(self.get("sticker.margin", 10))

    @property
    def max_file_size_bytes(self) -> int:
        return int(float(self.get("sticker.max_file_size_mb", 1)) * 1024 * 1024)

    @property
    def main_size(self) -> tuple[int, int]:
        return int(self.get("main.width", 240)), int(self.get("main.height", 240))

    @property
    def tab_size(self) -> tuple[int, int]:
        return int(self.get("tab.width", 96)), int(self.get("tab.height", 74))

    # ---------- 環境変数（秘密情報） ----------
    @property
    def provider_name(self) -> str:
        return os.getenv("IMAGE_PROVIDER") or str(self.get("generation.provider", "openai"))

    @property
    def model(self) -> str:
        return os.getenv("IMAGE_MODEL") or str(self.get("generation.model", "gpt-image-1"))

    @property
    def quality(self) -> str:
        return os.getenv("IMAGE_QUALITY") or str(self.get("generation.quality", "medium"))

    @property
    def api_key(self) -> str | None:
        """APIキー。値そのものは絶対にログ出力しないでください。"""
        return os.getenv("OPENAI_API_KEY") or None

    # ---------- 出力ディレクトリ ----------
    @property
    def dir_generated(self) -> Path:
        return self.resolve("output.generated", "output/generated")

    @property
    def dir_final(self) -> Path:
        return self.resolve("output.final", "output/final")

    @property
    def dir_main(self) -> Path:
        return self.resolve("output.main", "output/main")

    @property
    def dir_tab(self) -> Path:
        return self.resolve("output.tab", "output/tab")

    @property
    def dir_packages(self) -> Path:
        return self.resolve("output.packages", "output/packages")

    @property
    def log_path(self) -> Path:
        return self.resolve("output.log", "output/generation.log")

    @property
    def state_path(self) -> Path:
        return self.resolve("output.state", "output/state.json")

    @property
    def gallery_path(self) -> Path:
        return self.resolve("output.gallery", "output/gallery.html")

    @property
    def csv_path(self) -> Path:
        return self.resolve("data.csv", "data/stickers.csv")

    @property
    def master_image_path(self) -> Path:
        return self.resolve("character.master_image", "data/character/character_master.png")

    @property
    def master_prompt_path(self) -> Path:
        return self.resolve("character.master_prompt", "prompts/character_master.txt")

    @property
    def master_prompt_ja_path(self) -> Path:
        """日本語で書くキャラクター説明。あれば英語版より優先されます。"""
        return self.resolve("character.master_prompt_ja", "prompts/character_master_ja.txt")

    @property
    def dir_generated_prompts(self) -> Path:
        return self.root / "prompts" / "generated"

    @property
    def overrides_path(self) -> Path:
        """GUIで変更した設定の保存先。

        コメント付きの sticker_config.yaml を書き換えずに済むよう、
        差分だけを別ファイルに保存して読み込み時にマージします。
        """
        return self.path.parent / OVERRIDES_FILENAME

    def save_overrides(self, updates: dict[str, Any]) -> Path:
        """{"font.size": 48} 形式の差分を overrides.yaml へ保存します。"""
        path = self.overrides_path
        current: dict = {}
        if path.exists():
            current = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for dotted, value in updates.items():
            set_dotted(current, dotted, value)
            set_dotted(self.raw, dotted, value)  # 実行中の設定にも即反映
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# このファイルは GUI から自動生成されます。\n"
            "# sticker_config.yaml の値をここで上書きします。手動編集も可能です。\n"
            + yaml.safe_dump(current, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        return path

    def ensure_output_dirs(self) -> None:
        for d in (
            self.dir_generated,
            self.dir_final,
            self.dir_main,
            self.dir_tab,
            self.dir_packages,
            self.dir_generated_prompts,
        ):
            d.mkdir(parents=True, exist_ok=True)


def load_config(path: str | Path | None = None, *, load_env: bool = True) -> Config:
    """設定を読み込みます。

    Args:
        path: YAML のパス。None なら config/sticker_config.yaml。
        load_env: .env を読み込むか。テストでは False にできます。
    """
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not cfg_path.is_absolute():
        cfg_path = PROJECT_ROOT / cfg_path
    if not cfg_path.exists():
        raise ConfigError(f"設定ファイルが見つかりません: {cfg_path}")

    with cfg_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"設定ファイルの形式が不正です: {cfg_path}")

    # GUI が保存した差分設定を重ねます（元のYAMLのコメントを壊さないため）。
    overrides_path = cfg_path.parent / OVERRIDES_FILENAME
    if overrides_path.exists():
        overrides = yaml.safe_load(overrides_path.read_text(encoding="utf-8")) or {}
        if isinstance(overrides, dict):
            raw = _deep_merge(raw, overrides)

    root = cfg_path.parent.parent
    if load_env:
        load_dotenv(root / ".env")

    return Config(raw=raw, root=root, path=cfg_path)
