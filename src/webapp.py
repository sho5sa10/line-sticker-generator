"""ローカル Web GUI（Flask）。

`python -m src.main gui` で 127.0.0.1 にのみバインドして起動します。
外部へは公開しません。APIキーの値はレスポンスに一切含めません。

CLI と同じパイプライン（pipeline / image_generator / validator / package_builder）を
呼び出すだけなので、GUI と CLI の結果は常に一致します。
"""

from __future__ import annotations

import io
import os
import threading
import traceback
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_file, send_from_directory

from . import gallery as gallery_mod
from . import image_processor as ip
from . import package_builder as pkg
from . import pipeline
from . import validator as vd
from .config import ConfigError, load_config
from .csv_loader import CsvLoadError, StickerEntry, load_stickers, save_stickers
from .image_generator import ImageGenerator, MasterImageMissingError, check_master_image
from .logger import RunLogger, StateStore
from .providers import estimate_cost_usd
from .text_renderer import FontNotFoundError, TextStyle

WEB_DIR = Path(__file__).resolve().parent / "web"


# ======================================================================
# ジョブ管理（同時に1件だけ実行）
# ======================================================================
@dataclass
class Job:
    """バックグラウンドで走る処理1件分の状態。"""

    id: str
    kind: str                      # generate | render | package
    total: int = 0
    done: int = 0
    status: str = "running"        # running | finished | failed | cancelled
    events: list[dict] = field(default_factory=list)
    result: dict = field(default_factory=dict)
    api_calls: int = 0
    started_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def log(self, level: str, message: str, sticker_id: str = "") -> None:
        self.events.append(
            {
                "time": datetime.now().strftime("%H:%M:%S"),
                "level": level,
                "id": sticker_id,
                "message": message,
            }
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "total": self.total,
            "done": self.done,
            "status": self.status,
            "events": self.events,
            "result": self.result,
            "api_calls": self.api_calls,
            "started_at": self.started_at,
        }


class JobManager:
    """実行中ジョブを1件に制限し、進捗とキャンセルを管理します。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._job: Job | None = None
        self._cancel = threading.Event()

    @property
    def current(self) -> Job | None:
        return self._job

    def is_running(self) -> bool:
        return self._job is not None and self._job.status == "running"

    def cancel(self) -> bool:
        if not self.is_running():
            return False
        self._cancel.set()
        return True

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def start(self, kind: str, total: int, target, *args) -> Job:
        with self._lock:
            if self.is_running():
                raise RuntimeError("すでに処理が実行中です。完了を待つか中止してください。")
            self._cancel.clear()
            job = Job(id=datetime.now().strftime("%Y%m%d%H%M%S%f"), kind=kind, total=total)
            self._job = job

        def runner() -> None:
            try:
                target(job, *args)
                if job.status == "running":
                    job.status = "cancelled" if self.cancelled() else "finished"
            except Exception as exc:  # noqa: BLE001 - GUIへ必ず理由を返す
                job.status = "failed"
                job.log("error", f"{type(exc).__name__}: {exc}")
                job.result["traceback"] = traceback.format_exc(limit=5)

        threading.Thread(target=runner, daemon=True, name=f"job-{kind}").start()
        return job


# ======================================================================
# アプリ本体
# ======================================================================
def create_app(config=None) -> Flask:
    """Flask アプリを生成します（テストからも呼べます）。"""
    cfg = config or load_config()
    cfg.ensure_output_dirs()

    app = Flask(
        __name__,
        template_folder=str(WEB_DIR / "templates"),
        static_folder=str(WEB_DIR / "static"),
        static_url_path="/static",
    )
    app.config["STICKER_CONFIG"] = cfg
    jobs = JobManager()
    app.config["JOBS"] = jobs

    # ------------------------------------------------------------------
    # ヘルパ
    # ------------------------------------------------------------------
    def current_config():
        return app.config["STICKER_CONFIG"]

    def reload_config():
        """overrides.yaml を書き換えた後などに設定を読み直します。

        設定ファイルが読めない場合は、save_overrides がメモリ上の設定にも
        反映済みなので、現在の設定をそのまま使い続けます。
        """
        old = current_config()
        try:
            new_cfg = load_config(old.path)
        except ConfigError:
            return old
        new_cfg.ensure_output_dirs()
        app.config["STICKER_CONFIG"] = new_cfg
        return new_cfg

    def entries_or_error():
        return load_stickers(current_config().csv_path)

    def style_or_error(overrides: dict | None = None) -> TextStyle:
        cfg_ = current_config()
        style = TextStyle.from_config(cfg_)
        for key, value in (overrides or {}).items():
            if hasattr(style, key) and value is not None:
                setattr(style, key, type(getattr(style, key))(value))
        return style

    def sticker_status(cfg_, entry: StickerEntry) -> dict:
        raw = cfg_.dir_generated / f"{entry.id}.png"
        final = cfg_.dir_final / f"{entry.id}.png"
        return {
            "id": entry.id,
            "text": entry.text,
            "action": entry.action,
            "expression": entry.expression,
            "category": entry.category,
            "has_raw": raw.exists(),
            "has_final": final.exists(),
            "final_mtime": int(final.stat().st_mtime) if final.exists() else 0,
            "raw_mtime": int(raw.stat().st_mtime) if raw.exists() else 0,
            "size_kb": round(final.stat().st_size / 1024, 1) if final.exists() else 0,
        }

    # ------------------------------------------------------------------
    # 画面
    # ------------------------------------------------------------------
    @app.get("/")
    def index():
        return send_from_directory(WEB_DIR / "templates", "index.html")

    # ------------------------------------------------------------------
    # 状態取得
    # ------------------------------------------------------------------
    @app.get("/api/state")
    def api_state():
        cfg_ = current_config()
        try:
            entries = entries_or_error()
            csv_error = None
        except CsvLoadError as exc:
            entries, csv_error = [], str(exc)

        try:
            font_path = style_or_error().font_path
            font_error = None
        except FontNotFoundError as exc:
            font_path, font_error = "", str(exc)

        master_ok = cfg_.master_image_path.exists()
        packages = sorted(p.name for p in cfg_.dir_packages.glob("*.zip"))

        return jsonify(
            {
                "project_root": str(cfg_.root),
                "config_path": str(cfg_.path),
                "csv_path": str(cfg_.csv_path),
                "csv_error": csv_error,
                "font_path": font_path,
                "font_error": font_error,
                # APIキーの値は返しません。設定済みかどうかだけを返します。
                "api_key_set": bool(cfg_.api_key),
                "provider": cfg_.provider_name,
                "model": cfg_.model,
                "quality": cfg_.quality,
                "master_image": str(cfg_.master_image_path),
                "master_ok": master_ok,
                "has_main": (cfg_.dir_main / "main.png").exists(),
                "has_tab": (cfg_.dir_tab / "tab.png").exists(),
                "packages": packages,
                "line_spec": {
                    "sticker": list(cfg_.sticker_size),
                    "main": list(cfg_.main_size),
                    "tab": list(cfg_.tab_size),
                    "margin": cfg_.margin,
                    "max_file_size_mb": cfg_.get("sticker.max_file_size_mb", 1),
                    "valid_set_sizes": cfg_.get("package.valid_set_sizes", []),
                    "checked_at": cfg_.get("line_spec.checked_at", ""),
                },
                "font": {
                    "size": cfg_.get("font.size"),
                    "min_size": cfg_.get("font.min_size"),
                    "stroke_width": cfg_.get("font.stroke_width"),
                    "fill": cfg_.get("font.fill"),
                    "stroke_fill": cfg_.get("font.stroke_fill"),
                    "position": cfg_.get("font.position"),
                    "max_lines": cfg_.get("font.max_lines"),
                    "band_ratio": cfg_.get("font.band_ratio"),
                    "gap": cfg_.get("font.gap"),
                },
                "stickers": [sticker_status(cfg_, e) for e in entries],
                "job": jobs.current.to_dict() if jobs.current else None,
            }
        )

    @app.get("/api/cost")
    def api_cost():
        cfg_ = current_config()
        count = request.args.get("count", type=int, default=1)
        quality = request.args.get("quality") or cfg_.quality
        cost = estimate_cost_usd(cfg_.model, quality, count)
        return jsonify({"count": count, "quality": quality, "model": cfg_.model, "usd": cost})

    # ------------------------------------------------------------------
    # 画像配信
    # ------------------------------------------------------------------
    def _serve(directory: Path, filename: str):
        path = (directory / filename).resolve()
        # ディレクトリ外への参照を防ぎます。
        if directory.resolve() not in path.parents or not path.exists():
            return jsonify({"error": "not found"}), 404
        response = send_file(path, mimetype="image/png")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/img/final/<sticker_id>.png")
    def img_final(sticker_id: str):
        return _serve(current_config().dir_final, f"{sticker_id}.png")

    @app.get("/img/generated/<sticker_id>.png")
    def img_generated(sticker_id: str):
        return _serve(current_config().dir_generated, f"{sticker_id}.png")

    @app.get("/img/main.png")
    def img_main():
        return _serve(current_config().dir_main, "main.png")

    @app.get("/img/tab.png")
    def img_tab():
        return _serve(current_config().dir_tab, "tab.png")

    @app.get("/img/master.png")
    def img_master():
        cfg_ = current_config()
        return _serve(cfg_.master_image_path.parent, cfg_.master_image_path.name)

    # ------------------------------------------------------------------
    # 文字デザインのライブプレビュー（APIを呼ばない＝無料）
    # ------------------------------------------------------------------
    @app.post("/api/preview-text")
    def api_preview_text():
        cfg_ = current_config()
        body = request.get_json(silent=True) or {}
        sticker_id = str(body.get("id", "")).strip()
        overrides = body.get("style") or {}

        try:
            entries = {e.id: e for e in entries_or_error()}
        except CsvLoadError as exc:
            return jsonify({"error": str(exc)}), 400
        entry = entries.get(sticker_id)
        if entry is None:
            return jsonify({"error": f"IDが見つかりません: {sticker_id}"}), 404
        if body.get("text"):
            entry = StickerEntry(
                id=entry.id, text=str(body["text"]), action=entry.action,
                expression=entry.expression, category=entry.category,
            )

        # band_ratio / gap / position は設定側なので一時的に差し替えます。
        patched = {k: overrides.pop(k) for k in ("band_ratio", "gap", "position") if k in overrides}
        saved = {k: cfg_.get(f"font.{k}") for k in patched}
        for k, v in patched.items():
            cfg_.raw.setdefault("font", {})[k] = v
        try:
            style = style_or_error(overrides)
            if not (cfg_.dir_generated / f"{entry.id}.png").exists():
                return jsonify({"error": "この番号の原画がまだありません"}), 404
            result = pipeline.compose_final_image(cfg_, entry, style)
        except (FontNotFoundError, ip.ImageProcessingError) as exc:
            return jsonify({"error": str(exc)}), 400
        finally:
            for k, v in saved.items():
                cfg_.raw["font"][k] = v

        buf = io.BytesIO()
        result.image.save(buf, format="PNG")
        buf.seek(0)
        response = send_file(buf, mimetype="image/png")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/settings/font")
    def api_settings_font():
        """フォント設定を overrides.yaml へ保存します。"""
        body = request.get_json(silent=True) or {}
        allowed = {
            "size": int, "min_size": int, "stroke_width": int, "max_lines": int,
            "gap": int, "fill": str, "stroke_fill": str, "position": str,
            "band_ratio": float, "path": str,
        }
        updates = {}
        for key, caster in allowed.items():
            if key in body and body[key] is not None:
                try:
                    updates[f"font.{key}"] = caster(body[key])
                except (TypeError, ValueError):
                    return jsonify({"error": f"{key} の値が不正です: {body[key]!r}"}), 400
        if not updates:
            return jsonify({"error": "保存する項目がありません"}), 400

        path = current_config().save_overrides(updates)
        cfg_ = reload_config()
        return jsonify({"saved_to": str(path), "font": cfg_.get("font")})

    # ------------------------------------------------------------------
    # CSV編集
    # ------------------------------------------------------------------
    @app.get("/api/stickers")
    def api_stickers_get():
        try:
            entries = entries_or_error()
        except CsvLoadError as exc:
            return jsonify({"error": str(exc)}), 400
        cfg_ = current_config()
        return jsonify({"stickers": [sticker_status(cfg_, e) for e in entries]})

    @app.post("/api/stickers")
    def api_stickers_post():
        """CSV全体を保存します。上書き前に .bak を作ります。"""
        body = request.get_json(silent=True) or {}
        rows = body.get("stickers")
        if not isinstance(rows, list) or not rows:
            return jsonify({"error": "stickers が空です"}), 400

        try:
            entries = [
                StickerEntry(
                    id=str(r.get("id", "")).strip(),
                    text=str(r.get("text", "")).strip(),
                    action=str(r.get("action", "")).strip(),
                    expression=str(r.get("expression", "")).strip(),
                    category=str(r.get("category", "")).strip() or "misc",
                )
                for r in rows
            ]
            path = save_stickers(current_config().csv_path, entries)
        except (CsvLoadError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400

        cfg_ = current_config()
        return jsonify(
            {
                "saved_to": str(path),
                "count": len(entries),
                "stickers": [sticker_status(cfg_, e) for e in entries],
            }
        )

    # ------------------------------------------------------------------
    # 生成（バックグラウンド）
    # ------------------------------------------------------------------
    def _run_generate(job: Job, ids: list[str], force: bool, dry_run: bool) -> None:
        cfg_ = current_config()
        logger = RunLogger(cfg_.log_path, echo=False)
        state = StateStore(cfg_.state_path)
        generator = ImageGenerator(cfg_, logger, state, dry_run=dry_run)
        style = TextStyle.from_config(cfg_)
        by_id = {e.id: e for e in entries_or_error()}

        for sticker_id in ids:
            if jobs.cancelled():
                job.log("warn", "ユーザー操作により中止しました")
                break
            entry = by_id.get(sticker_id)
            if entry is None:
                job.done += 1
                job.log("error", "CSVに存在しません", sticker_id)
                continue

            result = generator.generate_one(entry, force=force)
            if result.status == "generated":
                job.api_calls += 1
                job.log("ok", "画像を生成しました", sticker_id)
            elif result.status == "skipped":
                job.log("skip", "既存画像のためスキップ（APIを呼びません）", sticker_id)
            elif result.status == "dry-run":
                job.log("info", "DRY-RUN（APIを呼びません）", sticker_id)
                job.done += 1
                continue
            else:
                job.done += 1
                job.log("error", result.detail or "生成に失敗しました", sticker_id)
                continue

            try:
                path, size_bytes, warnings = pipeline.render_final(cfg_, entry, style)
                for w in warnings:
                    job.log("warn", w, sticker_id)
                report = vd.validate_sticker(path, cfg_)
                for issue in report.issues:
                    job.log("warn" if issue.severity == "WARNING" else "error",
                            issue.message, sticker_id)
                if report.ok:
                    job.log("ok", f"完了 ({size_bytes / 1024:.0f}KB)", sticker_id)
                    state.set(sticker_id, "complete")
                else:
                    state.set(sticker_id, "validation_failed", report.errors[0].message)
            except Exception as exc:  # noqa: BLE001 - 1件の失敗で全体を止めない
                job.log("error", f"{type(exc).__name__}: {exc}", sticker_id)
                state.set(sticker_id, "error", str(exc))
            job.done += 1

        job.result["api_calls"] = job.api_calls

    @app.post("/api/generate")
    def api_generate():
        cfg_ = current_config()
        body = request.get_json(silent=True) or {}
        ids = [str(i).strip() for i in (body.get("ids") or []) if str(i).strip()]
        force = bool(body.get("force"))
        dry_run = bool(body.get("dry_run"))

        if not ids:
            return jsonify({"error": "対象が選択されていません"}), 400
        if jobs.is_running():
            return jsonify({"error": "すでに処理が実行中です"}), 409

        if not dry_run:
            if not cfg_.api_key:
                return jsonify(
                    {"error": "OPENAI_API_KEY が未設定です。.env に記入してください。"}
                ), 400
            try:
                check_master_image(cfg_)
            except MasterImageMissingError as exc:
                return jsonify({"error": str(exc)}), 400
            try:
                TextStyle.from_config(cfg_)
            except FontNotFoundError as exc:
                return jsonify({"error": str(exc)}), 400

        job = jobs.start("generate", len(ids), _run_generate, ids, force, dry_run)
        return jsonify(job.to_dict())

    def _run_render(job: Job, ids: list[str]) -> None:
        cfg_ = current_config()
        style = TextStyle.from_config(cfg_)
        by_id = {e.id: e for e in entries_or_error()}
        for sticker_id in ids:
            if jobs.cancelled():
                job.log("warn", "ユーザー操作により中止しました")
                break
            entry = by_id.get(sticker_id)
            job.done += 1
            if entry is None or not (cfg_.dir_generated / f"{sticker_id}.png").exists():
                job.log("skip", "原画がないためスキップ", sticker_id)
                continue
            try:
                path, size_bytes, warnings = pipeline.render_final(cfg_, entry, style)
                for w in warnings:
                    job.log("warn", w, sticker_id)
                job.log("ok", f"再合成しました ({size_bytes / 1024:.0f}KB)", sticker_id)
            except Exception as exc:  # noqa: BLE001
                job.log("error", f"{type(exc).__name__}: {exc}", sticker_id)

    @app.post("/api/render")
    def api_render():
        """APIを呼ばず、文字合成だけやり直します（無料）。"""
        body = request.get_json(silent=True) or {}
        ids = [str(i).strip() for i in (body.get("ids") or []) if str(i).strip()]
        if not ids:
            try:
                ids = [e.id for e in entries_or_error()]
            except CsvLoadError as exc:
                return jsonify({"error": str(exc)}), 400
        if jobs.is_running():
            return jsonify({"error": "すでに処理が実行中です"}), 409
        job = jobs.start("render", len(ids), _run_render, ids)
        return jsonify(job.to_dict())

    @app.get("/api/job")
    def api_job():
        job = jobs.current
        if job is None:
            return jsonify({"job": None})
        since = request.args.get("since", type=int, default=0)
        data = job.to_dict()
        data["events"] = data["events"][since:]
        data["event_offset"] = len(job.events)
        return jsonify({"job": data})

    @app.post("/api/job/cancel")
    def api_job_cancel():
        return jsonify({"cancelled": jobs.cancel()})

    # ------------------------------------------------------------------
    # 検証 / パッケージ / ギャラリー
    # ------------------------------------------------------------------
    @app.post("/api/validate")
    def api_validate():
        cfg_ = current_config()
        try:
            entries = entries_or_error()
        except CsvLoadError as exc:
            return jsonify({"error": str(exc)}), 400

        targets = [e for e in entries if (cfg_.dir_final / f"{e.id}.png").exists()]
        reports = vd.validate_all(cfg_, targets) if targets else []
        items = [
            {
                "file": Path(r.path).name,
                "ok": r.ok,
                "issues": [
                    {"severity": i.severity, "code": i.code, "message": i.message}
                    for i in r.issues
                ],
            }
            for r in reports
        ]
        return jsonify(
            {
                "checked": len(reports),
                "errors": sum(len(r.errors) for r in reports),
                "warnings": sum(len(r.warnings) for r in reports),
                "items": items,
            }
        )

    @app.post("/api/package")
    def api_package():
        cfg_ = current_config()
        try:
            entries = entries_or_error()
            main_path, main_size = pkg.build_main_image(cfg_)
            tab_path, tab_size = pkg.build_tab_image(cfg_)
            results = pkg.build_packages(cfg_, entries)
        except (pkg.PackageError, CsvLoadError) as exc:
            return jsonify({"error": str(exc)}), 400

        return jsonify(
            {
                "main": {"size_kb": round(main_size / 1024, 1),
                         "ok": vd.validate_main(main_path, cfg_).ok},
                "tab": {"size_kb": round(tab_size / 1024, 1),
                        "ok": vd.validate_tab(tab_path, cfg_).ok},
                "packages": [
                    {
                        "name": r.path.name,
                        "count": r.sticker_count,
                        "size_mb": round(r.size_bytes / 1024 / 1024, 2),
                        "downloadable": bool(r.size_bytes),
                        "warnings": r.warnings,
                    }
                    for r in results
                ],
            }
        )

    @app.get("/api/download/<name>")
    def api_download(name: str):
        cfg_ = current_config()
        path = (cfg_.dir_packages / name).resolve()
        if cfg_.dir_packages.resolve() not in path.parents or path.suffix != ".zip":
            return jsonify({"error": "not found"}), 404
        if not path.exists():
            return jsonify({"error": "not found"}), 404
        return send_file(path, as_attachment=True, download_name=name)

    @app.get("/api/package/<name>/contents")
    def api_package_contents(name: str):
        cfg_ = current_config()
        path = (cfg_.dir_packages / name).resolve()
        if cfg_.dir_packages.resolve() not in path.parents or not path.exists():
            return jsonify({"error": "not found"}), 404
        with zipfile.ZipFile(path) as zf:
            names = sorted(zf.namelist())
        return jsonify({"name": name, "files": names})

    @app.post("/api/gallery")
    def api_gallery():
        cfg_ = current_config()
        try:
            entries = entries_or_error()
        except CsvLoadError as exc:
            return jsonify({"error": str(exc)}), 400
        path = gallery_mod.build_gallery(cfg_, entries)
        return jsonify({"path": str(path)})

    @app.get("/api/log")
    def api_log():
        cfg_ = current_config()
        if not cfg_.log_path.exists():
            return jsonify({"lines": []})
        lines = cfg_.log_path.read_text(encoding="utf-8").splitlines()
        return jsonify({"lines": lines[-400:]})

    return app


def run_server(config, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    """GUI サーバーを起動します（ローカル専用）。"""
    # Flask は app.run() の際に CWD から .env を探して読み込みます。
    # どの .env を使うかは load_config() 側で決めたいので、その挙動は止めます。
    os.environ.setdefault("FLASK_SKIP_DOTENV", "1")

    app = create_app(config)
    url = f"http://{host}:{port}/"

    print("=" * 66)
    print("  LINEスタンプ生成 GUI")
    print(f"  {url}")
    print("  終了するには Ctrl+C を押してください。")
    print("=" * 66)

    if open_browser:
        import webbrowser

        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    app.run(host=host, port=port, debug=False, threaded=True, use_reloader=False)
