"""生成制御（キャッシュ・スキップ・dry-run・リトライ・再開）のテスト。

本物の画像生成APIは呼びません。ダミープロバイダを差し込んで検証します。
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from src.image_generator import ImageGenerator, MasterImageMissingError, check_master_image
from src.logger import RunLogger, StateStore
from src.providers.base import ImageGenerationProvider, ProviderError, RetryableProviderError
from tests.conftest import make_character


class FakeProvider(ImageGenerationProvider):
    """呼び出し回数を数えるだけのダミープロバイダ。"""

    name = "fake"

    def __init__(self, fail_times: int = 0, fatal: bool = False) -> None:
        self.calls = 0
        self.prompts: list[str] = []
        self.fail_times = fail_times
        self.fatal = fatal

    def generate(self, prompt, reference_image=None, output_path=None):
        self.calls += 1
        self.prompts.append(prompt)
        if self.calls <= self.fail_times:
            if self.fatal:
                raise ProviderError("fatal error")
            raise RetryableProviderError("temporary error")
        buf = io.BytesIO()
        make_character((512, 512)).save(buf, format="PNG")
        data = buf.getvalue()
        self._write(data, output_path)
        return data

    def estimate_cost_usd(self, count: int) -> float:
        return 0.01 * count


@pytest.fixture
def generator(tmp_config):
    """マスター画像を用意したジェネレータ。"""
    master = tmp_config.master_image_path
    master.parent.mkdir(parents=True, exist_ok=True)
    make_character((512, 512)).save(master)

    logger = RunLogger(tmp_config.log_path, echo=False)
    state = StateStore(tmp_config.state_path)
    gen = ImageGenerator(tmp_config, logger, state)
    gen._provider = FakeProvider()
    gen.config.raw["generation"]["retry_backoff_sec"] = 0
    return gen


def _entry(sid="001", text="了解！"):
    from src.csv_loader import StickerEntry

    return StickerEntry(id=sid, text=text, action="敬礼", expression="笑顔", category="basic")


# --- マスター画像 -------------------------------------------------------
def test_missing_master_image_raises(tmp_config):
    with pytest.raises(MasterImageMissingError, match="character_master.pngがありません"):
        check_master_image(tmp_config)


def test_master_image_found(generator, tmp_config):
    assert check_master_image(tmp_config).exists()


# --- 生成 / キャッシュ --------------------------------------------------
def test_generate_creates_png(generator):
    result = generator.generate_one(_entry())
    assert result.status == "generated"
    assert result.path.exists()
    with Image.open(result.path) as im:
        assert im.format == "PNG"
    assert generator._provider.calls == 1


def test_existing_image_is_skipped(generator):
    generator.generate_one(_entry())
    assert generator._provider.calls == 1

    result = generator.generate_one(_entry())
    assert result.status == "skipped"
    assert generator._provider.calls == 1, "既存画像があればAPIを呼ばない"


def test_force_regenerates(generator):
    generator.generate_one(_entry())
    result = generator.generate_one(_entry(), force=True)
    assert result.status == "generated"
    assert generator._provider.calls == 2


def test_force_archives_previous_image(generator, tmp_config):
    """強制再生成でも前の画像（手持ちの取り込み画像を含む）を消さずに退避します。"""
    from src.importer import archive_dir

    generator.generate_one(_entry())
    first = generator.output_path(_entry()).read_bytes()
    generator.generate_one(_entry(), force=True)

    archived = list(archive_dir(tmp_config).glob("001_*.png"))
    assert len(archived) == 1
    assert archived[0].read_bytes() == first


def test_failed_force_keeps_previous_image(generator, tmp_config):
    """APIが失敗したら、前の画像はそのまま残ります。"""
    generator.generate_one(_entry())
    before = generator.output_path(_entry()).read_bytes()

    generator._provider = FakeProvider(fail_times=99, fatal=True)
    result = generator.generate_one(_entry(), force=True)
    assert result.status == "error"
    assert generator.output_path(_entry()).read_bytes() == before


def test_dry_run_never_calls_api(tmp_config, generator):
    generator.dry_run = True
    result = generator.generate_one(_entry())
    assert result.status == "dry-run"
    assert generator._provider.calls == 0
    assert not generator.output_path(_entry()).exists()
    assert result.prompt  # プロンプトは組み立てられている


def test_prompt_file_is_saved(generator, tmp_config):
    generator.generate_one(_entry("007"))
    assert (tmp_config.dir_generated_prompts / "007.txt").exists()


# --- リトライ / 再開 ----------------------------------------------------
def test_retries_then_succeeds(generator):
    generator._provider = FakeProvider(fail_times=2)
    result = generator.generate_one(_entry())
    assert result.status == "generated"
    assert generator._provider.calls == 3


def test_gives_up_after_retries(generator):
    generator._provider = FakeProvider(fail_times=99)
    result = generator.generate_one(_entry())
    assert result.status == "error"
    assert generator._provider.calls == int(generator.config.get("generation.retries", 3))
    assert not generator.output_path(_entry()).exists(), "失敗時に壊れたPNGを残さない"


def test_fatal_error_does_not_retry(generator):
    generator._provider = FakeProvider(fail_times=99, fatal=True)
    result = generator.generate_one(_entry())
    assert result.status == "error"
    assert generator._provider.calls == 1


def test_error_is_recorded_for_resume(generator, tmp_config):
    generator._provider = FakeProvider(fail_times=99)
    generator.generate_one(_entry("035"))

    state = StateStore(tmp_config.state_path)
    assert state.status("035") == "error"
    assert "035" in state.failed_ids()


def test_log_file_records_events(generator, tmp_config):
    generator.generate_one(_entry("001"))
    log = tmp_config.log_path.read_text(encoding="utf-8")
    assert "001 START" in log
    assert "001 API REQUEST" in log
    assert "001 IMAGE GENERATED" in log


# --- コスト見積り -------------------------------------------------------
def test_pending_count_respects_cache(generator):
    entries = [_entry("001"), _entry("002"), _entry("003")]
    assert generator.pending_count(entries) == 3
    generator.generate_one(entries[0])
    assert generator.pending_count(entries) == 2
    assert generator.pending_count(entries, force=True) == 3


def test_estimate_cost(generator):
    assert generator.estimate_cost_usd(10) == pytest.approx(0.1)
