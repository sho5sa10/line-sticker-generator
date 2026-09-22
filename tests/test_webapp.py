"""Web GUI のテスト。画像生成APIは呼びません。"""

from __future__ import annotations

import io
import json
from pathlib import Path

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

    # 3枚では1セット（最低8枚）にならないので、理由を返して何も作らない
    res = client.post("/api/package", json={})
    assert res.status_code == 400
    assert "最低8枚" in res.get_json()["error"]
    assert "None" not in res.get_json()["error"]

    # 8枚そろえば main/tab/ZIP ができ、ダウンロードできる
    tmp_config.csv_path.write_text(
        "id,text,action,expression,category\n"
        + "".join(f"{i:03d},セリフ{i},ポーズ,表情,basic\n" for i in range(1, 9)),
        encoding="utf-8",
    )
    _finals(tmp_config, 8)
    data = client.post("/api/package", json={"set_size": 8}).get_json()
    assert data["main"]["ok"] is True
    assert data["tab"]["ok"] is True
    names = [p["name"] for p in data["packages"] if p["downloadable"]]
    assert names == ["line_stickers_001_008.zip"]
    assert client.get(f"/api/download/{names[0]}").status_code == 200


def test_gallery_endpoint(client, tmp_config):
    data = client.post("/api/gallery", json={}).get_json()
    assert tmp_config.gallery_path.exists()
    assert data["path"].endswith("gallery.html")


def test_log_endpoint(client):
    assert "lines" in client.get("/api/log").get_json()


# --- ステップ2: キャラクターマスター画像 --------------------------------
def _png_bytes(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_master_info_when_missing(client):
    d = client.get("/api/master").get_json()
    assert d["exists"] is False
    assert d["backups"] == []
    # 日本語版が未作成なら、初期値（日本語）を編集欄に出し、実際は英語版が使われる
    assert "黒髪" in d["prompt_ja"]
    assert d["prompt_mode"] == "en"
    assert "Japanese cute chibi office worker mascot." in d["full_prompt"]
    assert "Transparent background." in d["fixed_prompt"]


def test_master_upload_and_backup(client, tmp_config):
    first = _png_bytes(make_character((300, 300)))
    res = client.post(
        "/api/master/upload",
        data={"file": (io.BytesIO(first), "hero.png")},
        content_type="multipart/form-data",
    )
    assert res.status_code == 200
    assert res.get_json()["backup"] is None
    assert tmp_config.master_image_path.exists()

    # 2回目は前の画像が履歴として残ること（上書きで消さない）
    second = _png_bytes(make_character((320, 320), color=(10, 200, 90, 255)))
    res = client.post(
        "/api/master/upload",
        data={"file": (io.BytesIO(second), "hero2.png")},
        content_type="multipart/form-data",
    )
    assert res.status_code == 200
    backup = res.get_json()["backup"]
    assert backup and backup.startswith("character_master_")
    assert (tmp_config.master_image_path.parent / backup).exists()

    info = client.get("/api/master").get_json()
    assert info["exists"] is True
    assert backup in info["backups"]


def test_master_upload_rejects_non_image(client):
    res = client.post(
        "/api/master/upload",
        data={"file": (io.BytesIO(b"not an image at all"), "x.png")},
        content_type="multipart/form-data",
    )
    assert res.status_code == 400
    assert "画像として読み込めません" in res.get_json()["error"]


def test_master_upload_rejects_blank_image(client):
    blank = _png_bytes(Image.new("RGBA", (64, 64), (0, 0, 0, 0)))
    res = client.post(
        "/api/master/upload",
        data={"file": (io.BytesIO(blank), "blank.png")},
        content_type="multipart/form-data",
    )
    assert res.status_code == 400


def test_master_upload_requires_file(client):
    res = client.post("/api/master/upload", data={}, content_type="multipart/form-data")
    assert res.status_code == 400


def test_master_restore(client, tmp_config):
    for _ in range(2):
        client.post(
            "/api/master/upload",
            data={"file": (io.BytesIO(_png_bytes(make_character((300, 300)))), "a.png")},
            content_type="multipart/form-data",
        )
    backups = client.get("/api/master").get_json()["backups"]
    res = client.post("/api/master/restore", json={"name": backups[0]})
    assert res.status_code == 200
    assert tmp_config.master_image_path.exists()


def test_master_restore_rejects_traversal(client):
    for name in ("../../config/sticker_config.yaml", "character_master_../x.png", "nope.png"):
        assert client.post("/api/master/restore", json={"name": name}).status_code == 404


def test_master_prompt_save_japanese(client, tmp_config):
    res = client.post("/api/master/prompt", json={"prompt_ja": "20代の女性。\n茶色のボブヘア。"})
    assert res.status_code == 200
    d = res.get_json()
    assert d["prompt_mode"] == "ja"
    assert "茶色のボブヘア。" in tmp_config.master_prompt_ja_path.read_text(encoding="utf-8")
    # 実際に送る全文には、日本語の説明と固定の英語指示の両方が入る
    assert "茶色のボブヘア。" in d["full_prompt"]
    assert "Transparent background." in d["full_prompt"]
    # 英語版のファイルは作らない・書き換えない
    assert not tmp_config.master_prompt_path.exists()


def test_master_prompt_accepts_legacy_field(client, tmp_config):
    """画面とサーバーの版がずれていても保存できるよう、旧名 "prompt" も受け付けます。"""
    res = client.post("/api/master/prompt", json={"prompt": "短い黒髪。"})
    assert res.status_code == 200
    assert "短い黒髪。" in tmp_config.master_prompt_ja_path.read_text(encoding="utf-8")


def test_japanese_text_from_user_report_is_saved(client, tmp_config):
    """ユーザーが報告した入力そのもので保存できること（改行・全角記号を含む）。"""
    text = (
        "30代くらいの日本人男性の会社員。\n2〜2.5頭身のちびキャラ。\n短い黒髪。\n"
        "白いワイシャツ。\nシンプルな濃い色のネクタイ。\n少し丸みのある体型。\n"
        "親しみやすく、コミカルな雰囲気。\n配色はシンプルにする。"
    )
    res = client.post("/api/master/prompt", json={"prompt_ja": text})
    assert res.status_code == 200, res.get_json()
    assert tmp_config.master_prompt_ja_path.read_text(encoding="utf-8").strip() == text


def test_server_not_outdated_right_after_start(client):
    assert client.get("/api/server").get_json() == {"outdated": False}
    assert client.get("/api/state").get_json()["server_outdated"] is False


def test_server_outdated_after_code_change(tmp_config, monkeypatch):
    """起動後にプログラムが更新されたら、再起動が必要だと知らせます。"""
    from src import webapp

    app = webapp.create_app(tmp_config)
    client = app.test_client()
    real = webapp.code_fingerprint()
    monkeypatch.setattr(webapp, "code_fingerprint", lambda: real + 60)
    assert client.get("/api/server").get_json() == {"outdated": True}


# --- かんたん入力 ------------------------------------------------------
def test_profile_options_returned(client):
    d = client.get("/api/master/profile").get_json()
    assert {g["key"] for g in d["groups"]} >= {"kind", "gender", "hair", "outfit", "mood"}
    assert "会社員（男性）" in d["presets"]
    assert d["profile"] is None  # 何も保存されていない


def test_profile_inferred_from_bundled_text(client, tmp_config):
    """選択内容の保存が無くても、説明文がひな形と同じなら選択状態を復元します。"""
    from src.prompt_generator import DEFAULT_CHARACTER_JA

    tmp_config.master_prompt_ja_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_config.master_prompt_ja_path.write_text(DEFAULT_CHARACTER_JA + "\n", encoding="utf-8")
    d = client.get("/api/master/profile").get_json()
    assert d["profile"]["job"] == "会社員"
    assert d["text_matches_profile"] is True


def test_compose_endpoint(client):
    d = client.post("/api/master/compose", json={
        "profile": {"kind": "動物", "animal": "うさぎ", "hair": "ボブ", "palette": "パステル"},
    }).get_json()
    assert d["text"].splitlines()[0] == "うさぎのキャラクター。"
    assert "ボブ" not in d["text"]          # 動物には髪型を使わない
    assert "hair" not in d["profile"]


def test_save_prompt_with_profile(client, tmp_config):
    from src import character_profile as cp

    profile = cp.PRESETS["学生"]
    text = cp.compose(profile)
    res = client.post("/api/master/prompt", json={"prompt_ja": text, "profile": profile})
    assert res.status_code == 200
    assert cp.load_profile(tmp_config.character_profile_path) == profile

    d = client.get("/api/master/profile").get_json()
    assert d["profile"] == profile
    assert d["text_matches_profile"] is True


def test_manual_edit_is_detected(client, tmp_config):
    from src import character_profile as cp

    profile = cp.PRESETS["学生"]
    client.post("/api/master/prompt",
                json={"prompt_ja": cp.compose(profile) + "\n手で足した一文。", "profile": profile})
    d = client.get("/api/master/profile").get_json()
    assert d["profile"] == profile
    assert d["text_matches_profile"] is False  # 画面は「手で書き換えた」扱いにする


def test_master_prompt_rejects_empty(client):
    assert client.post("/api/master/prompt", json={"prompt_ja": "  "}).status_code == 400


def test_master_generate_without_key(client, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    res = client.post("/api/master/generate", json={})
    assert res.status_code == 400
    assert "OPENAI_API_KEY" in res.get_json()["error"]


# --- 退避（削除はしない） -----------------------------------------------
def test_archive_moves_files_without_deleting(client, tmp_config):
    _add_raw(tmp_config, "001")
    client.post("/api/render", json={"ids": ["001"]})
    _wait_for_job(client)
    assert (tmp_config.dir_final / "001.png").exists()

    d = client.post("/api/generated/archive", json={}).get_json()
    assert d["moved"] == {"generated": 1, "final": 1}
    assert not list(tmp_config.dir_generated.glob("*.png"))
    assert not list(tmp_config.dir_final.glob("*.png"))

    archived = Path(d["archived_to"])
    assert (archived / "generated" / "001.png").exists()
    assert (archived / "final" / "001.png").exists()


def test_archive_without_files(client):
    assert client.post("/api/generated/archive", json={}).status_code == 400


# --- 手持ち画像の取り込み -----------------------------------------------
def test_upload_single_sticker(client, tmp_config):
    res = client.post(
        "/api/stickers/002/upload",
        data={"file": (io.BytesIO(_png_bytes(make_character())), "whatever.png")},
        content_type="multipart/form-data",
    )
    assert res.status_code == 200
    d = res.get_json()
    assert d["ok"] is True
    assert d["sticker"]["has_final"] is True
    assert (tmp_config.dir_final / "002.png").exists()


def test_upload_works_without_api_key(client, monkeypatch, tmp_config):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    res = client.post(
        "/api/stickers/001/upload",
        data={"file": (io.BytesIO(_png_bytes(make_character())), "a.png")},
        content_type="multipart/form-data",
    )
    assert res.status_code == 200
    assert res.get_json()["ok"] is True


def test_upload_unknown_id(client):
    res = client.post(
        "/api/stickers/999/upload",
        data={"file": (io.BytesIO(_png_bytes(make_character())), "a.png")},
        content_type="multipart/form-data",
    )
    assert res.status_code == 404


def test_upload_broken_file(client, tmp_config):
    res = client.post(
        "/api/stickers/001/upload",
        data={"file": (io.BytesIO(b"nope"), "a.png")},
        content_type="multipart/form-data",
    )
    assert res.status_code == 400
    assert not (tmp_config.dir_generated / "001.png").exists()


def test_bulk_import_maps_filenames_to_ids(client, tmp_config):
    data = {
        "files": [
            (io.BytesIO(_png_bytes(make_character())), "001.png"),
            (io.BytesIO(_png_bytes(make_character())), "sticker_3.png"),
            (io.BytesIO(_png_bytes(make_character())), "no_number.png"),
            (io.BytesIO(_png_bytes(make_character())), "050.png"),  # CSVに無いID
        ]
    }
    res = client.post("/api/stickers/import", data=data, content_type="multipart/form-data")
    assert res.status_code == 200
    d = res.get_json()
    assert d["imported"] == 2
    assert sorted(r["id"] for r in d["results"]) == ["001", "003"]
    assert len(d["skipped"]) == 2
    assert (tmp_config.dir_final / "001.png").exists()
    assert (tmp_config.dir_final / "003.png").exists()


def test_bulk_import_requires_files(client):
    res = client.post("/api/stickers/import", data={}, content_type="multipart/form-data")
    assert res.status_code == 400


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


def test_profile_returns_preset_categories(client):
    d = client.get("/api/master/profile").get_json()
    assert d["preset_categories"] == ["人", "動物", "そのほか"]
    assert d["preset_info"]["ハシビロコウ"]["category"] == "動物"
    assert "ランキング" in d["preset_info"]["ふわふわ子ねこ"]["note"]
    # 表示順は定義順（JSONのキー並べ替えの影響を受けない）
    assert d["preset_order"][0] == "会社員（男性）"


# --- 画面の表示・キャッシュ ---------------------------------------------
def test_css_hidden_attribute_always_wins():
    """hidden を付けた要素は、個別の display 指定より優先して必ず隠す。

    以前は .assets img { display: block } が hidden を打ち消し、
    まだ無い main/tab 画像が壊れた画像として表示されていました。
    """
    from src.webapp import WEB_DIR

    css = (WEB_DIR / "static" / "style.css").read_text(encoding="utf-8")
    assert "[hidden] { display: none !important; }" in css


def test_assets_are_revalidated(client):
    """画面のファイルは毎回サーバーに確認させ、古いCSS/JSが使われないようにする。"""
    for path in ("/", "/static/style.css", "/static/app.js"):
        res = client.get(path)
        assert res.status_code == 200, path
        assert res.headers.get("Cache-Control") == "no-cache", path


# --- セットの作り方 ------------------------------------------------------
def _finals(tmp_config, n):
    for i in range(1, n + 1):
        _add_raw(tmp_config, f"{i:03d}")
        ip.save_png(make_character((370, 320)), tmp_config.dir_final / f"{i:03d}.png")


def test_package_plan_endpoint(client, tmp_config):
    _finals(tmp_config, 3)
    d = client.post("/api/package/plan", json={"set_size": None}).get_json()
    assert d["sets"] == []            # 3枚では8枚セットも作れない
    assert d["leftover"] == ["001", "002", "003"]

    d = client.post("/api/package/plan", json={"ids": ["001", "002"]}).get_json()
    assert "あと6枚選ぶと8枚セット" in d["error"]
    # 見込みを出すだけで、ファイルは作らない
    assert not list(tmp_config.dir_packages.glob("*.zip"))
    assert not (tmp_config.dir_main / "main.png").exists()


def test_package_invalid_selection_is_rejected_without_side_effects(client, tmp_config):
    _finals(tmp_config, 3)
    res = client.post("/api/package", json={"ids": ["001", "002", "003"]})
    assert res.status_code == 400
    assert "あと5枚選ぶと8枚セット" in res.get_json()["error"]
    assert not (tmp_config.dir_main / "main.png").exists()


# --- フォント・おまかせ・セリフの改行 ------------------------------------
def test_fonts_endpoint(client):
    d = client.get("/api/fonts").get_json()
    assert any(f["id"] == "biz-ud-gothic" for f in d["fonts"])


def test_preview_with_font_id(client, tmp_config):
    _add_raw(tmp_config, "001")
    ok = client.post("/api/preview-text", json={"id": "001", "style": {"font_id": "biz-ud-gothic"}})
    assert ok.status_code == 200
    bad = client.post("/api/preview-text", json={"id": "001", "style": {"font_id": "nope"}})
    assert bad.status_code == 400


def test_preview_ignores_raw_font_path(client, tmp_config):
    """画面から任意のファイルパスを渡されても使わない。"""
    _add_raw(tmp_config, "001")
    res = client.post("/api/preview-text",
                      json={"id": "001", "style": {"font_path": "C:/Windows/win.ini"}})
    assert res.status_code == 200


def test_save_font_by_id(client, tmp_config):
    res = client.post("/api/settings/font", json={"font_id": "biz-ud-gothic", "size": 50})
    assert res.status_code == 200
    font = res.get_json()["font"]
    assert font["path"].endswith("BIZ-UDGothicB.ttc")
    assert client.get("/api/state").get_json()["font"]["font_id"] == "biz-ud-gothic"


def test_design_suggest_endpoint(client, tmp_config):
    tmp_config.master_image_path.parent.mkdir(parents=True, exist_ok=True)
    make_character((300, 300), color=(255, 215, 67, 255)).save(tmp_config.master_image_path)
    d = client.get("/api/design/suggest").get_json()
    assert d["suggestions"]
    assert all(s["fill"].startswith("#") for s in d["suggestions"])


def test_patch_sticker_text_keeps_line_break(client, tmp_config):
    res = client.patch("/api/stickers/002", json={"text": "ありがとう\r\nございます"})
    assert res.status_code == 200
    entries = {e.id: e for e in load_stickers(tmp_config.csv_path)}
    assert entries["002"].text == "ありがとう\nございます"
    assert entries["001"].text == "了解！"  # ほかの行は変わらない
    assert client.patch("/api/stickers/999", json={"text": "x"}).status_code == 404
    assert client.patch("/api/stickers/001", json={"text": "  "}).status_code == 400


def test_font_store_and_install(client, tmp_config, font_path, monkeypatch):
    d = client.get("/api/fonts").get_json()
    item = next(f for f in d["free_fonts"] if f["id"] == "hachi-maru-pop")
    assert item["installed"] is False and item["category"] == "手書き"

    import httpx

    data = open(font_path, "rb").read()

    class Res:
        def __init__(self, content):
            self.content = content

        def raise_for_status(self):
            pass

    monkeypatch.setattr(httpx, "get", lambda url, **kw: Res(b"OFL" if url.endswith("OFL.txt") else data))
    res = client.post("/api/fonts/install", json={"id": "hachi-maru-pop"})
    assert res.status_code == 200
    d = client.get("/api/fonts").get_json()
    assert any(f["id"] == "hachi-maru-pop" for f in d["fonts"])
    assert next(f for f in d["free_fonts"] if f["id"] == "hachi-maru-pop")["installed"] is True

    assert client.post("/api/fonts/install", json={"id": "nope"}).status_code == 400


def test_font_check_endpoint(client):
    d = client.get("/api/fonts/check?font_id=biz-ud-gothic").get_json()
    assert d["missing"] == [] and d["affected_ids"] == []
    assert client.get("/api/fonts/check?font_id=nope").status_code == 404


# --- 申請用のタイトル・説明文 ---------------------------------------------
def test_listing_suggest_save_and_check(client):
    r = client.post("/api/listing/suggest", json={"creator": "yourname"})
    assert r.status_code == 200
    d = r.get_json()
    assert d["count"] == 3 and d["candidates"]
    first = d["candidates"][0]
    assert first["title_en"] and first["copyright"].endswith("yourname")

    r = client.post("/api/listing/suggest", json={"ids": ["002"]})
    assert r.get_json()["count"] == 1

    r = client.put("/api/listing", json={"listing": {**first, "title_en": "x" * 50}})
    d = r.get_json()
    assert any("長すぎ" in i for i in d["issues"]["title_en"])
    assert client.get("/api/listing").get_json()["listing"]["creator"] == "yourname"

    r = client.post("/api/listing/check", json={"listing": {"title_ja": "LINEのねこ"}})
    assert r.get_json()["issues"]["title_ja"]


# --- 販売状況 ------------------------------------------------------------
def test_sales_status_roundtrip(client):
    stickers = client.get("/api/state").get_json()["stickers"]
    assert all(s["sale"] == "" for s in stickers)
    assert {s["category"] for s in stickers} == {"basic", "thanks", "misc"}

    r = client.post("/api/sales", json={"ids": ["001", "003"], "status": "selling"})
    assert r.status_code == 200 and r.get_json()["sales"] == {"001": "selling", "003": "selling"}
    sale = {s["id"]: s["sale"] for s in client.get("/api/state").get_json()["stickers"]}
    assert sale == {"001": "selling", "002": "", "003": "selling"}

    assert client.post("/api/sales", json={"ids": ["001"], "status": ""}).get_json()["sales"] == {"003": "selling"}
    assert client.post("/api/sales", json={"ids": [], "status": "selling"}).status_code == 400
    assert client.post("/api/sales", json={"ids": ["001"], "status": "bogus"}).status_code == 400


# --- 検証結果は再起動しても残る ---------------------------------------------
def test_validation_result_survives_restart(app, tmp_config):
    import os

    from src.webapp import create_app

    client = app.test_client()
    assert client.get("/api/state").get_json()["validation"]["passed"] is False

    ip.save_png(make_character((370, 320)), tmp_config.dir_final / "001.png")
    d = client.post("/api/validate").get_json()
    v = d["validation"]
    assert v["checked"] == 1 and v["at"]
    assert v["passed"] is (d["errors"] == 0)

    # サーバーを作り直しても（=GUIの再起動）結果が残る
    again = create_app(tmp_config).test_client().get("/api/state").get_json()["validation"]
    assert again["at"] == v["at"] and again["passed"] == v["passed"] and not again["stale"]

    # 検証後に画像が変わったら「検証済み」は外れる
    final = tmp_config.dir_final / "001.png"
    st = final.stat()
    os.utime(final, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    after = client.get("/api/stickers").get_json()["validation"]
    assert after["stale"] is True and after["passed"] is False


# --- 作り直し後の古いZIPは「完了」にしない ---------------------------------
def test_packages_become_stale_when_images_change(client, tmp_config):
    import os

    st = lambda: client.get("/api/state").get_json()["packages_status"]  # noqa: E731
    assert st() == {"count": 0, "latest": 0, "stale": False}

    ip.save_png(make_character((370, 320)), tmp_config.dir_final / "001.png")
    zp = tmp_config.dir_packages / "line_stickers_001_008.zip"
    zp.parent.mkdir(parents=True, exist_ok=True)
    zp.write_bytes(b"PK")
    final = tmp_config.dir_final / "001.png"
    t = final.stat().st_mtime
    os.utime(zp, (t + 10, t + 10))
    assert st()["stale"] is False and st()["count"] == 1

    # 完成画像がZIPより新しくなった（作り直した）
    os.utime(final, (t + 20, t + 20))
    assert st()["stale"] is True
    assert client.get("/api/stickers").get_json()["packages_status"]["stale"] is True

    # 完成画像を退避して1枚も無い
    os.utime(final, (t, t))
    final.unlink()
    assert st()["stale"] is True
