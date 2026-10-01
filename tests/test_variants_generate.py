"""候補（variant）の生成のテスト（Phase 1b）。

本物の画像生成APIは呼びません（FakeProvider を使います）。
最重要の確認点は「採用中の原画 generated/ と完成画像 final/ が変わらないこと」です。
"""

from __future__ import annotations

import hashlib
import io
import json

import pytest
from PIL import Image

from src import image_processor as ip
from src import variants as vr
from src.csv_loader import StickerEntry
from src.image_generator import ImageGenerator
from src.logger import RunLogger, StateStore
from src.providers.base import ImageGenerationProvider, ProviderError
from tests.conftest import make_character


class CountingProvider(ImageGenerationProvider):
    """N回目の呼び出しだけ失敗させられるダミープロバイダ。"""

    name = "fake"

    def __init__(self, fail_on: set[int] | None = None) -> None:
        self.calls = 0
        self.fail_on = fail_on or set()

    def generate(self, prompt, reference_image=None, output_path=None):
        self.calls += 1
        if self.calls in self.fail_on:
            raise ProviderError("fatal error")
        buf = io.BytesIO()
        make_character((512, 512), color=(10 * self.calls % 255, 80, 80, 255)).save(buf, format="PNG")
        data = buf.getvalue()
        self._write(data, output_path)
        return data

    def estimate_cost_usd(self, count: int) -> float:
        return 0.011 * count


def _entry(sid="001", text="了解！"):
    return StickerEntry(id=sid, text=text, action="敬礼", expression="笑顔", category="basic")


def _generator(cfg, provider=None):
    master = cfg.master_image_path
    master.parent.mkdir(parents=True, exist_ok=True)
    make_character((512, 512)).save(master)
    gen = ImageGenerator(cfg, RunLogger(cfg.log_path, echo=False), StateStore(cfg.state_path))
    gen._provider = provider or CountingProvider()
    gen.config.raw["generation"]["retry_backoff_sec"] = 0
    return gen


def _sha1(path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def _state(cfg) -> dict:
    return json.loads(cfg.variants_path.read_text(encoding="utf-8"))


# --- Test 1: 候補が作られる ------------------------------------------------
def test_generates_requested_number_of_variants(tmp_config):
    gen = _generator(tmp_config)
    results = vr.generate_variants(tmp_config, [_entry()], 3, gen)

    assert [r["status"] for r in results] == ["generated"] * 3
    for vid in ("v001", "v002", "v003"):
        assert (tmp_config.dir_variants / "001" / f"{vid}.png").exists()
    assert gen._provider.calls == 3
    assert not list(tmp_config.dir_variants.rglob("*.part"))   # 一時ファイルを残さない


# --- Test 2 / 3: 既存の採用画像に触れない ----------------------------------
def test_generated_and_final_are_untouched(tmp_config):
    raw = tmp_config.dir_generated / "001.png"
    fin = tmp_config.dir_final / "001.png"
    ip.save_png(make_character((512, 512)), raw)
    ip.save_png(make_character((370, 320)), fin)
    before = (_sha1(raw), raw.stat().st_mtime_ns, _sha1(fin), fin.stat().st_mtime_ns)

    vr.generate_variants(tmp_config, [_entry()], 2, _generator(tmp_config))

    assert (_sha1(raw), raw.stat().st_mtime_ns, _sha1(fin), fin.stat().st_mtime_ns) == before
    assert list(tmp_config.dir_generated.glob("*.png")) == [raw]      # 増えてもいない
    archive = tmp_config.root / "output" / "archive" / "replaced"
    assert not archive.exists() or not list(archive.glob("*.png"))    # 退避も起きない


def test_state_json_is_not_touched_by_variant_generation(tmp_config):
    gen = _generator(tmp_config)
    vr.generate_variants(tmp_config, [_entry()], 2, gen)
    store = StateStore(tmp_config.state_path)
    assert store.status("001") is None      # 採用中の進捗は変えない


# --- Test 4 / 5: 連番 ------------------------------------------------------
def test_new_variants_continue_after_existing_ones(tmp_config):
    gen = _generator(tmp_config)
    vr.generate_variants(tmp_config, [_entry()], 2, gen)
    vr.generate_variants(tmp_config, [_entry()], 2, gen)

    ids = [v.variant_id for v in vr.list_variants(tmp_config, "001")]
    assert ids == ["v001", "v002", "v003", "v004"]
    assert _state(tmp_config)["stickers"]["001"]["next_seq"] == 5


def test_missing_numbers_are_not_reused(tmp_config):
    gen = _generator(tmp_config)
    vr.generate_variants(tmp_config, [_entry()], 3, gen)
    # v002 を人が消した状況を作る（記録からもファイルからも消す）
    data = _state(tmp_config)
    data["stickers"]["001"]["variants"] = [
        v for v in data["stickers"]["001"]["variants"] if v["variant_id"] != "v002"]
    tmp_config.variants_path.write_text(json.dumps(data), encoding="utf-8")
    (tmp_config.dir_variants / "001" / "v002.png").unlink()

    vr.generate_variants(tmp_config, [_entry()], 1, gen)
    ids = [v.variant_id for v in vr.list_variants(tmp_config, "001")]
    assert ids == ["v001", "v003", "v004"]      # v002 は再利用しない


# --- Test 6: メタデータ ----------------------------------------------------
def test_metadata_is_recorded(tmp_config):
    gen = _generator(tmp_config)
    vr.generate_variants(tmp_config, [_entry()], 1, gen)

    item = _state(tmp_config)["stickers"]["001"]["variants"][0]
    assert item["variant_id"] == "v001"
    assert item["file"] == "output/variants/001/v001.png"
    assert item["source"] == "api" and item["verdict"] == "pending"
    assert item["human_rating"] is None and item["created_at"]
    assert item["provider"] == tmp_config.provider_name
    assert item["model"] == tmp_config.model
    assert item["model_params"]["quality"] == tmp_config.quality
    # プロンプト全文のSHA1と一致する
    assert item["prompt_sha1"] == vr.sha1_text(gen.prompt_for(_entry()))
    # 実際に参照したマスター画像のSHA1と一致する
    assert item["master_hash"] == vr.sha1_file(tmp_config.master_image_path)
    # コストは既存のロジック（プロバイダ）から取る
    assert item["cost_usd"] == gen.estimate_cost_usd(1)
    assert item["prompt_file"] == "prompts/generated/001.txt"


def test_master_hash_is_the_one_used_not_the_current_one(tmp_config):
    gen = _generator(tmp_config)
    vr.generate_variants(tmp_config, [_entry()], 1, gen)
    first_hash = _state(tmp_config)["stickers"]["001"]["variants"][0]["master_hash"]

    # マスターを別のキャラに差し替えてから、もう1案作る
    make_character((512, 512), color=(10, 20, 200, 255)).save(tmp_config.master_image_path)
    gen2 = ImageGenerator(tmp_config, RunLogger(tmp_config.log_path, echo=False),
                          StateStore(tmp_config.state_path))
    gen2._provider = CountingProvider()
    vr.generate_variants(tmp_config, [_entry()], 1, gen2)

    items = _state(tmp_config)["stickers"]["001"]["variants"]
    assert items[0]["master_hash"] == first_hash
    assert items[1]["master_hash"] != first_hash        # 生成時点のマスターを残す


# --- Test 7: 途中で失敗しても成功分は残る ----------------------------------
def test_failure_keeps_successful_variants_and_consumes_number(tmp_config):
    gen = _generator(tmp_config, CountingProvider(fail_on={3}))
    results = vr.generate_variants(tmp_config, [_entry()], 3, gen)

    assert [r["status"] for r in results] == ["generated", "generated", "error"]
    assert (tmp_config.dir_variants / "001" / "v001.png").exists()
    assert (tmp_config.dir_variants / "001" / "v002.png").exists()
    assert not (tmp_config.dir_variants / "001" / "v003.png").exists()

    record = _state(tmp_config)["stickers"]["001"]
    assert [v["variant_id"] for v in record["variants"]] == ["v001", "v002"]  # 記録だけ残らない
    assert record["next_seq"] == 4                                            # v003 は再利用しない

    vr.generate_variants(tmp_config, [_entry()], 1, gen)
    assert [v.variant_id for v in vr.list_variants(tmp_config, "001")] == ["v001", "v002", "v004"]


# --- legacy との共存 -------------------------------------------------------
def test_legacy_image_and_new_variants_coexist(tmp_config):
    raw = tmp_config.dir_generated / "001.png"
    ip.save_png(make_character((512, 512)), raw)
    before = _sha1(raw)

    vr.generate_variants(tmp_config, [_entry()], 2, _generator(tmp_config))

    sticker = vr.get_sticker(tmp_config, "001")
    assert [(v.variant_id, v.source) for v in sticker.variants] == [
        ("v001", "legacy"), ("v002", "api"), ("v003", "api")]
    assert sticker.adopted == "v001"                     # 採用状態は変わらない
    # Phase 5b(C1) で変更: 記録に残す時点で候補置き場へ実体をコピーし、そちらを指します。
    # （generated/001.png は採用のたびに中身が変わるため、元の絵を残せなくなるのを防ぐ）
    assert sticker.variants[0].file == "output/variants/001/v001.png"
    assert _sha1(tmp_config.dir_variants / "001" / "v001.png") == before
    assert _sha1(raw) == before                          # 採用中の原画は動かさない


def test_adopted_is_not_changed_by_generating(tmp_config):
    ip.save_png(make_character((512, 512)), tmp_config.dir_generated / "001.png")
    gen = _generator(tmp_config)
    vr.generate_variants(tmp_config, [_entry()], 1, gen)
    vr.generate_variants(tmp_config, [_entry()], 1, gen)
    assert _state(tmp_config)["stickers"]["001"]["adopted"] == "v001"
    assert all(v["verdict"] == "pending"
               for v in _state(tmp_config)["stickers"]["001"]["variants"][1:])


# --- 複数スタンプ・dry-run -------------------------------------------------
def test_multiple_stickers(tmp_config):
    gen = _generator(tmp_config)
    vr.generate_variants(tmp_config, [_entry("001"), _entry("002", "OK！")], 2, gen)
    for sid in ("001", "002"):
        assert sorted(p.name for p in (tmp_config.dir_variants / sid).glob("*.png")) == \
            ["v001.png", "v002.png"]
    assert set(_state(tmp_config)["stickers"]) == {"001", "002"}


def test_dry_run_creates_nothing(tmp_config):
    gen = _generator(tmp_config)
    gen.dry_run = True
    results = vr.generate_variants(tmp_config, [_entry()], 3, gen)
    assert [r["status"] for r in results] == ["dry-run"] * 3
    assert gen._provider.calls == 0
    assert not tmp_config.dir_variants.exists()
    assert not tmp_config.variants_path.exists()


# --- Test 8: 既存の1枚生成が従来どおり -------------------------------------
def test_existing_single_generation_is_unchanged(tmp_config):
    gen = _generator(tmp_config)
    result = gen.generate_one(_entry())        # output_path を渡さない＝従来の呼び方

    assert result.status == "generated"
    assert result.path == tmp_config.dir_generated / "001.png"
    assert (tmp_config.dir_generated / "001.png").exists()
    assert StateStore(tmp_config.state_path).status("001") == "generated"
    assert not tmp_config.dir_variants.exists()

    # 2回目はスキップされる（既存のキャッシュ動作）
    assert gen.generate_one(_entry()).status == "skipped"
    assert gen._provider.calls == 1


# --- 例外で中断しても、できた候補と番号は失われない --------------------------
def test_crash_midway_keeps_records_and_numbers(tmp_config):
    class Boom(CountingProvider):
        def generate(self, prompt, reference_image=None, output_path=None):
            if self.calls >= 2:
                raise KeyboardInterrupt("ユーザー中断")
            return super().generate(prompt, reference_image, output_path)

    gen = _generator(tmp_config, Boom())
    with pytest.raises(KeyboardInterrupt):
        vr.generate_variants(tmp_config, [_entry()], 3, gen)

    record = _state(tmp_config)["stickers"]["001"]
    assert [v["variant_id"] for v in record["variants"]] == ["v001", "v002"]  # 成功分は記録済み
    assert record["next_seq"] == 4        # 中断した v003 の番号は消費したまま

    # 中断後に再実行しても、既存のPNGを上書きしない
    gen2 = _generator(tmp_config)
    vr.generate_variants(tmp_config, [_entry()], 1, gen2)
    assert [v.variant_id for v in vr.list_variants(tmp_config, "001")] == ["v001", "v002", "v004"]


def test_orphan_png_is_never_overwritten(tmp_config):
    """記録に無いPNGが残っていても、その番号は使い回さない。"""
    ip.save_png(make_character((512, 512)), tmp_config.dir_variants / "001" / "v001.png")
    vr.generate_variants(tmp_config, [_entry()], 1, _generator(tmp_config))
    assert [v.variant_id for v in vr.list_variants(tmp_config, "001")] == ["v002"]
    assert (tmp_config.dir_variants / "001" / "v001.png").exists()
