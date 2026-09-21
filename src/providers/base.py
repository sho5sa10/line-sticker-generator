"""画像生成プロバイダの抽象インターフェース。

将来 Gemini / Stability などへ差し替えられるよう、
画像生成の呼び出しはすべてこのインターフェース経由にします。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class ProviderError(Exception):
    """プロバイダ側で回復不能なエラーが起きた場合に送出されます。"""


class RetryableProviderError(ProviderError):
    """レート制限・一時的な通信障害など、リトライで回復しうるエラー。"""


class ImageGenerationProvider(ABC):
    """画像生成プロバイダの共通インターフェース。"""

    name: str = "base"

    @abstractmethod
    def generate(
        self,
        prompt: str,
        reference_image: str | None = None,
        output_path: str | None = None,
    ) -> bytes:
        """画像を1枚生成します。

        Args:
            prompt: 画像生成プロンプト。
            reference_image: キャラクター一貫性のための参照画像パス。
            output_path: 指定するとそのパスにPNGを書き出します。

        Returns:
            生成されたPNGのバイト列。
        """
        raise NotImplementedError

    @abstractmethod
    def estimate_cost_usd(self, count: int) -> float:
        """count 枚生成したときの概算コスト(USD)を返します。"""
        raise NotImplementedError

    # --- 共通ユーティリティ ---
    @staticmethod
    def _write(data: bytes, output_path: str | None) -> None:
        if output_path:
            p = Path(output_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
