"""画像生成の実行制御（キャッシュ・リトライ・dry-run・途中再開）。

APIコストに直結する層なので、以下を必ず守ります。
  - 既に output/generated/<id>.png があれば API を呼びません（--force で上書き）。
  - dry-run では API を一切呼びません。
  - 失敗は state.json に記録し、次回そこから再開できます。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from .csv_loader import StickerEntry
from .prompt_generator import build_prompt, load_master_prompt, save_prompt
from .providers import (
    ProviderError,
    RetryableProviderError,
    create_provider,
    estimate_cost_usd,
)


class MasterImageMissingError(Exception):
    """キャラクターマスター画像が無い場合に送出されます。"""


@dataclass
class GenerationResult:
    """1件分の生成結果。"""

    sticker_id: str
    status: str  # generated | skipped | dry-run | error
    path: Path | None = None
    prompt: str = ""
    detail: str = ""

    @property
    def called_api(self) -> bool:
        return self.status == "generated"


def check_master_image(config) -> Path:
    """キャラクターマスター画像の存在を確認します。

    無い場合は、勝手にキャラクターを生成せず明確にエラーにします。
    """
    p = config.master_image_path
    if not p.exists():
        raise MasterImageMissingError(
            f"character_master.pngがありません\n"
            f"  期待パス: {p}\n"
            f"  先に基準となるキャラクター画像（透過PNG・全身）を配置してください。\n"
            f"  この画像を差し替えるだけで別キャラクターのスタンプセットを作れます。"
        )
    return p


class ImageGenerator:
    """CSVエントリから原画PNGを生成するオーケストレータ。"""

    def __init__(self, config, logger, state, *, dry_run: bool = False) -> None:
        self.config = config
        self.logger = logger
        self.state = state
        self.dry_run = dry_run
        self.master_prompt = load_master_prompt(config.master_prompt_path)
        self._provider = None
        self._reference: str | None = None

    # ------------------------------------------------------------------
    @property
    def provider(self):
        """プロバイダを遅延生成します（dry-run ではAPIキー不要にするため）。"""
        if self._provider is None:
            self._provider = create_provider(self.config)
        return self._provider

    def reference_image(self) -> str | None:
        """参照画像（キャラクターマスター）のパス。"""
        if self._reference is None:
            if not bool(self.config.get("character.use_as_reference", True)):
                return None
            self._reference = str(check_master_image(self.config))
        return self._reference

    def output_path(self, entry: StickerEntry) -> Path:
        return self.config.dir_generated / f"{entry.id}.png"

    def prompt_for(self, entry: StickerEntry) -> str:
        return build_prompt(entry, self.master_prompt)

    # ------------------------------------------------------------------
    def generate_one(self, entry: StickerEntry, *, force: bool = False) -> GenerationResult:
        """1件生成します。既存画像があればスキップします。"""
        out = self.output_path(entry)
        prompt = self.prompt_for(entry)

        if bool(self.config.get("generation.save_prompt", True)):
            save_prompt(prompt, self.config.dir_generated_prompts, entry.id)

        skip_existing = bool(self.config.get("generation.skip_existing", True))
        if out.exists() and skip_existing and not force:
            self.logger.event(entry.id, "SKIP", "already exists")
            return GenerationResult(entry.id, "skipped", out, prompt)

        if self.dry_run:
            self.logger.event(entry.id, "DRY-RUN", "API was not called")
            return GenerationResult(entry.id, "dry-run", None, prompt)

        self.logger.event(entry.id, "START")
        retries = int(self.config.get("generation.retries", 3))
        backoff = float(self.config.get("generation.retry_backoff_sec", 3))
        reference = self.reference_image()

        last_error = ""
        for attempt in range(1, retries + 1):
            try:
                self.logger.event(entry.id, "API REQUEST", f"attempt {attempt}/{retries}")
                # 一時ファイルに書いてから移動し、中断時に壊れたPNGを残さないようにします。
                tmp = out.with_suffix(".png.part")
                self.provider.generate(
                    prompt=prompt,
                    reference_image=reference,
                    output_path=str(tmp),
                )
                tmp.replace(out)
                self.logger.event(entry.id, "IMAGE GENERATED", str(out.name))
                self.state.set(entry.id, "generated")
                return GenerationResult(entry.id, "generated", out, prompt)

            except RetryableProviderError as exc:
                last_error = str(exc)
                self.logger.event(entry.id, "RETRY", last_error)
                if attempt < retries:
                    time.sleep(backoff * attempt)
            except ProviderError as exc:
                last_error = str(exc)
                break
            except Exception as exc:  # noqa: BLE001 - 想定外も記録して継続判断する
                last_error = f"{type(exc).__name__}: {exc}"
                break

        self.logger.error(entry.id, last_error or "unknown error")
        self.state.set(entry.id, "error", last_error)
        return GenerationResult(entry.id, "error", None, prompt, last_error)

    # ------------------------------------------------------------------
    def pending_count(self, entries, *, force: bool = False) -> int:
        """実際にAPIを呼ぶ予定の枚数（コスト見積り用）。"""
        if force:
            return len(entries)
        if not bool(self.config.get("generation.skip_existing", True)):
            return len(entries)
        return sum(1 for e in entries if not self.output_path(e).exists())

    def estimate_cost_usd(self, count: int) -> float | None:
        """概算コスト(USD)。APIキーが無くても静的な単価表から算出します。"""
        if self._provider is not None:
            return self._provider.estimate_cost_usd(count)
        try:
            return self.provider.estimate_cost_usd(count)
        except Exception:  # noqa: BLE001 - APIキー未設定でも見積り表示は止めない
            return estimate_cost_usd(self.config.model, self.config.quality, count)
