"""進捗の記録（StateStore.record）の保存が失敗しても、生成・仕上げ・取り込みを止めないこと（Phase 6-C N-1）。

state.json は進捗の記録で、生成済みかどうかは画像ファイルで判断しています。record() は保存先の I/O の失敗
（OSError）だけを捕まえて警告し、False を返します。set() はこれまでどおり例外を呼び出し元へ返します。
保存の失敗は、StateStore の保存だけに起こします（画像の置き換えまで失敗させないため）。
"""

from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path

import pytest

from src import image_generator as ig
from src import logger as lg
from src import main as cli
from src import pipeline
from src import validator as vd
from src.logger import StateStore
from src.providers.base import ProviderError, RetryableProviderError
from tests.conftest import make_character
from tests.test_candidate_numbering import FakeProvider, _entry, _generator
from tests.test_stability import GUI_HEADERS, web  # noqa: F401 - web はフィクスチャ

GOOD = {"stickers": {"001": {"status": "error", "detail": "前回の記録", "updated_at": "x"}}}


def _write_state(config, data=GOOD) -> bytes:
    config.state_path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    config.state_path.write_bytes(raw)
    return raw


def _leftovers(path) -> list[str]:
    return sorted(p.name for p in path.parent.iterdir()
                  if p.is_file() and p.name.startswith(f".{path.name}.") and p.name.endswith(".tmp"))


def _fail_saves(monkeypatch, until_recorded: str | None = None):
    """StateStore の保存だけを PermissionError にします（until_recorded を記録したあとの保存は成功）。"""
    real_save = StateStore.save
    calls = []

    def save(self):
        calls.append(1)
        if until_recorded is None or until_recorded not in self.data["stickers"]:
            raise PermissionError("state.json is held by another process")
        return real_save(self)

    monkeypatch.setattr(StateStore, "save", save)
    return calls


class _Issue:
    severity = "ERROR"
    message = "テスト用の検証エラー"


class _NG:
    """検証に失敗した結果（validator.validate_sticker の戻り値と同じ形）。"""
    ok = False
    errors = [_Issue()]
    issues = [_Issue()]
    warnings: list = []


def _fail_validation_for(monkeypatch, sticker_id: str) -> None:
    real = vd.validate_sticker

    def validate(path, config):
        return _NG() if Path(path).stem == sticker_id else real(path, config)

    monkeypatch.setattr(vd, "validate_sticker", validate)


def _stickers(config) -> dict:
    return json.loads(config.state_path.read_text(encoding="utf-8"))["stickers"]


# ===========================================================================
# record() そのもの
# ===========================================================================
def test_record_saves_like_set(tmp_config):
    store = StateStore(tmp_config.state_path)
    assert store.record("001", "generated") is True
    assert store.record("002", "error", "理由") is True
    by_record = json.loads(tmp_config.state_path.read_text(encoding="utf-8"))["stickers"]
    assert by_record["001"]["status"] == "generated" and by_record["001"]["detail"] == ""   # detail の既定は ""
    assert by_record["002"] == {"status": "error", "detail": "理由", "updated_at": by_record["002"]["updated_at"]}

    other = tmp_config.state_path.with_name("state_set.json")
    by_set = StateStore(other)
    by_set.set("001", "generated")
    assert set(json.loads(other.read_text(encoding="utf-8"))["stickers"]["001"]) == set(by_record["001"])
    assert tmp_config.state_path.read_text(encoding="utf-8") == json.dumps(store.data, ensure_ascii=False, indent=2)


def test_record_returns_false_when_saving_fails(tmp_config, monkeypatch, capsys):
    old = _write_state(tmp_config)
    store = StateStore(tmp_config.state_path)

    def failing_replace(src, dst):
        raise OSError("disk error")

    monkeypatch.setattr(os, "replace", failing_replace)
    assert store.record("001", "generated") is False
    assert store.status("001") == "generated"                        # メモリは更新済み（次の保存で書かれる）
    assert "WARNING" in capsys.readouterr().err
    assert tmp_config.state_path.read_bytes() == old                 # 前の state.json は変わらない
    assert _leftovers(tmp_config.state_path) == []


def test_record_keeps_the_permission_retry_then_returns_false(tmp_config, monkeypatch, capsys):
    """Phase 6-A の再試行（PermissionError のときだけ、決まった回数）は、record() を通しても同じ。"""
    old = _write_state(tmp_config)
    store = StateStore(tmp_config.state_path)
    calls, sleeps = [], []

    def always_busy(src, dst):
        calls.append(1)
        raise PermissionError("in use")

    monkeypatch.setattr(os, "replace", always_busy)
    monkeypatch.setattr(lg.time, "sleep", sleeps.append)
    assert store.record("001", "generated") is False
    assert len(calls) == len(lg._REPLACE_RETRY_DELAYS) + 1 and sleeps == list(lg._REPLACE_RETRY_DELAYS)
    assert "WARNING" in capsys.readouterr().err
    assert tmp_config.state_path.read_bytes() == old
    assert _leftovers(tmp_config.state_path) == []


@pytest.mark.parametrize("error", [ValueError, TypeError, RuntimeError, KeyError])
def test_record_does_not_swallow_other_errors(tmp_config, monkeypatch, error):
    store = StateStore(tmp_config.state_path)

    def broken_save(self):
        raise error("programming error")

    monkeypatch.setattr(StateStore, "save", broken_save)
    with pytest.raises(error):
        store.record("001", "generated")


def test_set_still_raises(tmp_config, monkeypatch):
    """set() の約束（保存の失敗は呼び出し元へ）は変わらない。"""
    store = StateStore(tmp_config.state_path)
    _fail_saves(monkeypatch)
    with pytest.raises(PermissionError):
        store.set("001", "generated")


# ===========================================================================
# 生成（generate_one）: API の後の記録の保存が失敗しても、生成は成功
# ===========================================================================
def test_generate_one_succeeds_when_only_the_state_save_fails(tmp_config, monkeypatch, capsys):
    provider = FakeProvider()
    gen = _generator(tmp_config, provider)
    _fail_saves(monkeypatch)
    result = gen.generate_one(_entry())                              # 例外が外に出ない
    assert provider.calls == 1 and result.status == "generated"
    assert (tmp_config.dir_generated / "001.png").exists()
    assert "WARNING" in capsys.readouterr().err


@pytest.mark.parametrize("error", [ProviderError, RetryableProviderError], ids=["provider-error", "retryable"])
def test_generate_one_api_failure_with_a_state_save_failure(tmp_config, monkeypatch, capsys, error):
    """API の失敗と、その error の記録の保存の失敗が重なっても、API の失敗として返る（保存の失敗は二次例外にならない）。"""
    class FailingProvider(FakeProvider):
        def generate(self, prompt, reference_image=None, output_path=None):
            self.calls += 1
            raise error("偽の API の失敗")

    provider = FailingProvider()
    gen = _generator(tmp_config, provider)
    monkeypatch.setattr(ig.time, "sleep", lambda seconds: None)
    _fail_saves(monkeypatch)
    result = gen.generate_one(_entry())                              # 例外が外に出ない
    assert result.status == "error" and "偽の API の失敗" in result.detail
    retries = int(tmp_config.get("generation.retries", 3))
    assert provider.calls == (retries if error is RetryableProviderError else 1)   # 既存の再試行の仕様どおり
    assert not (tmp_config.dir_generated / "001.png").exists()      # 成功と取り違えない
    assert "WARNING" in capsys.readouterr().err


# ===========================================================================
# 一括生成（GUI のジョブ・CLI）: 1件目の記録の保存が失敗しても、2件目へ進む
# ===========================================================================
TWO_ENTRIES = ("id,text,action,expression,category\n"
               "001,了解！,敬礼,笑顔,basic\n002,ありがとう！,手を合わせる,笑顔,thanks\n")


def _two_entry_project(config, monkeypatch) -> FakeProvider:
    csv_path = config.root / "data" / "stickers.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text(TWO_ENTRIES, encoding="utf-8")
    config.raw["data"]["csv"] = "data/stickers.csv"
    import yaml
    config.path.write_text(yaml.safe_dump(config.raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    config.master_image_path.parent.mkdir(parents=True, exist_ok=True)
    make_character((256, 256)).save(config.master_image_path)
    monkeypatch.setenv("OPENAI_API_KEY", "test-dummy-not-used")
    provider = FakeProvider()
    monkeypatch.setattr(ig, "create_provider", lambda cfg: provider)
    return provider


def _completed(job: dict, sticker_id: str) -> bool:
    """ジョブのログに、そのスタンプの完成（仕上げと検証の成功「完了 (…KB)」）があるか。"""
    return any(e["id"] == sticker_id and e["level"] == "ok" and e["message"].startswith("完了")
               for e in job["events"])


def _wait_for_job(client, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get("/api/job", headers=GUI_HEADERS).get_json()["job"]
        if job and job["status"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("ジョブが終了しませんでした")


@pytest.fixture
def gui(tmp_config, monkeypatch):
    provider = _two_entry_project(tmp_config, monkeypatch)
    from src.webapp import create_app

    app = create_app(tmp_config)
    app.config["TESTING"] = True
    return app.test_client(), provider


def test_gui_generate_job_continues_after_a_state_save_failure(gui, tmp_config, monkeypatch):
    client, provider = gui
    _fail_saves(monkeypatch, until_recorded="002")                   # 001 の記録だけ保存に失敗
    r = client.post("/api/generate", json={"ids": ["001", "002"]}, headers=GUI_HEADERS)
    assert r.status_code == 200, r.get_json()
    job = _wait_for_job(client)
    assert job["status"] == "finished"                               # ジョブ全体は失敗にならない
    assert job["api_calls"] == 2 and provider.calls == 2
    for sid in ("001", "002"):
        assert (tmp_config.dir_generated / f"{sid}.png").exists()
        assert (tmp_config.dir_final / f"{sid}.png").exists()
    # 001 は保存に失敗しても complete のまま（error に変わらない）。記録はメモリに残り、002 の保存で一緒に書かれる
    stickers = _stickers(tmp_config)
    assert stickers["001"]["status"] == "complete" and stickers["002"]["status"] == "complete"
    assert not [e for e in job["events"] if e["id"] == "001" and e["level"] == "error"]
    assert _completed(job, "001") and _completed(job, "002")


def test_gui_validation_failure_with_a_state_save_failure(gui, tmp_config, monkeypatch):
    """検証の失敗と、その記録の保存の失敗が重なっても、検証の失敗として扱い（complete にしない）、次へ進む。"""
    client, provider = gui
    _fail_validation_for(monkeypatch, "001")
    _fail_saves(monkeypatch, until_recorded="002")
    r = client.post("/api/generate", json={"ids": ["001", "002"]}, headers=GUI_HEADERS)
    assert r.status_code == 200, r.get_json()
    job = _wait_for_job(client)
    assert job["status"] == "finished" and provider.calls == 2
    events = job["events"]
    assert not [e for e in events if "PermissionError" in e["message"]]   # 保存の失敗は二次例外にならない
    assert not _completed(job, "001") and _completed(job, "002")            # 001 を完成扱いにしない
    assert ("error", "001") in [(e["level"], e["id"]) for e in events]      # 検証のエラーとして残る
    stickers = _stickers(tmp_config)
    assert stickers["001"]["status"] == "validation_failed" and stickers["002"]["status"] == "complete"


def test_gui_render_error_with_a_state_save_failure_moves_on(gui, tmp_config, monkeypatch):
    """仕上げが失敗し、その error の記録の保存も失敗しても、例外が二重に漏れず、次のスタンプへ進む。"""
    client, provider = gui
    real_render = pipeline.render_final

    def render(config, entry, style):
        if entry.id == "001":
            raise RuntimeError("render failed")
        return real_render(config, entry, style)

    monkeypatch.setattr(pipeline, "render_final", render)
    _fail_saves(monkeypatch, until_recorded="002")
    r = client.post("/api/generate", json={"ids": ["001", "002"]}, headers=GUI_HEADERS)
    assert r.status_code == 200, r.get_json()
    job = _wait_for_job(client)
    assert job["status"] == "finished" and provider.calls == 2
    logs = [(e["level"], e.get("id")) for e in job["events"]]
    assert ("error", "001") in logs                                  # 本来の仕上げのエラーは、エラーとして残る
    assert (tmp_config.dir_final / "002.png").exists()               # 002 は仕上げまで進んだ
    stickers = json.loads(tmp_config.state_path.read_text(encoding="utf-8"))["stickers"]
    assert stickers["001"]["status"] == "error" and stickers["002"]["status"] == "complete"


def test_cli_generate_continues_after_a_state_save_failure(tmp_config, monkeypatch):
    provider = _two_entry_project(tmp_config, monkeypatch)
    _fail_saves(monkeypatch)                                         # すべての記録の保存が失敗
    code = cli.main(["--config", str(tmp_config.path), "generate", "--yes"])
    assert code == cli.EXIT_OK                                       # 保存の失敗は「生成の失敗」に数えない
    assert provider.calls == 2
    for sid in ("001", "002"):
        assert (tmp_config.dir_generated / f"{sid}.png").exists()
        assert (tmp_config.dir_final / f"{sid}.png").exists()


def test_cli_validation_failure_with_a_state_save_failure(tmp_config, monkeypatch, capsys):
    """検証の失敗と、その記録の保存の失敗が重なっても、検証の失敗として数え（error にしない）、002 へ進む。"""
    provider = _two_entry_project(tmp_config, monkeypatch)
    _fail_validation_for(monkeypatch, "001")
    _fail_saves(monkeypatch, until_recorded="002")
    code = cli.main(["--config", str(tmp_config.path), "generate", "--yes"])
    out = capsys.readouterr().out
    assert code == cli.EXIT_ERROR                                    # 検証の失敗は、これまでどおり失敗
    assert provider.calls == 2
    assert "生成 2 / スキップ 0 / 失敗 0" in out and "失敗したID: 001" in out   # error としては数えない
    assert (tmp_config.dir_final / "002.png").exists()
    stickers = _stickers(tmp_config)
    assert stickers["001"]["status"] == "validation_failed" and stickers["002"]["status"] == "complete"
    assert "001 ERROR" not in tmp_config.log_path.read_text(encoding="utf-8")


def test_cli_render_failure_with_a_state_save_failure(tmp_config, monkeypatch, capsys):
    """仕上げの失敗と、その error の記録の保存の失敗が重なっても、CLI は止まらず、001 は error、002 は complete。"""
    provider = _two_entry_project(tmp_config, monkeypatch)
    real_render = cli.render_final

    def render(config, entry, style):
        if entry.id == "001":
            raise RuntimeError("render failed")
        return real_render(config, entry, style)

    monkeypatch.setattr(cli, "render_final", render)
    _fail_saves(monkeypatch, until_recorded="002")
    code = cli.main(["--config", str(tmp_config.path), "generate", "--yes"])   # 例外が外に出ない
    out = capsys.readouterr().out
    assert code == cli.EXIT_ERROR and provider.calls == 2
    assert "失敗 1" in out and "失敗したID: 001" in out                # 仕上げの失敗は隠れない
    assert not (tmp_config.dir_final / "001.png").exists() and (tmp_config.dir_final / "002.png").exists()
    stickers = _stickers(tmp_config)
    assert stickers["001"]["status"] == "error" and stickers["002"]["status"] == "complete"


# ===========================================================================
# 取り込み: 記録の保存が失敗しても、取り込みは成功
# ===========================================================================
def test_upload_succeeds_when_only_the_state_save_fails(web, tmp_config, monkeypatch):  # noqa: F811
    _fail_saves(monkeypatch)
    buf = io.BytesIO()
    make_character((512, 512)).save(buf, format="PNG")
    r = web.post("/api/stickers/001/upload", headers=GUI_HEADERS,
                 data={"file": (io.BytesIO(buf.getvalue()), "001.png")},
                 content_type="multipart/form-data")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert (tmp_config.dir_generated / "001.png").exists() and (tmp_config.dir_final / "001.png").exists()
