"""Phase 6 の追加修正: 途中で止まった再生成の再開と、API 応答直後の異常終了。

- 途中の実行は、それが対象にした regen の印がいまも残っているときだけ再開する
  （印を取り消したら、古い実行の記録（regen_run）だけを根拠に作らない）
- API が画像を返した直後・記録する前に落ちても、同じ候補をもう一度プロバイダに頼まない
画像生成APIは呼びません（偽のプロバイダ・別プロセスの強制終了で再現します）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from src import variants as vr
from tests.test_regen import (  # noqa: F401 - 同じ一時プロジェクト（フィクスチャ）を使う
    _ids, _item, _plan, _post, _record, _run, env,
)

PROJECT = Path(__file__).resolve().parents[1]


def _fail_partway(client, box, count=3, succeed=1):
    """001 だけを対象にして count 枚の生成を始め、succeed 枚だけ作って残りを失敗させる。"""
    vr.set_verdict(_cfg(client), "002", "v001", "rejected")   # 002 を対象から外す
    box["fail_on"] = set(range(succeed + 1, count + 1))
    job = _run(client, count=count)
    box["fail_on"] = set()
    assert job["status"] == "finished"
    run = _record(_cfg(client), "001")["regen_run"]
    assert len(run["done"]) == succeed and run["owner"] is None
    return run


def _cfg(client):
    return client.application.config["STICKER_TEST_CONFIG"]


@pytest.fixture
def app(env, tmp_config):
    client, box = env
    client.application.config["STICKER_TEST_CONFIG"] = tmp_config
    return client, box


# ===========================================================================
# Critical 1: 途中の実行の再開は、いまの regen の印を確かめてから
# ===========================================================================
def test_a_resume_generates_only_the_rest_while_the_mark_remains(app, tmp_config):
    client, box = app
    _fail_partway(client, box, count=3, succeed=1)
    plan = _plan(client, 3)
    assert [(t["id"], t["count"], t["resume"]) for t in plan["targets"]] == [("001", 2, True)]
    calls = box["calls"]
    _run(client, count=3)
    assert box["calls"] == calls + 2                            # 残り 2 枚だけ
    record = _record(tmp_config, "001")
    assert record["regen_generated_at"] and "regen_run" not in record


def test_b_withdrawn_regen_is_not_resumed(app, tmp_config):
    client, box = app
    _fail_partway(client, box, count=3, succeed=1)
    vr.set_verdict(tmp_config, "001", "v002", "rejected")      # regen を取り下げた（印が 0 件）
    plan = _plan(client, 3)
    assert plan["targets"] == [] and plan["total"] == 0
    calls = box["calls"]
    r = _post(client, count=3, expected_total=0)
    assert r.status_code == 400                                 # 対象が無いので始まらない
    assert vr.begin_regen(tmp_config, "001", 3) is None        # 古い実行の記録だけでは始めない
    assert box["calls"] == calls                                # 追加の API 呼び出し 0


def test_c_new_regen_after_withdrawal_starts_fresh_instead_of_resuming(app, tmp_config):
    client, box = app
    old = _fail_partway(client, box, count=3, succeed=1)
    vr.set_verdict(tmp_config, "001", "v002", "rejected")      # 古い印を取り下げ
    vr.set_verdict(tmp_config, "001", "v001", "regen")         # 別の候補に新しく regen
    plan = _plan(client, 2)
    assert [(t["id"], t["count"], t["resume"]) for t in plan["targets"]] == [("001", 2, False)]
    calls = box["calls"]
    _run(client, count=2)
    assert box["calls"] == calls + 2                            # 新しい指示のぶん（2 枚）だけ
    record = _record(tmp_config, "001")
    assert "regen_run" not in record and record["regen_generated_at"] > old["since"]
    assert old["done"][0] in _ids(tmp_config, "001")           # 古い実行で作れた候補は残る


def test_old_marks_kept_and_a_new_mark_added_resumes_then_leaves_the_new_one(app, tmp_config):
    """古い印が残っていれば古い実行を続け、その後に付けた新しい印は次回の対象として残る。"""
    client, box = app
    _fail_partway(client, box, count=3, succeed=1)
    vr.set_verdict(tmp_config, "001", "v001", "regen")         # 新しい印（古い印も残っている）
    assert [(t["count"], t["resume"]) for t in _plan(client, 2)["targets"]] == [(2, True)]
    _run(client, count=2)
    assert [(t["count"], t["resume"]) for t in _plan(client, 2)["targets"]] == [(2, False)]


def test_existing_data_without_new_fields_still_works(app, tmp_config):
    client, box = app

    def strip(state):
        for record in state["stickers"].values():
            for key in ("regen_run", "regen_generated_at"):
                record.pop(key, None)
            for item in record["variants"]:
                item.pop("regen_marked_at", None)
    vr.update(tmp_config, strip)
    assert [t["id"] for t in _plan(client, 1)["targets"]] == ["001", "002"]
    _run(client, count=1)
    assert _plan(client, 1)["targets"] == []


# ===========================================================================
# API が画像を返した直後・記録する前に落ちた場合
# ===========================================================================
_CHILD = r"""
import io, os, sys
from pathlib import Path
import yaml
sys.path.insert(0, sys.argv[1])
from src import image_generator as ig, variants as vr
from src.config import Config
from src.csv_loader import StickerEntry
from src.image_generator import ImageGenerator
from src.logger import RunLogger, StateStore
from src.providers.base import ImageGenerationProvider
from tests.test_scoring import sticker_like
cfg_path, mode = Path(sys.argv[2]), sys.argv[3]
cfg = Config(raw=yaml.safe_load(cfg_path.read_text(encoding="utf-8")), root=cfg_path.parent.parent, path=cfg_path)
counter = cfg.root / "provider_calls.txt"

class Fake(ImageGenerationProvider):
    name = "fake"
    def generate(self, prompt, reference_image=None, output_path=None):
        n = int(counter.read_text()) + 1 if counter.exists() else 1
        counter.write_text(str(n))
        buf = io.BytesIO()
        sticker_like(body=(30 + n * 40 % 200, 90, 160, 255)).save(buf, "PNG")
        data = buf.getvalue()
        if mode == "half_file":                  # 画像を半分だけ書いたところで落ちる
            Path(output_path).write_bytes(data[: len(data) // 2])
            os._exit(9)
        self._write(data, output_path)
        if mode == "part_written":               # 一時ファイル（.part）に書き終えた直後に落ちる
            os._exit(9)
        return data
    def estimate_cost_usd(self, count):
        return 0.0

ig.create_provider = lambda config: Fake()
generator = ImageGenerator(cfg, RunLogger(cfg.log_path, echo=False), StateStore(cfg.state_path))
if mode == "after_response":                     # 画像ファイルができ、記録する直前に落ちる
    real = generator.generate_one
    def generate_one(entry, **kwargs):
        result = real(entry, **kwargs)
        os._exit(9)
    generator.generate_one = generate_one
if mode == "before_finish":                      # 最後の 1 枚を記録した後、締めの前に落ちる
    real_finish = vr.finish_regen
    vr.finish_regen = lambda *a, **k: os._exit(9)
entry = StickerEntry(id="001", text="了解！", action="敬礼", expression="笑顔", category="basic")
vr.run_regen(cfg, entry, int(sys.argv[4]), generator)
print("finished")
"""


def _crash(tmp_config, mode, count):
    vr.set_verdict(tmp_config, "002", "v001", "rejected")
    p = subprocess.run([sys.executable, "-c", _CHILD, str(PROJECT), str(tmp_config.path), mode, str(count)],
                       capture_output=True, text=True, encoding="utf-8",
                       env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(PROJECT)},
                       timeout=120)
    assert p.returncode == 9, p.stderr
    calls = tmp_config.root / "provider_calls.txt"
    return int(calls.read_text()) if calls.exists() else 0


def test_crash_after_the_api_response_does_not_request_the_same_candidate_again(app, tmp_config):
    client, box = app
    _generate_fields_before = _ids(tmp_config, "001")
    assert _crash(tmp_config, "after_response", count=2) == 1   # 1 回課金され、記録前に落ちた
    assert (vr.variant_dir(tmp_config, "001") / "v003.png").exists()
    assert _ids(tmp_config, "001") == _generate_fields_before  # 記録には無い

    plan = _plan(client, 2)
    assert [(t["id"], t["count"], t["resume"]) for t in plan["targets"]] == [("001", 1, True)]
    _run(client, count=2)
    assert box["calls"] == 1                                    # 残り 1 枚だけ頼む（v003 は頼まない）
    assert _ids(tmp_config, "001") == _generate_fields_before + ["v003", "v004"]
    assert _item(tmp_config, "001", "v003")["verdict"] == "pending"
    record = _record(tmp_config, "001")
    assert record["regen_generated_at"] and "regen_run" not in record


def test_crash_with_only_the_temporary_file_written_recovers_it(app, tmp_config):
    client, box = app
    assert _crash(tmp_config, "part_written", count=1) == 1
    part = vr.variant_dir(tmp_config, "001") / "v003.png.part"
    assert part.exists() and not part.with_suffix("").exists()
    assert _plan(client, 1)["total"] == 0                       # もう作る必要は無い
    assert box["calls"] == 0
    vr.set_verdict(tmp_config, "001", "v001", "regen")         # 新しい指示で締めと復元が行われる
    _run(client, count=1)
    assert box["calls"] == 1
    assert "v003" in _ids(tmp_config, "001") and not part.exists()


def test_a_broken_file_left_by_a_crash_is_not_registered(app, tmp_config):
    client, box = app
    assert _crash(tmp_config, "half_file", count=1) == 1
    broken = vr.variant_dir(tmp_config, "001") / "v003.png.part"
    assert broken.exists()
    plan = _plan(client, 1)
    assert [(t["id"], t["count"]) for t in plan["targets"]] == [("001", 1)]   # 壊れた分は作り直す
    _run(client, count=1)
    assert box["calls"] == 1
    assert "v003" not in _ids(tmp_config, "001")               # 壊れた画像は登録しない
    assert _ids(tmp_config, "001")[-1] == "v004"               # 番号は再利用しない


def test_crash_after_the_last_registration_is_completed_without_regenerating(app, tmp_config):
    client, box = app
    assert _crash(tmp_config, "before_finish", count=2) == 2
    record = _record(tmp_config, "001")
    assert record["regen_run"]["done"] == ["v003", "v004"] and "regen_generated_at" not in record
    assert _plan(client, 2)["targets"] == []                   # 作り終えているので対象外
    assert box["calls"] == 0
    vr.set_verdict(tmp_config, "001", "v001", "regen")         # 新しい指示は、新しい実行の対象になる
    assert [(t["count"], t["resume"]) for t in _plan(client, 2)["targets"]] == [(2, False)]
    _run(client, count=2)
    record = _record(tmp_config, "001")
    assert box["calls"] == 2 and "regen_run" not in record and record["regen_generated_at"]
