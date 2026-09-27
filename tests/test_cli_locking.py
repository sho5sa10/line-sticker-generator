"""STEP 9 の残課題 #2: CLI の生成・再合成も、GUI の採用と同じスタンプの鍵の中で行う。

`python -m src.main generate` / `render` を別スレッドで実際に動かし、採用（とその巻き戻し）と
同時に走らないこと、生成した画像が巻き戻しに消されないことを確かめます。
画像生成APIは呼びません（偽のプロバイダ）。順序は Event で制御します。
"""

from __future__ import annotations

import hashlib
import io
import json
import threading
from pathlib import Path

import pytest

from src import image_generator as ig
from src import main as cli
from src import pipeline
from src import validator as vd
from src import variants as vr
from src.csv_loader import StickerEntry
from src.providers.base import ImageGenerationProvider
from tests.test_scoring import sticker_like

ENTRY = StickerEntry(id="001", text="了解！", action="敬礼", expression="笑顔", category="basic")
WAIT = 30


def _png(color) -> bytes:
    buf = io.BytesIO()
    sticker_like(body=color).save(buf, format="PNG")
    return buf.getvalue()


def _sha(path) -> str:
    return hashlib.sha1(Path(path).read_bytes()).hexdigest()


class _NG:
    ok = False
    errors = [type("Issue", (), {"message": "NG"})()]
    warnings = []
    issues = errors


class _GatedProvider(ImageGenerationProvider):
    name = "fake"

    def __init__(self, gate, data):
        self.gate, self.data = gate, data

    def generate(self, prompt, reference_image=None, output_path=None):
        self._write(self.data, output_path)
        self.gate["paused"].set()
        assert self.gate["go"].wait(WAIT)
        return self.data

    def estimate_cost_usd(self, count):
        return 0.0


@pytest.fixture
def project(tmp_config, monkeypatch):
    """CLI から使える一時プロジェクト（001 に候補、v002 を採用中）。"""
    (tmp_config.root / "data").mkdir(parents=True, exist_ok=True)
    (tmp_config.root / "data" / "stickers.csv").write_text(
        "id,text,action,expression,category\n001,了解！,敬礼,笑顔,basic\n", encoding="utf-8")
    raw = dict(tmp_config.raw)
    raw["data"] = {**raw.get("data", {}), "csv": "data/stickers.csv"}
    import yaml
    tmp_config.path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    tmp_config.raw["data"]["csv"] = "data/stickers.csv"
    tmp_config.master_image_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_config.master_image_path.write_bytes(_png((250, 205, 60, 255)))
    monkeypatch.setenv("OPENAI_API_KEY", "test-dummy-not-used")
    (tmp_config.dir_generated / "001.png").write_bytes(_png((250, 205, 60, 255)))
    for color in ((90, 170, 220, 255), (210, 110, 140, 255)):
        def reg(state, color=color):
            record = vr.ensure_record(tmp_config, state, "001")
            vid, path = vr.allocate_variant(tmp_config, record, "001")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(_png(color))
            vr.register_variant(tmp_config, record, vid, path, meta={})
        vr.update_sticker(tmp_config, "001", reg)
    vr.adopt(tmp_config, ENTRY, "v002")
    gate = {"paused": threading.Event(), "go": threading.Event()}
    data = _png((120, 200, 130, 255))
    monkeypatch.setattr(ig, "create_provider", lambda config: _GatedProvider(gate, data))
    return tmp_config, gate, data


def _thread(fn):
    out = {"error": None, "value": None}

    def target():
        try:
            out["value"] = fn()
        except BaseException as exc:  # noqa: BLE001
            out["error"] = exc
    t = threading.Thread(target=target)
    t.start()
    return t, out


def test_cli_generate_and_a_failing_adoption_never_interleave(project, monkeypatch):
    cfg, gate, data = project
    record_before = json.loads(cfg.variants_path.read_text(encoding="utf-8"))["stickers"]["001"]
    generating, gen = _thread(lambda: cli.main(
        ["--config", str(cfg.path), "generate", "--id", "001", "--force", "--yes"]))
    assert gate["paused"].wait(WAIT)                 # CLI の生成が generated に書く直前で止まっている

    validating = threading.Event()

    def validate(*args, **kwargs):
        if threading.current_thread() is not generating:
            validating.set()
            gate["go"].set()
            generating.join(5)                       # 鍵が無ければ、この間に CLI が generated を書き換える
            return _NG()
        return vd.__dict__["_real_validate"](*args, **kwargs)
    vd.__dict__["_real_validate"] = vd.validate_sticker
    monkeypatch.setattr(vd, "validate_sticker", validate)
    adopting, adopt = _thread(lambda: vr.adopt(cfg, ENTRY, "v003"))
    adoption_ran_during_generation = validating.wait(1.0)
    gate["go"].set()
    generating.join(WAIT)
    adopting.join(WAIT)
    monkeypatch.undo()

    assert not adoption_ran_during_generation      # 採用は CLI の生成が終わるまで待った
    assert gen["error"] is None and gen["value"] == 0
    assert isinstance(adopt["error"], vr.AdoptError)
    assert _sha(cfg.dir_generated / "001.png") == hashlib.sha1(data).hexdigest()   # 巻き戻しに消されない
    assert json.loads(cfg.variants_path.read_text(encoding="utf-8"))["stickers"]["001"] == record_before
    assert not list(vr.adopt_backup_root(cfg).glob("*"))
    leftovers = [p.name for p in (cfg.root / "output").rglob("*") if p.name.endswith((".lock", ".tmp", ".part"))]
    assert leftovers == []


def test_cli_render_waits_for_the_adoption(project, monkeypatch):
    cfg, _gate, _data = project
    at_validation, release = threading.Event(), threading.Event()
    real_validate, real_render = vd.validate_sticker, pipeline.render_final
    cli_renders = []
    main_thread = {}

    def validate(*args, **kwargs):
        at_validation.set()
        assert release.wait(WAIT)
        return real_validate(*args, **kwargs)

    def render(*args, **kwargs):
        if threading.current_thread() is main_thread.get("t"):
            cli_renders.append(at_validation.is_set() and not release.is_set())
        return real_render(*args, **kwargs)
    monkeypatch.setattr(vd, "validate_sticker", validate)
    monkeypatch.setattr(pipeline, "render_final", render)
    monkeypatch.setattr(cli, "render_final", render)
    adopting, adopt = _thread(lambda: vr.adopt(cfg, ENTRY, "v003"))
    assert at_validation.wait(WAIT)

    rendering, rend = _thread(lambda: cli.main(["--config", str(cfg.path), "render", "--id", "001"]))
    main_thread["t"] = rendering
    rendering.join(1.0)                              # 鍵が無ければ、この間に再合成してしまう
    release.set()
    adopting.join(WAIT)
    rendering.join(WAIT)
    monkeypatch.undo()

    assert adopt["error"] is None and rend["error"] is None and rend["value"] == 0
    assert cli_renders == [False]                    # 採用の途中では再合成しなかった
    check = cfg.root / "check.png"
    from src.text_renderer import TextStyle
    pipeline.render_final(cfg, ENTRY, TextStyle.from_config(cfg), output_path=check)
    assert _sha(check) == _sha(cfg.dir_final / "001.png")      # final はいまの原画から作ったもの
