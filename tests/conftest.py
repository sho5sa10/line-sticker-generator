"""pytest 共通フィクスチャ。

ここにあるテストは画像生成APIを一切呼びません。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml
from PIL import Image, ImageDraw

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config import Config, load_config  # noqa: E402
from src.text_renderer import resolve_font_path  # noqa: E402


@pytest.fixture(scope="session")
def real_config() -> Config:
    """リポジトリの実設定（.env は読み込みません）。"""
    return load_config(load_env=False)


@pytest.fixture(scope="session")
def font_path(real_config) -> str:
    try:
        return str(resolve_font_path(real_config))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"日本語フォントが見つからないためスキップします: {exc}")


@pytest.fixture
def tmp_config(tmp_path, real_config, font_path) -> Config:
    """出力先を tmp_path に向けた設定を作ります。"""
    raw = yaml.safe_load(real_config.path.read_text(encoding="utf-8"))
    raw["font"]["path"] = font_path
    raw["output"] = {
        "generated": "output/generated",
        "final": "output/final",
        "main": "output/main",
        "tab": "output/tab",
        "packages": "output/packages",
        "log": "output/generation.log",
        "state": "output/state.json",
        "gallery": "output/gallery.html",
    }
    cfg = Config(raw=raw, root=tmp_path, path=tmp_path / "config" / "sticker_config.yaml")
    cfg.ensure_output_dirs()
    return cfg


def make_character(size=(512, 512), color=(220, 80, 80, 255)) -> Image.Image:
    """テスト用のダミーキャラクター画像（透過背景 + 中央に楕円）。"""
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    w, h = size
    draw.ellipse((w * 0.2, h * 0.15, w * 0.8, h * 0.85), fill=color)
    return img


@pytest.fixture
def dummy_character() -> Image.Image:
    return make_character()


@pytest.fixture
def dummy_csv(tmp_path) -> Path:
    p = tmp_path / "stickers.csv"
    p.write_text(
        "id,text,action,expression,category\n"
        "001,了解！,敬礼する,自信のある笑顔,basic\n"
        "002,ありがとう！,手を合わせる,嬉しそうな笑顔,thanks\n"
        "003,ちょっと何言ってるかわからない,首をかしげる,無表情の真顔,misc\n",
        encoding="utf-8",
    )
    return p
