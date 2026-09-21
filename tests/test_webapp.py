"""Web GUI のテスト。画像生成APIは呼びません。"""

from __future__ import annotations

import io
import json

import pytest
from PIL import Image

from src import image_processor as ip
from src.csv_loader import load_stickers
from tests.conftest import make_character

flask = pytest.importorskip("flask", reason="Flask が未インストールです")


@pytest.fixture
def app(tmp_config):
    from src.webapp import create_app

    # CSV をテンポラリへコピーして編集テストが実CSVを壊さないようにします。
    csv_src = tmp_config.root / "data" / "stickers.csv"
    csv_src.parent.mkdir(parents=True, exist_ok=True)
    csv_src.write_text(
        "id,text,action,expression,category\n"
        "001,了解！,敬礼する,自信のある笑顔,basic\n"
        "002,ありがとう！,手を合わせる,嬉しそうな笑顔,thanks\n"
        "003,ちょっと何言ってるかわからない,首をかしげる,無表情の真顔,misc\n",
        encoding="utf-8",
    )
    tmp_config.raw["data"]["csv"] = "data/stickers.csv"

    application = create_app(tmp_config)
    application.config["TESTING"] = True
    return application


@pytest.fixture
def client(app):
    return app.test_client()


def _add_raw(cfg, sticker_id: str):
    ip.save_png(make_character((512, 512)), cfg.dir_generated / f"{sticker_id}.png")


# --- 基本 --------------------------------------------------------------
def test_index_serves_html(client):
    res = client.get("/")
    assert res.status_code == 200
    assert b"<title>" in res.data


def test_state_never_leaks_api_key(client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-super-secret-value")
    res = client.get("/api/state")
    assert res.status_code == 200
    body = res.get_data(as_text=True)
    assert "sk-super-secret-value" not in body
    data = json.loads(body)
    assert data["api_key_set"] is True
    assert "api_key" not in data


def test_state_lists_stickers(client):
    data = client.get("/api/state").get_json()
    assert len(data["stickers"]) == 3
    assert data["stickers"][0]["id"] == "001"
    assert data["stickers"][0]["has_raw"] is False
    assert data["line_spec"]["sticker"] == [370, 320]
    assert data["line_spec"]["valid_set_sizes"] == [8, 16, 24, 32, 40]


def test_cost_endpoint(client):
    data = client.get("/api/cost?count=100&quality=low").get_json()
    assert data["count"] == 100
    assert data["usd"] == pytest.approx(1.1, abs=0.01)


# --- 画像配信 ----------------------------------------------------------
def test_image_served_and_404(client, tmp_config):
    assert client.get("/img/generated/001.png").status_code == 404
    _add_raw(tmp_config, "001")
    res = client.get("/img/generated/001.png")
    assert res.status_code == 200
    assert res.mimetype == "image/png"


def test_image_path_traversal_blocked(client):
    for path in ("/img/final/..%2F..%2Fconfig%2Fsticker_config.yaml",
                 "/img/generated/....%2F%2Fmain.png"):
        assert client.get(path).status_code in (400, 404)


# --- CSV編集 -----------------------------------------------------------
def test_save_csv_and_backup(client, tmp_config):
    rows = [
        {"id": "001", "text": "了解！", "action": "敬礼", "expression": "笑顔", "category": "basic"},
        {"id": "002", "text": "新しいセリフ", "action": "手を振る", "expression": "笑顔", "category": "greeting"},
    ]
    res = client.post("/api/stickers", json={"stickers": rows})
    assert res.status_code == 200
    assert res.get_json()["count"] == 2

    entries = load_stickers(tmp_config.csv_path)
    assert [e.text for e in entries] == ["了解！", "新しいセリフ"]
    # 上書き前のバックアップが残ること
    assert tmp_config.csv_path.with_suffix(".csv.bak").exists()


def test_save_csv_rejects_duplicate_id(client):
    rows = [
        {"id": "001", "text": "A", "action": "", "expression": "", "category": "basic"},
        {"id": "001", "text": "B", "action": "", "expression": "", "category": "basic"},
    ]
    res = client.post("/api/stickers", json={"stickers": rows})
    assert res.status_code == 400
    assert "重複" in res.get_json()["error"]


def test_save_csv_rejects_empty_text(client):
    res = client.post(
        "/api/stickers",
        json={"stickers": [{"id": "001", "text": "  ", "action": "", "expression": "", "category": "basic"}]},
    )
    assert res.status_code == 400


# --- 文字デザイン ------------------------------------------------------
def test_preview_without_raw_or_master_is_404(client):
    """原画もマスター画像も無ければ、理由を添えて404を返します。"""
    res = client.post("/api/preview-text", json={"id": "001", "style": {}})
    assert res.status_code == 404
    assert "マスター画像" in res.get_json()["error"]


def test_preview_falls_back_to_master_image(client, tmp_config):
    """原画が無くても、マスター画像を代役にして文字デザインを確認できます。"""
    master = tmp_config.master_image_path
    master.parent.mkdir(parents=True, exist_ok=True)
    make_character((512, 512)).save(master)

    res = client.post("/api/preview-text", json={"id": "001", "style": {"size": 50}})
    assert res.status_code == 200
    assert res.headers["X-Preview-Source"] == "master"
    with Image.open(io.BytesIO(res.data)) as im:
        assert im.size == tuple(tmp_config.sticker_size)
    # 代用プレビューで完成画像を作ってしまわないこと
    assert not (tmp_config.dir_final / "001.png").exists()


def test_preview_prefers_raw_over_master(client, tmp_config):
    master = tmp_config.master_image_path
    master.parent.mkdir(parents=True, exist_ok=True)
    make_character((512, 512)).save(master)
    _add_raw(tmp_config, "001")

    res = client.post("/api/preview-text", json={"id": "001", "style": {}})
    assert res.status_code == 200
    assert res.headers["X-Preview-Source"] == "raw"


def test_preview_text_returns_png(client, tmp_config):
    _add_raw(tmp_config, "003")
    res = client.post(
        "/api/preview-text",
        json={"id": "003", "style": {"size": 48, "stroke_width": 5, "band_ratio": 0.35}},
    )
    assert res.status_code == 200
    with Image.open(io.BytesIO(res.data)) as im:
        assert im.size == tuple(tmp_config.sticker_size)
        assert im.mode == "RGBA"


def test_preview_does_not_write_final(client, tmp_config):
    _add_raw(tmp_config, "001")
    client.post("/api/preview-text", json={"id": "001", "style": {"size": 40}})
    assert not (tmp_config.dir_final / "001.png").exists()


def test_preview_unknown_id(client, tmp_config):
    _add_raw(tmp_config, "001")
    res = client.post("/api/preview-text", json={"id": "999"})
    assert res.status_code == 404
    assert "IDが見つかりません" in res.get_json()["error"]


def test_favicon_is_inlined(client):
    """/favicon.ico への無駄なリクエストが出ないよう data URI を埋め込みます。"""
    html = client.get("/").get_data(as_text=True)
    assert 'rel="icon"' in html
    assert "data:image/svg+xml" in html


def test_save_font_settings_writes_overrides(client, tmp_config):
    res = client.post("/api/settings/font", json={"size": 44, "stroke_width": 9, "fill": "#000000"})
    assert res.status_code == 200
    assert res.get_json()["font"]["size"] == 44

    overrides = tmp_config.overrides_path
    assert overrides.exists()
    text = overrides.read_text(encoding="utf-8")
    assert "44" in text

    # ベースの sticker_config.yaml は書き換えられていないこと（コメントを守るため）
    import yaml as _yaml

    base = _yaml.safe_load(tmp_config.path.read_text(encoding="utf-8"))
    assert base["font"]["size"] != 44

    # 読み直すと overrides が上書きとして効くこと
    from src.config import load_config as _load

    reloaded = _load(tmp_config.path, load_env=False)
    assert reloaded.get("font.size") == 44
    assert reloaded.get("font.stroke_width") == 9


def test_save_font_settings_rejects_bad_value(client):
    res = client.post("/api/settings/font", json={"size": "とても大きい"})
    assert res.status_code == 400


# --- 生成（APIキー無し / dry-run） -------------------------------------
def test_generate_without_api_key_is_rejected(client, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    res = client.post("/api/generate", json={"ids": ["001"]})
    assert res.status_code == 400
    assert "OPENAI_API_KEY" in res.get_json()["error"]


def test_generate_requires_ids(client):
    assert client.post("/api/generate", json={"ids": []}).status_code == 400


def test_dry_run_calls_no_api(client, tmp_config):
    res = client.post("/api/generate", json={"ids": ["001", "002"], "dry_run": True})
    assert res.status_code == 200

    job = _wait_for_job(client)
    assert job["status"] == "finished"
    assert job["api_calls"] == 0
    assert not list(tmp_config.dir_generated.glob("*.png"))


def _wait_for_job(client, timeout: float = 10.0) -> dict:
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get("/api/job").get_json()["job"]
        if job and job["status"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("ジョブが終了しませんでした")


def test_render_job_creates_final(client, tmp_config):
    _add_raw(tmp_config, "001")
    res = client.post("/api/render", json={"ids": ["001"]})
    assert res.status_code == 200
    job = _wait_for_job(client)
    assert job["status"] == "finished"
    assert (tmp_config.dir_final / "001.png").exists()


# --- 検証 / パッケージ / ギャラリー -------------------------------------
def test_validate_endpoint(client, tmp_config):
    _add_raw(tmp_config, "001")
    client.post("/api/render", json={"ids": ["001"]})
    _wait_for_job(client)

    data = client.post("/api/validate", json={}).get_json()
    assert data["checked"] >= 1
    assert data["errors"] == 0


def test_package_endpoint_and_download(client, tmp_config):
    for sid in ("001", "002", "003"):
        _add_raw(tmp_config, sid)
    client.post("/api/render", json={})
    _wait_for_job(client)

    data = client.post("/api/package", json={}).get_json()
    assert data["main"]["ok"] is True
    assert data["tab"]["ok"] is True
    # 3枚は 8 の倍数でないため未パッケージ警告が出る
    assert any(p["warnings"] for p in data["packages"])


def test_gallery_endpoint(client, tmp_config):
    data = client.post("/api/gallery", json={}).get_json()
    assert tmp_config.gallery_path.exists()
    assert data["path"].endswith("gallery.html")


def test_log_endpoint(client):
    assert "lines" in client.get("/api/log").get_json()


# --- 起動時の案内 -------------------------------------------------------
def test_banner_is_quiet_on_loopback():
    from src.webapp import startup_banner

    text = "\n".join(startup_banner("127.0.0.1", 8765))
    assert "http://127.0.0.1:8765/" in text
    assert "このPCからのみ接続できます" in text
    assert "警告" not in text
    # Flask 既定の紛らわしい定型警告は出しません
    assert "development server" not in text


def test_banner_warns_when_exposed():
    from src.webapp import startup_banner

    text = "\n".join(startup_banner("0.0.0.0", 8765))
    assert "警告" in text
    assert "課金" in text


def test_is_loopback():
    from src.webapp import is_loopback

    assert is_loopback("127.0.0.1")
    assert is_loopback("localhost")
    assert not is_loopback("0.0.0.0")
    assert not is_loopback("192.168.1.5")
