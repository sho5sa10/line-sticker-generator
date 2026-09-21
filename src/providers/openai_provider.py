"""OpenAI (gpt-image-1) 用の画像生成プロバイダ実装。

APIキーは .env / 環境変数からのみ読み込みます。ハードコードは禁止です。
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

from .base import ImageGenerationProvider, ProviderError, RetryableProviderError

# gpt-image-1 の概算単価(USD/枚, 1024x1024)。
# 画像出力トークン課金のため、実際の請求額は変動します。
# 必ず https://platform.openai.com/docs/pricing で最新単価を確認してください。
PRICE_TABLE_USD: dict[str, dict[str, float]] = {
    "gpt-image-1": {"low": 0.011, "medium": 0.042, "high": 0.167},
    "gpt-image-1-mini": {"low": 0.005, "medium": 0.015, "high": 0.060},
}
DEFAULT_UNIT_PRICE_USD = 0.042


class OpenAIImageProvider(ImageGenerationProvider):
    """OpenAI Images API (generate / edit) を利用するプロバイダ。"""

    name = "openai"

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "gpt-image-1",
        size: str = "1024x1024",
        quality: str = "medium",
        background: str = "transparent",
        output_format: str = "png",
        timeout_sec: float = 180.0,
    ) -> None:
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            raise ProviderError(
                "OPENAI_API_KEY が設定されていません。"
                ".env.example をコピーして .env を作成し、APIキーを記入してください。"
            )
        self.model = model
        self.size = size
        self.quality = quality
        self.background = background
        self.output_format = output_format
        self.timeout_sec = timeout_sec
        self._client = None

    # ------------------------------------------------------------------
    @property
    def client(self):
        """OpenAI クライアントを遅延生成します（import失敗を実行時まで遅らせる）。"""
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:  # pragma: no cover - 環境依存
                raise ProviderError(
                    "openai パッケージが見つかりません。"
                    "`pip install -r requirements.txt` を実行してください。"
                ) from exc
            kwargs = {"api_key": self.api_key, "timeout": self.timeout_sec}
            base_url = os.getenv("OPENAI_BASE_URL")
            if base_url:
                kwargs["base_url"] = base_url
            self._client = OpenAI(**kwargs)
        return self._client

    # ------------------------------------------------------------------
    def generate(
        self,
        prompt: str,
        reference_image: str | None = None,
        output_path: str | None = None,
    ) -> bytes:
        """画像を1枚生成し、PNGのバイト列を返します。

        reference_image が指定されていれば images.edit（参照画像あり）、
        なければ images.generate を使用します。
        """
        try:
            if reference_image and Path(reference_image).exists():
                data = self._edit(prompt, reference_image)
            else:
                data = self._generate(prompt)
        except (ProviderError, RetryableProviderError):
            raise
        except Exception as exc:  # noqa: BLE001 - SDK例外を分類して包み直す
            raise self._classify(exc) from exc

        if not data:
            raise ProviderError("APIレスポンスに画像データが含まれていません")

        self._write(data, output_path)
        return data

    # ------------------------------------------------------------------
    def _generate(self, prompt: str) -> bytes:
        kwargs = {
            "model": self.model,
            "prompt": prompt,
            "size": self.size,
            "n": 1,
        }
        if self.model.startswith("gpt-image"):
            kwargs["background"] = self.background
            kwargs["quality"] = self.quality
            kwargs["output_format"] = self.output_format
        result = self.client.images.generate(**kwargs)
        return self._extract(result)

    def _edit(self, prompt: str, reference_image: str) -> bytes:
        kwargs = {
            "model": self.model,
            "prompt": prompt,
            "size": self.size,
            "n": 1,
        }
        if self.model.startswith("gpt-image"):
            kwargs["background"] = self.background
            kwargs["quality"] = self.quality
            kwargs["output_format"] = self.output_format
        with open(reference_image, "rb") as fh:
            result = self.client.images.edit(image=[fh], **kwargs)
        return self._extract(result)

    @staticmethod
    def _extract(result) -> bytes:
        items = getattr(result, "data", None) or []
        if not items:
            raise ProviderError("APIレスポンスが空です")
        item = items[0]
        b64 = getattr(item, "b64_json", None)
        if b64:
            return base64.b64decode(b64)

        url = getattr(item, "url", None)
        if url:
            import httpx

            resp = httpx.get(url, timeout=60.0)
            resp.raise_for_status()
            return resp.content
        raise ProviderError("APIレスポンスに b64_json も url もありません")

    @staticmethod
    def _classify(exc: Exception) -> ProviderError:
        """SDK例外をリトライ可否で分類します。"""
        name = type(exc).__name__
        status = getattr(exc, "status_code", None)
        retryable_names = {
            "RateLimitError",
            "APIConnectionError",
            "APITimeoutError",
            "InternalServerError",
            "APIStatusError",
        }
        if name in retryable_names or (isinstance(status, int) and status >= 500) or status == 429:
            return RetryableProviderError(f"{name}: {exc}")
        return ProviderError(f"{name}: {exc}")

    # ------------------------------------------------------------------
    def estimate_cost_usd(self, count: int) -> float:
        unit = PRICE_TABLE_USD.get(self.model, {}).get(self.quality, DEFAULT_UNIT_PRICE_USD)
        return round(unit * max(count, 0), 4)

    def unit_price_usd(self) -> float:
        return PRICE_TABLE_USD.get(self.model, {}).get(self.quality, DEFAULT_UNIT_PRICE_USD)
