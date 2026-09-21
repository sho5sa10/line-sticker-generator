"""画像生成プロバイダのファクトリ。"""

from __future__ import annotations

from .base import ImageGenerationProvider, ProviderError, RetryableProviderError

__all__ = [
    "ImageGenerationProvider",
    "ProviderError",
    "RetryableProviderError",
    "create_provider",
    "AVAILABLE_PROVIDERS",
]

AVAILABLE_PROVIDERS = ("openai",)


def estimate_cost_usd(model: str, quality: str, count: int) -> float | None:
    """APIキー無しでも概算コストを出せる静的な見積り。

    単価表に無いモデルの場合は None を返します。
    """
    from .openai_provider import PRICE_TABLE_USD

    unit = PRICE_TABLE_USD.get(model, {}).get(quality)
    if unit is None:
        return None
    return round(unit * max(count, 0), 4)


def create_provider(config) -> ImageGenerationProvider:
    """設定から画像生成プロバイダを生成します。

    新しいプロバイダ（gemini / stability など）を追加する場合は、
    providers/ に ImageGenerationProvider の実装を置き、ここに分岐を足してください。
    """
    name = (config.provider_name or "openai").lower()

    if name == "openai":
        from .openai_provider import OpenAIImageProvider

        return OpenAIImageProvider(
            api_key=config.api_key,
            model=config.model,
            size=str(config.get("generation.size", "1024x1024")),
            quality=config.quality,
            background=str(config.get("generation.background", "transparent")),
            output_format=str(config.get("generation.output_format", "png")),
            timeout_sec=float(config.get("generation.timeout_sec", 180)),
        )

    raise ProviderError(
        f"未対応のプロバイダです: {name} (利用可能: {', '.join(AVAILABLE_PROVIDERS)})"
    )
