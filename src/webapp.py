"""ローカル Web GUI（Flask）。

`python -m src.main gui` で 127.0.0.1 にのみバインドして起動します。
外部へは公開しません。APIキーの値はレスポンスに一切含めません。

CLI と同じパイプライン（pipeline / image_generator / validator / package_builder）を
呼び出すだけなので、GUI と CLI の結果は常に一致します。
"""

from __future__ import annotations

import io
import ipaddress
import os
import threading
import traceback
import zipfile
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, jsonify, request, send_file, send_from_directory
from PIL import Image

from . import character_profile as cprof
from . import fonts as fontlib
from . import style_suggest
from . import gallery as gallery_mod
from . import image_processor as ip
from . import image_generator as image_generator_mod
from . import importer
from . import sales as sales_mod
from . import variants as variants_mod
from . import listing as listing_mod
from . import llm as llm_mod
from . import package_builder as pkg
from . import pipeline
from . import validator as vd
from .config import ConfigError, load_config
from .csv_loader import CsvLoadError, StickerEntry, load_stickers, save_stickers
from .image_generator import ImageGenerator, MasterImageMissingError, check_master_image
from .logger import RunLogger, StateStore
from .providers import estimate_cost_usd
from .providers.base import ProviderError
from .text_renderer import FontNotFoundError, TextStyle

WEB_DIR = Path(__file__).resolve().parent / "web"
SRC_DIR = Path(__file__).resolve().parent


def code_fingerprint() -> float:
    """サーバー側プログラム（src/**/*.py）の最終更新時刻。

    画面（HTML/JS）はリクエストのたびにファイルから読み直されますが、
    サーバー側のプログラムは起動時のまま動き続けます。起動後にプログラムが
    更新されると「新しい画面 + 古いサーバー」の食い違いが起きるため、
    それを検出して再起動を促すのに使います。
    """
    return max((f.stat().st_mtime for f in SRC_DIR.rglob("*.py")), default=0.0)


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


class JobBusyError(RuntimeError):
    """ほかのジョブが実行中のため、新しいジョブを始められない（この競合だけを表します）。

    RuntimeError のサブクラスなので、既存の except RuntimeError でも捕まえられます。
    """


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
                raise JobBusyError("すでに処理が実行中です。完了を待つか中止してください。")
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
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024  # 1リクエストあたりのアップロード上限
    jobs = JobManager()
    app.config["JOBS"] = jobs
    started_code = code_fingerprint()

    def server_outdated() -> bool:
        return code_fingerprint() > started_code + 0.001

    # ------------------------------------------------------------------
    # ローカルGUI以外からの書き換えを拒否します（CSRF対策）
    #
    # 127.0.0.1 にしか公開していなくても、ブラウザで悪意あるページを開くと、
    # そのページから同じPCの http://127.0.0.1:8765 へ POST を送れてしまいます。
    # 採用・生成・退避などは取り消しが難しく、画像生成は課金も発生するため、
    # 「別サイトから送られた書き換え」を入口で止めます。読み取り(GET)は制限しません。
    # ------------------------------------------------------------------
    LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}

    def _local(hostname: str | None) -> bool:
        return bool(hostname) and hostname.strip("[]") in LOCAL_HOSTS

    def _host_name(value: str | None) -> str | None:
        """Host ヘッダ（"127.0.0.1:8765" や "[::1]:8765"）からホスト名だけを取り出します。"""
        try:
            return urlsplit(f"//{value or ''}").hostname
        except ValueError:
            return None

    def _readable_host(hostname: str | None) -> bool:
        """読み取りを許すホスト名。このPCの名前か、IPアドレスそのもの（--host 0.0.0.0 で
        スマホ等から IP で開く場合）。DNS rebinding は必ず「名前」で来るので、ここで止まります。"""
        if _local(hostname):
            return True
        try:
            ipaddress.ip_address((hostname or "").strip("[]"))
            return True
        except ValueError:
            return False

    @app.before_request
    def block_cross_site_writes():
        host = _host_name(request.host)
        forwarded = request.headers.get("X-Forwarded-Host")
        if forwarded and not _local(_host_name(forwarded.split(",")[0].strip())):
            return jsonify({"error": "このアドレスからは操作できません"}), 403
        if request.method in ("GET", "HEAD", "OPTIONS"):
            # 別サイトの名前で届いた読み取り（DNS rebinding）で、記録や画像を読まれないように
            if not _readable_host(host):
                return jsonify({"error": "このアドレスからは開けません"}), 403
            return None
        if not _local(host):
            return jsonify({"error": "このアドレスからは操作できません"}), 403
        origin = request.headers.get("Origin")
        if origin and not _local(urlsplit(origin).hostname):
            return jsonify({"error": "別のサイトからの操作は受け付けません"}), 403
        # ブラウザが付ける印。別サイトからの送信は cross-site になります
        site = request.headers.get("Sec-Fetch-Site")
        if site and site not in ("same-origin", "none"):
            return jsonify({"error": "別のサイトからの操作は受け付けません"}), 403
        # フォームや text/plain は、確認なしで別サイトから送れる形式なので、
        # GUI が付ける印（X-Sticker-Client）が無ければ拒否します。
        ctype = (request.content_type or "").split(";")[0].strip().lower()
        if (ctype not in ("", "application/json")
                and request.headers.get("X-Sticker-Client") != "1"
                and (origin is not None or site is not None)):
            return jsonify({"error": "別のサイトからの操作は受け付けません"}), 403
        return None

    @app.errorhandler(variants_mod.StateBusyError)
    def variants_state_busy(exc):
        """候補の記録を一時的に読み書きできない（他の処理が使用中）。壊れていないので 503。"""
        return jsonify({"error": str(exc), "retry": True}), 503

    @app.after_request
    def no_stale_assets(response):
        """画面（HTML/CSS/JS）は毎回サーバーに確認させ、古いものが使われないようにします。"""
        if request.path == "/" or request.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

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

    def sticker_status(cfg_, entry: StickerEntry, sales: dict | None = None) -> dict:
        if sales is None:
            sales = sales_mod.load_sales(cfg_)
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
            "sale": sales.get(entry.id, ""),
        }

    def packages_status(cfg_) -> dict:
        """ZIPがいまの完成画像から作られたものか。

        ZIPより新しい完成画像がある、または完成画像が1枚も無いときは「古い」とみなします
        （キャラクターを作り直したあとに、前のキャラのZIPを完了扱いしないため）。
        """
        zips = list(cfg_.dir_packages.glob("*.zip"))
        finals = list(cfg_.dir_final.glob("*.png"))
        if not zips:
            return {"count": 0, "latest": 0, "stale": False}
        latest_zip = max(z.stat().st_mtime for z in zips)
        latest_final = max((f.stat().st_mtime for f in finals), default=0)
        stale = not finals or latest_final > latest_zip
        return {"count": len(zips), "latest": int(latest_zip), "stale": stale}

    def variants_state(cfg_) -> str:
        """"ok" / "missing" / "corrupt" / "busy"（一時的に読めない）。"""
        try:
            return variants_mod.state_status(cfg_)
        except variants_mod.StateBusyError:
            return "busy"

    def statuses(cfg_, entries) -> list[dict]:
        sales = sales_mod.load_sales(cfg_)
        # 候補の件数だけ一覧に載せます（variants.json の読み込みは1リクエストに1回）
        vstate = variants_mod.load(cfg_)
        found = variants_mod.list_all(cfg_, [e.id for e in entries], vstate)
        out = []
        for e in entries:
            row = sticker_status(cfg_, e, sales)
            sv = found.get(e.id)
            row["variant_count"] = sv.variant_count if sv else 0
            row["adopted"] = sv.adopted if sv else None
            # flags は保存済みの値だけを見ます（ここでは計算しません）
            row["flags"] = sorted({f for v in (sv.variants if sv else []) for f in v.flags})
            # 採用した画像が、採用以外の操作で差し替わっていないか
            row["generated_mismatch"] = bool(sv and sv.generated_mismatch)
            # 採用に失敗し、元に戻す処理も完了できなかった（画像が採用前と違う可能性）
            row["rollback_failed"] = bool(sv and sv.rollback_failed)
            # 初回候補生成の実行中（running）・途中で止まった実行がある（pending）
            row["initial_run"] = variants_mod.initial_status((vstate.get("stickers") or {}).get(e.id))
            out.append(row)
        return out

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
                "server_outdated": server_outdated(),
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
                "packages_status": packages_status(cfg_),
                "validation": vd.load_result(cfg_),
                # 候補の記録の状態（壊れていたら一覧に警告と修復ボタンを出します）
                "variants_state": variants_state(cfg_),
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
                    "font_id": fontlib.current_font_id(cfg_),
                },
                "stickers": statuses(cfg_, entries),
                "job": jobs.current.to_dict() if jobs.current else None,
            }
        )

    @app.get("/api/server")
    def api_server():
        """軽量な状態確認。プログラムが更新されて再起動が必要かどうかを返します。"""
        return jsonify({"outdated": server_outdated()})

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

    @app.get("/img/variants/<sticker_id>/<variant_id>.png")
    def img_variant(sticker_id: str, variant_id: str):
        cfg_ = current_config()
        return _serve(variants_mod.variant_dir(cfg_, sticker_id), f"{variant_id}.png")

    @app.get("/img/master.png")
    def img_master():
        cfg_ = current_config()
        return _serve(cfg_.master_image_path.parent, cfg_.master_image_path.name)

    # ------------------------------------------------------------------
    # ステップ2: キャラクターマスター画像
    # ------------------------------------------------------------------
    @app.get("/api/master")
    def api_master_get():
        cfg_ = current_config()
        p = cfg_.master_image_path
        info = {
            "path": str(p),
            "exists": p.exists(),
            "raw_count": len(list(cfg_.dir_generated.glob("*.png"))),
            "final_count": len(list(cfg_.dir_final.glob("*.png"))),
            "backups": sorted(b.name for b in p.parent.glob("character_master_*.png")),
        }
        if p.exists():
            try:
                with Image.open(p) as im:
                    info.update(width=im.width, height=im.height, mode=im.mode)
                    # 体が画像の端で切れていないか（全身が入っていないとスタンプの絵も切れます）
                    info["cropped_sides"] = ip.cropped_sides(im)
                info["mtime"] = int(p.stat().st_mtime)
                info["size_kb"] = round(p.stat().st_size / 1024, 1)
            except Exception as exc:  # noqa: BLE001
                info["error"] = f"画像を読み込めません: {exc}"
        info.update(_prompt_info(cfg_))
        return jsonify(info)

    def _prompt_info(cfg_) -> dict:
        """マスタープロンプトの状態。日本語版があればそれが使われます。"""
        from .prompt_generator import (
            DEFAULT_CHARACTER_JA,
            FIXED_STYLE_PROMPT,
            master_prompt_from_config,
        )

        ja_path = cfg_.master_prompt_ja_path
        ja_text = ja_path.read_text(encoding="utf-8").strip() if ja_path.exists() else ""
        return {
            # 編集欄が空にならないよう、未作成なら初期値（日本語）を返します。
            "prompt_ja": ja_text or DEFAULT_CHARACTER_JA,
            "prompt_mode": "ja" if ja_text else "en",
            "prompt_ja_path": str(ja_path),
            "prompt_en_path": str(cfg_.master_prompt_path),
            "fixed_prompt": FIXED_STYLE_PROMPT,
            "full_prompt": master_prompt_from_config(cfg_),
        }

    @app.post("/api/master/prompt")
    def api_master_prompt():
        """日本語のキャラクター説明を保存します。

        英語版（prompts/character_master.txt）は書き換えません。
        日本語版を空にして保存したい場合は、英語版へ戻すことになるため拒否します。
        """
        cfg_ = current_config()
        body = request.get_json(silent=True) or {}
        text = str(body.get("prompt_ja") or body.get("prompt") or "").strip()
        if not text:
            return jsonify({"error": "キャラクターの説明が空です"}), 400
        path = cfg_.master_prompt_ja_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
        if isinstance(body.get("profile"), dict):
            cprof.save_profile(cfg_.character_profile_path, body["profile"])
        body = {"saved_to": str(path)}
        body.update(_prompt_info(cfg_))
        return jsonify(body)

    @app.get("/api/master/profile")
    def api_master_profile():
        """かんたん入力の選択肢・ひな形と、保存済みの選択内容を返します。"""
        cfg_ = current_config()
        ja_path = cfg_.master_prompt_ja_path
        text = ja_path.read_text(encoding="utf-8").strip() if ja_path.exists() else ""
        profile = cprof.load_profile(cfg_.character_profile_path)
        if profile is None and text:
            # 保存された選択内容が無くても、説明文がひな形と同じなら選択状態を復元します。
            profile = cprof.infer_profile(text)
        return jsonify({
            "groups": cprof.GROUPS,
            "presets": cprof.PRESETS,
            "preset_info": cprof.PRESET_INFO,
            "preset_categories": cprof.PRESET_CATEGORIES,
            # JSONの辞書はキーが並べ替えられるので、表示順は別に渡します。
            "preset_order": list(cprof.PRESETS),
            "profile": profile,
            # 説明文が選択内容から作った文章と違う＝手で書き換えてある
            "text_matches_profile": bool(profile) and cprof.compose(profile) == text,
        })

    @app.get("/api/master/random")
    def api_master_random():
        """おまかせで組み合わせたキャラクターを返します（シャッフル用。保存はしません）。"""
        import random

        try:
            n = min(max(int(request.args.get("n", 4)), 1), 12)
        except ValueError:
            n = 4
        rng = random.Random()
        out, seen = [], set()
        for _ in range(n * 5):
            p = cprof.random_profile(rng)
            name = cprof.profile_name(p)
            if name in seen or name in cprof.PRESETS:
                continue
            seen.add(name)
            out.append({"name": name, "profile": p, "text": cprof.compose(p)})
            if len(out) >= n:
                break
        return jsonify({"characters": out})

    @app.post("/api/master/compose")
    def api_master_compose():
        """選択内容から説明文を組み立てます（保存はしません）。"""
        body = request.get_json(silent=True) or {}
        profile = cprof.normalize(body.get("profile"))
        return jsonify({"text": cprof.compose(profile), "profile": profile})

    def _backup_master(path: Path) -> str | None:
        """既存のマスター画像を退避します（削除はしません）。"""
        if not path.exists():
            return None
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = path.with_name(f"character_master_{stamp}.png")
        path.replace(backup)
        return backup.name

    @app.post("/api/master/upload")
    def api_master_upload():
        """自分で用意した画像をマスターとして設定します。"""
        cfg_ = current_config()
        file = request.files.get("file")
        if file is None or not file.filename:
            return jsonify({"error": "ファイルが選ばれていません"}), 400

        data = file.read()
        if not data:
            return jsonify({"error": "ファイルが空です"}), 400

        # 拡張子ではなく中身で判定します（PNG以外もPNGへ変換して受け入れます）。
        try:
            with Image.open(io.BytesIO(data)) as im:
                im.load()
                rgba = im.convert("RGBA")
        except Exception:  # noqa: BLE001
            return jsonify({"error": "画像として読み込めませんでした"}), 400

        if not ip.has_transparency(rgba):
            rgba = ip.make_background_transparent(rgba)
        if ip.is_blank(rgba):
            return jsonify({"error": "画像の中身が空です（全ピクセルが透明）"}), 400

        target = cfg_.master_image_path
        target.parent.mkdir(parents=True, exist_ok=True)
        backup = _backup_master(target)
        rgba.save(target, format="PNG")
        return jsonify(
            {
                "saved_to": str(target),
                "backup": backup,
                "width": rgba.width,
                "height": rgba.height,
            }
        )

    @app.post("/api/master/restore")
    def api_master_restore():
        """退避したマスター画像を戻します。"""
        cfg_ = current_config()
        name = str((request.get_json(silent=True) or {}).get("name", ""))
        target = cfg_.master_image_path
        source = (target.parent / name).resolve()
        if (
            not name.startswith("character_master_")
            or source.suffix != ".png"
            or target.parent.resolve() not in source.parents
            or not source.exists()
        ):
            return jsonify({"error": "指定された履歴が見つかりません"}), 404
        _backup_master(target)
        source.replace(target)
        return jsonify({"restored": name})

    def _run_master(job: Job) -> None:
        from .prompt_generator import CONSISTENCY_RULE, NO_TEXT_RULE, master_prompt_from_config
        from .providers import create_provider

        cfg_ = current_config()
        prompt = "\n\n".join(
            [
                master_prompt_from_config(cfg_),
                "POSE: standing straight and relaxed, facing forward, arms down naturally.\n"
                "FACIAL EXPRESSION: calm friendly smile.\n"
                "This is the reference sheet image that defines the character design.",
                CONSISTENCY_RULE,
                NO_TEXT_RULE,
            ]
        )
        job.log("info", "キャラクターマスター画像を生成します")
        provider = create_provider(cfg_)
        target = cfg_.master_image_path
        target.parent.mkdir(parents=True, exist_ok=True)
        backup = _backup_master(target)
        if backup:
            job.log("info", f"前のマスター画像を {backup} として残しました")
        try:
            provider.generate(prompt=prompt, reference_image=None, output_path=str(target))
        except Exception:
            # 失敗したら退避した画像を戻します。
            if backup and (target.parent / backup).exists():
                (target.parent / backup).replace(target)
                job.log("warn", "生成に失敗したため、前のマスター画像を戻しました")
            raise
        job.api_calls += 1
        job.done = 1
        job.log("ok", "マスター画像を作成しました")
        with Image.open(target) as im:
            sides = ip.cropped_sides(im)
        if sides:
            job.log("warn", "キャラクターが画像の端で切れているようです。全身が入るよう、作り直すか履歴から戻してください")

    @app.post("/api/master/generate")
    def api_master_generate():
        cfg_ = current_config()
        if not cfg_.api_key:
            return jsonify({"error": "OPENAI_API_KEY が未設定です。.env に記入してください。"}), 400
        if jobs.is_running():
            return jsonify({"error": "すでに処理が実行中です"}), 409
        try:
            job = jobs.start("master", 1, _run_master)
        except JobBusyError as exc:     # 入口の確認とほぼ同時に、別のジョブが始まった
            return jsonify({"error": str(exc)}), 409
        return jsonify(job.to_dict())

    @app.post("/api/generated/archive")
    def api_generated_archive():
        """生成済み画像を退避します（削除はしません）。

        キャラクターを変えたあと、古い絵柄の画像が混ざらないようにするためのものです。
        """
        cfg_ = current_config()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = cfg_.root / "output" / "archive" / stamp
        moved = {"generated": 0, "final": 0}
        sources = (("generated", cfg_.dir_generated), ("final", cfg_.dir_final))
        sticker_ids = sorted({f.stem for _kind, d in sources for f in d.glob("*.png")})
        try:
            with ExitStack() as locks:
                # 採用中のスタンプの画像を動かすと、採用の巻き戻しで元に戻されてしまうため、
                # 対象スタンプの鍵を ID 順に取ってから動かします（修復と同じ順序）
                for sticker_id in sticker_ids:
                    locks.enter_context(variants_mod.adopt_lock(cfg_, sticker_id))
                for kind, src_dir in sources:
                    files = sorted(src_dir.glob("*.png"))
                    if not files:
                        continue
                    dest = base / kind
                    dest.mkdir(parents=True, exist_ok=True)
                    for f in files:
                        f.replace(dest / f.name)
                        moved[kind] += 1
        except variants_mod.LockBusyError as exc:
            return jsonify({"error": f"{exc}（まだ何も退避していません）"}), 409
        if not any(moved.values()):
            return jsonify({"error": "退避する画像がありません"}), 400
        return jsonify({"archived_to": str(base), "moved": moved})

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

        # フォントはIDで受け取り、一覧に載っているものだけを使います（任意のファイルは読みません）。
        font_id = overrides.pop("font_id", None)
        font_override = {}
        if font_id:
            info = fontlib.find_font(cfg_, str(font_id))
            if info is None:
                return jsonify({"error": f"フォントが見つかりません: {font_id}"}), 400
            font_override = {"font_path": info.path, "font_index": info.index, "variation": info.variation}
        for key in ("font_path", "font_index", "variation"):
            overrides.pop(key, None)  # 画面から直接パスを指定させない
        overrides.update(font_override)

        # band_ratio / gap / position は設定側なので一時的に差し替えます。
        patched = {k: overrides.pop(k) for k in ("band_ratio", "gap", "position") if k in overrides}
        saved = {k: cfg_.get(f"font.{k}") for k in patched}
        for k, v in patched.items():
            cfg_.raw.setdefault("font", {})[k] = v
        # まだ生成していないスタンプでも、マスター画像を代役にしてプレビューできます。
        # これにより、1円も使う前に文字デザインを決められます。
        raw = cfg_.dir_generated / f"{entry.id}.png"
        if raw.exists():
            source, source_kind = raw, "raw"
        elif cfg_.master_image_path.exists():
            source, source_kind = cfg_.master_image_path, "master"
        else:
            for k, v in saved.items():
                cfg_.raw["font"][k] = v
            return jsonify(
                {
                    "error": "プレビューに使える画像がありません。"
                    "キャラクターマスター画像を用意すると、生成前でも文字の見た目を確認できます。"
                }
            ), 404

        try:
            style = style_or_error(overrides)
            result = pipeline.compose_final_image(cfg_, entry, style, source=source)
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
        response.headers["X-Preview-Source"] = source_kind
        return response

    @app.post("/api/settings/font")
    def api_settings_font():
        """フォント設定を overrides.yaml へ保存します。"""
        body = request.get_json(silent=True) or {}
        allowed = {
            "size": int, "min_size": int, "stroke_width": int, "max_lines": int,
            "gap": int, "fill": str, "stroke_fill": str, "position": str,
            "band_ratio": float,
        }
        updates = {}
        if body.get("font_id"):
            info = fontlib.find_font(current_config(), str(body["font_id"]))
            if info is None:
                return jsonify({"error": f"フォントが見つかりません: {body['font_id']}"}), 400
            # プロジェクト内のフォントは相対パスで保存します（フォルダを移動しても動くように）
            updates.update({"font.path": fontlib.portable_path(current_config(), info.path),
                            "font.index": info.index,
                            "font.variation": info.variation})
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

    @app.get("/api/fonts")
    def api_fonts():
        """選べるフォントの一覧と、いま使っているフォント、追加できる無料フォント。"""
        cfg_ = current_config()
        installed = {f.id for f in fontlib.available_fonts(cfg_)}
        return jsonify({
            "fonts": [f.to_dict() for f in fontlib.available_fonts(cfg_)],
            "current": fontlib.current_font_id(cfg_),
            "custom_dir": str(fontlib.custom_font_dir(cfg_)),
            "free_categories": fontlib.FREE_FONT_CATEGORIES,
            "free_fonts": [
                {"id": f.id, "label": f.label, "category": f.category, "size_mb": f.size_mb,
                 "note": f.note, "installed": f.id in installed}
                for f in fontlib.FREE_FONTS
            ],
        })

    @app.get("/api/fonts/check")
    def api_fonts_check():
        """選んだフォントに、セリフで使っている文字がそろっているか調べます。"""
        cfg_ = current_config()
        info = fontlib.find_font(cfg_, str(request.args.get("font_id", "")))
        if info is None:
            return jsonify({"error": "フォントが見つかりません"}), 404
        try:
            texts = [e.text for e in entries_or_error()]
        except CsvLoadError as exc:
            return jsonify({"error": str(exc)}), 400
        missing = fontlib.missing_chars(info.path, texts, info.index)
        affected = [e.id for e in entries_or_error() if any(ch in e.text for ch in missing)]
        return jsonify({"font_id": info.id, "missing": missing, "affected_ids": affected})

    @app.post("/api/fonts/install")
    def api_fonts_install():
        """一覧にある無料フォントをダウンロードして追加します（ボタンを押したときだけ）。"""
        cfg_ = current_config()
        font_id = str((request.get_json(silent=True) or {}).get("id", ""))
        if jobs.is_running():
            return jsonify({"error": "ほかの処理が実行中です。終わってから追加してください"}), 409
        try:
            path = fontlib.install_free_font(cfg_, font_id)
        except fontlib.FontInstallError as exc:
            return jsonify({"error": str(exc)}), 400
        info = fontlib.find_font(cfg_, font_id)
        return jsonify({"installed": font_id, "path": str(path),
                        "font": info.to_dict() if info else None})

    @app.get("/api/design/suggest")
    def api_design_suggest():
        """キャラクターに合う文字スタイルの候補（設定は変えません）。"""
        return jsonify(style_suggest.suggest_styles(current_config()))

    # ------------------------------------------------------------------
    # CSV編集
    # ------------------------------------------------------------------
    @app.patch("/api/stickers/<sticker_id>")
    def api_sticker_patch(sticker_id: str):
        """1件のセリフだけを書き換えます（改行も保存できます）。"""
        body = request.get_json(silent=True) or {}
        text = str(body.get("text", "")).replace("\r\n", "\n").strip()
        if not text:
            return jsonify({"error": "セリフが空です"}), 400
        cfg_ = current_config()
        try:
            entries = entries_or_error()
        except CsvLoadError as exc:
            return jsonify({"error": str(exc)}), 400
        target = next((e for e in entries if e.id == sticker_id), None)
        if target is None:
            return jsonify({"error": f"IDが見つかりません: {sticker_id}"}), 404
        updated = [
            StickerEntry(id=e.id, text=text, action=e.action, expression=e.expression,
                         category=e.category) if e.id == sticker_id else e
            for e in entries
        ]
        save_stickers(cfg_.csv_path, updated)
        entry = next(e for e in updated if e.id == sticker_id)
        return jsonify({"sticker": sticker_status(cfg_, entry)})
    @app.get("/api/stickers")
    def api_stickers_get():
        try:
            entries = entries_or_error()
        except CsvLoadError as exc:
            return jsonify({"error": str(exc)}), 400
        cfg_ = current_config()
        return jsonify({"stickers": statuses(cfg_, entries), "validation": vd.load_result(cfg_),
                        "packages_status": packages_status(cfg_)})

    # ---------- 候補（variant）の読み取り ----------
    @app.get("/api/variants")
    def api_variants_get():
        """CSVにある全スタンプの候補状況を返します（読み取りのみ）。"""
        try:
            entries = entries_or_error()
        except CsvLoadError as exc:
            return jsonify({"error": str(exc)}), 400
        cfg_ = current_config()
        state = variants_mod.load(cfg_)
        found = variants_mod.list_all(cfg_, [e.id for e in entries], state)
        return jsonify({
            "schema": state.get("schema", variants_mod.SCHEMA),
            "state_exists": cfg_.variants_path.exists(),
            "state_status": variants_state(cfg_),
            "stickers": {sid: sv.to_dict() for sid, sv in found.items()},
        })

    @app.get("/api/variants/<sticker_id>")
    def api_variants_one(sticker_id: str):
        cfg_ = current_config()
        try:
            known = {e.id for e in entries_or_error()}
        except CsvLoadError as exc:
            return jsonify({"error": str(exc)}), 400
        if sticker_id not in known:
            return jsonify({"error": f"IDが見つかりません: {sticker_id}"}), 404
        sticker = variants_mod.get_sticker(cfg_, sticker_id)
        if sticker is None:
            return jsonify({"sticker_id": sticker_id, "adopted": None, "legacy": False,
                            "variant_count": 0, "variants": []})
        return jsonify(sticker.to_dict())

    def _variant_entry(sticker_id: str):
        """CSVの行を取り出します。無ければ (None, エラーレスポンス)。"""
        try:
            entry = next((e for e in entries_or_error() if e.id == sticker_id), None)
        except CsvLoadError as exc:
            return None, (jsonify({"error": str(exc)}), 400)
        if entry is None:
            return None, (jsonify({"error": f"IDが見つかりません: {sticker_id}"}), 404)
        return entry, None

    def _variant_error(exc) -> tuple:
        """候補の操作エラーをAPIの形にします（記録が壊れている場合は 409）。"""
        if isinstance(exc, variants_mod.StateBusyError):
            return jsonify({"error": str(exc), "retry": True}), 503
        status = 409 if isinstance(exc, variants_mod.StateCorruptError) else 400
        return jsonify({"error": str(exc)}), status

    @app.post("/api/variants/repair")
    def api_variants_repair():
        """壊れた記録を退避し、候補フォルダの画像から記録を作り直します（画像は消しません）。"""
        cfg_ = current_config()
        try:
            report = variants_mod.repair_state(cfg_)
        except variants_mod.VariantError as exc:
            return _variant_error(exc)
        return jsonify({"report": report, "variants_state": variants_state(cfg_)})

    # ------------------------------------------------------------------
    # 再生成（Phase 6）: regen の印が付いたスタンプだけに候補を作る
    # ------------------------------------------------------------------
    def _regen_count(body: dict) -> int:
        count = body.get("count", variants_mod.REGEN_DEFAULT_COUNT)
        if (isinstance(count, bool) or not isinstance(count, int)
                or not 1 <= count <= variants_mod.REGEN_MAX_COUNT):
            raise ValueError(
                f"1スタンプあたりの枚数は 1〜{variants_mod.REGEN_MAX_COUNT} の整数で指定してください: {count}")
        return count

    def _run_regen(job: Job, ids: list[str], count: int) -> None:
        """再生成の候補を作るジョブ。API を呼んでいる間は鍵を持ちません（記録の更新時だけ）。"""
        cfg_ = current_config()
        logger = RunLogger(cfg_.log_path, echo=False)
        state = StateStore(cfg_.state_path)
        generator = ImageGenerator(cfg_, logger, state, dry_run=False)
        by_id = {e.id: e for e in entries_or_error()}

        for sticker_id in ids:
            if jobs.cancelled():
                job.log("warn", "ユーザー操作により中止しました（作れた候補は残し、次回は残りだけ作ります）")
                break
            entry = by_id.get(sticker_id)
            if entry is None:
                job.log("error", "CSVに存在しません", sticker_id)
                continue

            def on_start(remaining, sid=sticker_id):
                job.log("info", f"候補を {remaining} 枚作ります", sid)

            def on_event(sid, variant_id, status, detail):
                job.api_calls += 1
                job.done += 1
                if status == "generated":
                    job.log("ok", f"{variant_id} を作りました", sid)
                else:
                    job.log("error", f"{variant_id}: {detail}", sid)

            # 記録の鍵の中で対象かどうかを確かめ直し、実行中の目印を付けてから作ります
            # （番号の予約・登録・締めは variants.run_regen に任せます）
            outcome = variants_mod.run_regen(cfg_, entry, count, generator, on_start=on_start,
                                             on_event=on_event, should_stop=jobs.cancelled)
            if not outcome["claimed"]:
                job.log("skip", "対象ではなくなったか、別の処理が実行中のため作りませんでした", sticker_id)
            elif outcome["complete"]:
                job.log("ok", "再生成の候補を作り終えました", sticker_id)
            else:
                job.log("warn", "途中で止まりました（作れた候補は残し、次回は残りだけ作ります）", sticker_id)
        job.result["api_calls"] = job.api_calls

    @app.post("/api/variants/generate")
    def api_variants_generate():
        """regen の印が付いたスタンプに、新しい候補を作ります。

        dry_run=true なら対象・枚数・費用だけを返します（プロバイダを作らず、APIキーも使いません）。
        実行するときは、確認画面で見た合計枚数（expected_total）を添えてください。
        いまの合計と違えば、確認し直してもらうため生成を始めません。
        """
        cfg_ = current_config()
        body = request.get_json(silent=True) or {}
        if body.get("regen_only") is not True:
            return jsonify({"error": "いまは regen_only=true（再生成の印が付いたスタンプだけ）に対応しています"}), 400
        try:
            count = _regen_count(body)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        try:
            entries = entries_or_error()
        except CsvLoadError as exc:
            return jsonify({"error": str(exc)}), 400
        if variants_state(cfg_) == "corrupt":
            return _variant_error(variants_mod.StateCorruptError(variants_mod.corrupt_message(cfg_)))

        plan = variants_mod.regen_plan(cfg_, [e.id for e in entries], count)
        total = plan["total"]
        summary = {
            "count": count, "targets": plan["targets"], "busy": plan["busy"], "total": total,
            "usd": estimate_cost_usd(cfg_.model, cfg_.quality, total),
            "model": cfg_.model, "quality": cfg_.quality,
            "max_count": variants_mod.REGEN_MAX_COUNT, "max_total": variants_mod.REGEN_MAX_TOTAL,
        }
        if total > variants_mod.REGEN_MAX_TOTAL:
            return jsonify({**summary, "error": (
                f"1回に作れる候補は {variants_mod.REGEN_MAX_TOTAL} 枚までです（今回 {total} 枚）。"
                "1スタンプあたりの枚数を減らしてください")}), 400
        if body.get("dry_run"):
            return jsonify(summary)

        expected = body.get("expected_total")
        if isinstance(expected, bool) or not isinstance(expected, int):
            return jsonify({"error": "確認画面の合計枚数（expected_total）を指定してください"}), 400
        if total == 0:
            return jsonify({**summary, "error": "再生成の対象がありません（印が無いか、作り終えています）"}), 400
        if expected != total:
            return jsonify({**summary, "error": "対象が変わりました。もう一度確認してから実行してください"}), 409
        if not cfg_.api_key:
            return jsonify({"error": "OPENAI_API_KEY が未設定です。.env に記入してください。"}), 400
        try:
            image_generator_mod.create_provider(cfg_)      # 設定の誤りをここで知らせる（通信はしません）
            check_master_image(cfg_)
        except (ProviderError, ValueError, MasterImageMissingError) as exc:
            return jsonify({"error": str(exc)}), 400
        try:
            job = jobs.start("variants", total, _run_regen, [t["id"] for t in plan["targets"]], count)
        except RuntimeError as exc:                         # ほかの処理が実行中（同時のリクエストを含む）
            return jsonify({"error": str(exc)}), 409
        return jsonify({**summary, "job": job.to_dict()})

    # ------------------------------------------------------------------
    # 初回候補生成（Phase 7）: 候補が1件も無いスタンプに、最初の候補を作る
    # ------------------------------------------------------------------
    def _run_initial(job: Job, ids: list[str], count: int) -> None:
        """初回生成の候補を作るジョブ。API を呼んでいる間は鍵を持ちません（記録の更新時だけ）。"""
        cfg_ = current_config()
        logger = RunLogger(cfg_.log_path, echo=False)
        state = StateStore(cfg_.state_path)
        generator = ImageGenerator(cfg_, logger, state, dry_run=False)
        by_id = {e.id: e for e in entries_or_error()}

        for sticker_id in ids:
            if jobs.cancelled():
                job.log("warn", "ユーザー操作により中止しました（作れた候補は残し、次回は残りだけ作ります）")
                break
            entry = by_id.get(sticker_id)
            if entry is None:
                job.log("error", "CSVに存在しません", sticker_id)
                continue

            def on_start(remaining, recovered, sid=sticker_id):
                if recovered:
                    job.log("ok", f"前回作れていた {recovered} 枚を登録しました（APIは呼びません）", sid)
                job.log("info", f"最初の候補を {remaining} 枚作ります", sid)

            def on_event(sid, variant_id, status, detail):
                job.api_calls += 1
                job.done += 1
                if status == "generated":
                    job.log("ok", f"{variant_id} を作りました", sid)
                else:
                    job.log("error", f"{variant_id}: {detail}", sid)

            # 記録の鍵の中で対象かどうかを確かめ直し、実行中の目印を付けてから作ります
            outcome = variants_mod.run_initial(cfg_, entry, count, generator, on_start=on_start,
                                               on_event=on_event, should_stop=jobs.cancelled)
            if outcome["closed"]:
                job.log("ok", f"前回の続きを締めました（登録 {outcome['recovered']} 枚、APIは呼びません）",
                        sticker_id)
            elif not outcome["claimed"]:
                job.log("skip", "対象ではなくなったか、別の処理が実行中のため作りませんでした", sticker_id)
            elif outcome["complete"]:
                job.log("ok", "最初の候補を作り終えました（採用は候補比較の画面で行います）", sticker_id)
            else:
                job.log("warn", "途中で止まりました（作れた候補は残し、次回は残りだけ作ります）", sticker_id)
        job.result["api_calls"] = job.api_calls

    @app.post("/api/variants/initial")
    def api_variants_initial():
        """候補が1件も無いスタンプに、最初の候補を作ります（途中で止まった初回生成の続きも）。

        dry_run=true なら対象・枚数・費用だけを返します（プロバイダを作らず、何も書きません）。
        実行するときは、確認画面で見た合計枚数（expected_total）を添えてください（再生成と同じ）。
        """
        cfg_ = current_config()
        body = request.get_json(silent=True) or {}
        ids = body.get("ids")
        if (not isinstance(ids, list) or not ids
                or not all(isinstance(i, str) and i.strip() for i in ids)):
            return jsonify({"error": "対象のスタンプ（ids）を1件以上選んでください"}), 400
        ids = [i.strip() for i in ids]
        if len(set(ids)) != len(ids):
            return jsonify({"error": "同じスタンプが重複して指定されています"}), 400
        try:
            count = _regen_count(body)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        try:
            entries = entries_or_error()
        except CsvLoadError as exc:
            return jsonify({"error": str(exc)}), 400
        known = {e.id for e in entries}
        unknown = [i for i in ids if i not in known]
        if unknown:
            return jsonify({"error": f"IDが見つかりません: {', '.join(unknown)}"}), 400
        if variants_state(cfg_) == "corrupt":
            return _variant_error(variants_mod.StateCorruptError(variants_mod.corrupt_message(cfg_)))

        plan = variants_mod.initial_plan(cfg_, ids, count)
        total = plan["total"]
        summary = {
            "ids": ids, "count": count, "targets": plan["targets"],
            "target_ids": [t["id"] for t in plan["targets"]], "target_count": len(plan["targets"]),
            "skipped": plan["skipped"], "busy": plan["busy"], "total": total, "expected_total": total,
            "usd": estimate_cost_usd(cfg_.model, cfg_.quality, total),
            "model": cfg_.model, "quality": cfg_.quality,
            "max_count": variants_mod.INITIAL_MAX_COUNT, "max_total": variants_mod.INITIAL_MAX_TOTAL,
        }
        if total > variants_mod.INITIAL_MAX_TOTAL:
            return jsonify({**summary, "error": (
                f"1回に作れる候補は {variants_mod.INITIAL_MAX_TOTAL} 枚までです（今回 {total} 枚）。"
                "1スタンプあたりの枚数か、スタンプの数を減らしてください")}), 400
        if body.get("dry_run"):
            return jsonify(summary)

        expected = body.get("expected_total")
        if isinstance(expected, bool) or not isinstance(expected, int):
            return jsonify({"error": "確認画面の合計枚数（expected_total）を指定してください"}), 400
        if not plan["targets"]:
            return jsonify({**summary, "error": "初回生成の対象がありません（候補・原画があるか、実行中です）"}), 400
        if expected != total:
            return jsonify({**summary, "error": "対象が変わりました。もう一度確認してから実行してください"}), 409
        if not cfg_.api_key:
            return jsonify({"error": "OPENAI_API_KEY が未設定です。.env に記入してください。"}), 400
        try:
            image_generator_mod.create_provider(cfg_)      # 設定の誤りをここで知らせる（通信はしません）
            check_master_image(cfg_)
        except (ProviderError, ValueError, MasterImageMissingError) as exc:
            return jsonify({"error": str(exc)}), 400
        try:
            job = jobs.start("initial", total, _run_initial, summary["target_ids"], count)
        except JobBusyError as exc:                         # ほかの処理が実行中（同時のリクエストを含む）
            return jsonify({"error": str(exc)}), 409
        return jsonify({**summary, "job": job.to_dict()})

    @app.post("/api/variants/<sticker_id>/<variant_id>/adopt")
    def api_variants_adopt(sticker_id: str, variant_id: str):
        """候補を採用します（退避・合成・検証は既存の variants.adopt に任せます）。"""
        cfg_ = current_config()
        entry, err = _variant_entry(sticker_id)
        if err:
            return err
        try:
            style = style_or_error()
        except FontNotFoundError as exc:
            return jsonify({"error": str(exc)}), 400
        try:
            result = variants_mod.adopt(cfg_, entry, variant_id, style=style)
        except variants_mod.VariantError as exc:
            return _variant_error(exc)
        sticker = variants_mod.get_sticker(cfg_, sticker_id)
        return jsonify({
            "sticker": sticker.to_dict() if sticker else None,
            "final_size_kb": round(result["size_bytes"] / 1024, 1),
            "warnings": result["warnings"],
            "validation": vd.load_result(cfg_),
            "packages_status": packages_status(cfg_),
        })

    @app.post("/api/variants/<sticker_id>/<variant_id>/verdict")
    def api_variants_verdict(sticker_id: str, variant_id: str):
        """候補に人の判断（pending / rejected / regen）を付けます。"""
        cfg_ = current_config()
        body = request.get_json(silent=True) or {}
        try:
            variants_mod.set_verdict(cfg_, sticker_id, variant_id, str(body.get("verdict", "")))
        except variants_mod.VariantError as exc:
            return _variant_error(exc)
        return jsonify({"sticker": variants_mod.get_sticker(cfg_, sticker_id).to_dict()})

    @app.post("/api/variants/<sticker_id>/<variant_id>/score")
    def api_variants_score(sticker_id: str, variant_id: str):
        """候補1件を再評価します（計算は scoring.py のみ。人の判断は変えません）。"""
        from . import scoring

        cfg_ = current_config()
        try:
            scoring.score_variant(cfg_, sticker_id, variant_id)
        except (scoring.ScoringError, variants_mod.VariantError) as exc:
            return _variant_error(exc)
        return jsonify({"sticker": variants_mod.get_sticker(cfg_, sticker_id).to_dict(),
                        "formula": scoring.SCORING_FORMULA})

    @app.post("/api/variants/<sticker_id>/<variant_id>/rating")
    def api_variants_rating(sticker_id: str, variant_id: str):
        """候補に5段階の評価を付けます（null で消します）。"""
        cfg_ = current_config()
        body = request.get_json(silent=True) or {}
        rating = body.get("rating")
        if rating is not None:
            try:
                rating = int(rating)
            except (TypeError, ValueError):
                return jsonify({"error": f"評価は数字で指定してください: {rating}"}), 400
        try:
            variants_mod.set_rating(cfg_, sticker_id, variant_id, rating)
        except variants_mod.VariantError as exc:
            return _variant_error(exc)
        return jsonify({"sticker": variants_mod.get_sticker(cfg_, sticker_id).to_dict()})

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
                "stickers": statuses(cfg_, entries),
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

            # generated/<id>.png と final/<id>.png を書き換えるので、採用・取り込みと同じ
            # スタンプの鍵を持って行います（採用の巻き戻しに、生成した画像を消されないため）。
            # 鍵の順序は「スタンプ → 記録」。ここでは記録（variants.json）の鍵は取りません。
            try:
                with variants_mod.adopt_lock(cfg_, sticker_id):
                    _generate_one_locked(job, generator, entry, force, cfg_, style, state)
            except variants_mod.LockBusyError as exc:
                job.log("error", f"{exc}（このスタンプは生成しませんでした）", sticker_id)
            job.done += 1

        job.result["api_calls"] = job.api_calls

    def _generate_one_locked(job: Job, generator, entry, force: bool, cfg_, style, state) -> None:
        """1スタンプ分の生成・合成・検証。スタンプの鍵を持った状態で呼びます。"""
        sticker_id = entry.id
        result = generator.generate_one(entry, force=force)
        if result.status == "generated":
            job.api_calls += 1
            job.log("ok", "画像を生成しました", sticker_id)
        elif result.status == "skipped":
            job.log("skip", "既存画像のためスキップ（APIを呼びません）", sticker_id)
        elif result.status == "dry-run":
            job.log("info", "DRY-RUN（APIを呼びません）", sticker_id)
            return
        else:
            job.log("error", result.detail or "生成に失敗しました", sticker_id)
            return

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

        try:
            job = jobs.start("generate", len(ids), _run_generate, ids, force, dry_run)
        except JobBusyError as exc:     # 入口の確認とほぼ同時に、別のジョブが始まった
            return jsonify({"error": str(exc)}), 409
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
            if entry is None:
                job.log("skip", "原画がないためスキップ", sticker_id)
                continue
            try:
                # final/<id>.png を書き換えるので、採用と同時に走らないようスタンプの鍵を持ちます
                with variants_mod.adopt_lock(cfg_, sticker_id):
                    if not (cfg_.dir_generated / f"{sticker_id}.png").exists():
                        job.log("skip", "原画がないためスキップ", sticker_id)
                        continue
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
        try:
            job = jobs.start("render", len(ids), _run_render, ids)
        except JobBusyError as exc:     # 入口の確認とほぼ同時に、別のジョブが始まった
            return jsonify({"error": str(exc)}), 409
        return jsonify(job.to_dict())

    # ------------------------------------------------------------------
    # 手持ち画像の取り込み（APIを呼ばない＝無料）
    # ------------------------------------------------------------------
    def _import_result_dict(r: importer.ImportResult) -> dict:
        return {
            "id": r.sticker_id, "ok": r.ok, "message": r.message,
            "archived": r.archived, "issues": r.issues, "size_kb": r.size_kb,
        }

    def _import_context():
        cfg_ = current_config()
        cfg_.ensure_output_dirs()
        style = TextStyle.from_config(cfg_)
        logger = RunLogger(cfg_.log_path, echo=False)
        state = StateStore(cfg_.state_path)
        by_id = {e.index: e for e in entries_or_error()}
        return cfg_, style, logger, state, by_id

    @app.post("/api/stickers/<sticker_id>/upload")
    def api_sticker_upload(sticker_id: str):
        """1枚分の画像を取り込み、完成画像まで作ります。"""
        if jobs.is_running():
            return jsonify({"error": "ほかの処理が実行中です。終わってから取り込んでください"}), 409
        file = request.files.get("file")
        if file is None or not file.filename:
            return jsonify({"error": "ファイルが選ばれていません"}), 400
        try:
            cfg_, style, logger, state, by_id = _import_context()
        except (CsvLoadError, FontNotFoundError) as exc:
            return jsonify({"error": str(exc)}), 400

        entry = by_id.get(int(sticker_id)) if sticker_id.isdigit() else None
        if entry is None:
            return jsonify({"error": f"IDが見つかりません: {sticker_id}"}), 404

        result = importer.import_image(cfg_, entry, file.read(), style, logger, state)
        body = _import_result_dict(result)
        body["sticker"] = sticker_status(cfg_, entry)
        # 画像として読めない等、取り込み自体が失敗したときだけ 400 にします。
        failed_to_store = not result.ok and not (cfg_.dir_generated / f"{entry.id}.png").exists()
        return jsonify(body), (400 if failed_to_store else 200)

    @app.post("/api/stickers/import")
    def api_stickers_import():
        """複数の画像をまとめて取り込みます。ファイル名の数字をIDとして使います。"""
        if jobs.is_running():
            return jsonify({"error": "ほかの処理が実行中です。終わってから取り込んでください"}), 409
        files = request.files.getlist("files")
        if not files:
            return jsonify({"error": "ファイルが選ばれていません"}), 400
        try:
            cfg_, style, logger, state, by_id = _import_context()
        except (CsvLoadError, FontNotFoundError) as exc:
            return jsonify({"error": str(exc)}), 400

        results, skipped = [], []
        for f in files:
            num = importer.id_from_filename(f.filename or "")
            entry = by_id.get(num) if num is not None else None
            if entry is None:
                skipped.append({
                    "file": f.filename,
                    "reason": "ファイル名からIDが分かりません（例: 001.png）"
                    if num is None else f"CSVにID {num:03d} がありません",
                })
                continue
            r = importer.import_image(cfg_, entry, f.read(), style, logger, state)
            d = _import_result_dict(r)
            d["file"] = f.filename
            results.append(d)

        return jsonify({
            "imported": sum(1 for r in results if r["ok"]),
            "results": results,
            "skipped": skipped,
            "stickers": statuses(cfg_, entries_or_error()),
        })

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
        errors = sum(len(r.errors) for r in reports)
        warnings = sum(len(r.warnings) for r in reports)
        # 再起動しても「検証済み」が分かるように結果を残します（画像が変われば自動で無効）
        validation = vd.record_result(cfg_, len(reports), errors, warnings)
        return jsonify(
            {
                "checked": len(reports),
                "errors": errors,
                "warnings": warnings,
                "items": items,
                "validation": validation,
            }
        )

    def _package_options(body: dict) -> dict:
        """画面から来た「セットの作り方」を build_packages の引数に変換します。"""
        set_size = body.get("set_size")
        ids = body.get("ids")
        return {
            "set_size": int(set_size) if str(set_size or "").strip().isdigit() else None,
            "ids": [str(i) for i in ids] if isinstance(ids, list) else None,
        }

    # ---------- 販売状況 ----------
    @app.post("/api/sales")
    def api_sales():
        """選んだスタンプに「申請中」「販売中」などの印を付けます。"""
        cfg_ = current_config()
        body = request.get_json(silent=True) or {}
        ids = [str(i) for i in body.get("ids") or []]
        if not ids:
            return jsonify({"error": "スタンプが選ばれていません"}), 400
        try:
            sales = sales_mod.set_status(cfg_, ids, str(body.get("status", "")))
        except sales_mod.SalesError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"sales": sales, "labels": sales_mod.STATUS_LABELS})

    # ---------- ローカルLLM（文章づくりの補助。料金なし） ----------
    def _character_text(cfg_, body: dict) -> str:
        """画面の説明文（未保存でも）を優先し、無ければ保存済みの説明文を使います。"""
        text = str(body.get("character") or "").strip()
        if not text and cfg_.master_prompt_ja_path.exists():
            text = cfg_.master_prompt_ja_path.read_text(encoding="utf-8").strip()
        return text[:1000]

    def _llm_call(fn):
        try:
            return fn()
        except llm_mod.LLMError as exc:
            return jsonify({"error": str(exc)}), 502

    @app.get("/api/llm/status")
    def api_llm_status():
        return jsonify(llm_mod.LocalLLM(current_config()).status())

    @app.post("/api/llm/phrases")
    def api_llm_phrases():
        """テーマに合うセリフの案を作ります（CSVには保存しません）。"""
        cfg_ = current_config()
        body = request.get_json(silent=True) or {}
        try:
            entries = entries_or_error()
        except CsvLoadError:
            entries = []
        examples = [{"text": e.text, "action": e.action, "expression": e.expression,
                     "category": e.category} for e in entries[:4]]

        def run():
            rows = llm_mod.suggest_phrases(
                llm_mod.LocalLLM(cfg_), theme=str(body.get("theme", "")).strip()[:100],
                count=int(body.get("count", 10) or 10), character=_character_text(cfg_, body),
                existing=[e.text for e in entries], examples=examples)
            return jsonify({"rows": rows})
        return _llm_call(run)

    @app.post("/api/llm/fill")
    def api_llm_fill():
        """セリフに合うポーズ・表情を作ります（CSVには保存しません）。"""
        cfg_ = current_config()
        body = request.get_json(silent=True) or {}
        texts = [str(t)[:40] for t in (body.get("texts") or []) if str(t).strip()][:100]
        if not texts:
            return jsonify({"error": "セリフがありません"}), 400
        return _llm_call(lambda: jsonify({"results": llm_mod.fill_pose(
            llm_mod.LocalLLM(cfg_), texts, character=_character_text(cfg_, body))}))

    @app.post("/api/llm/listing")
    def api_llm_listing():
        """申請用のタイトル・説明文を作ります（保存はしません）。"""
        cfg_ = current_config()
        body = request.get_json(silent=True) or {}
        try:
            entries = entries_or_error()
        except CsvLoadError as exc:
            return jsonify({"error": str(exc)}), 400
        ids = {str(i) for i in body.get("ids") or []}
        if ids:
            entries = [e for e in entries if e.id in ids] or entries
        try:
            volume = max(1, int(body.get("volume") or 1))
        except (TypeError, ValueError):
            volume = 1

        def run():
            result = llm_mod.write_listing(llm_mod.LocalLLM(cfg_), character=_character_text(cfg_, body),
                                           texts=[e.text for e in entries], volume=volume)
            return jsonify({"listing": result, "issues": listing_mod.check_listing(result)})
        return _llm_call(run)

    @app.post("/api/llm/polish")
    def api_llm_polish():
        """キャラクターの説明文を、絵にしやすい文章に整えます（保存はしません）。"""
        cfg_ = current_config()
        body = request.get_json(silent=True) or {}
        text = _character_text(cfg_, body)
        if not text:
            return jsonify({"error": "キャラクターの説明文がありません"}), 400
        return _llm_call(lambda: jsonify({"text": llm_mod.polish_character(llm_mod.LocalLLM(cfg_), text)}))

    # ---------- 申請用のタイトル・説明文 ----------
    def _listing_payload(data: dict) -> dict:
        return {
            "listing": data,
            "issues": listing_mod.check_listing(data),
            "limits": listing_mod.LIMITS,
            "labels": listing_mod.FIELD_LABELS,
        }

    @app.get("/api/listing")
    def api_listing_get():
        return jsonify(_listing_payload(listing_mod.load_listing(current_config())))

    @app.put("/api/listing")
    def api_listing_put():
        body = request.get_json(silent=True) or {}
        data = listing_mod.save_listing(current_config(), body.get("listing") or {})
        return jsonify(_listing_payload(data))

    @app.post("/api/listing/check")
    def api_listing_check():
        body = request.get_json(silent=True) or {}
        return jsonify({"issues": listing_mod.check_listing(body.get("listing") or {})})

    @app.post("/api/listing/suggest")
    def api_listing_suggest():
        """キャラの特徴とセリフから、タイトル・説明文の案を作ります（APIは使いません）。"""
        cfg_ = current_config()
        body = request.get_json(silent=True) or {}
        try:
            entries = entries_or_error()
        except CsvLoadError as exc:
            return jsonify({"error": str(exc)}), 400
        ids = {str(i) for i in body.get("ids") or []}
        if ids:
            entries = [e for e in entries if e.id in ids] or entries
        profile = cprof.load_profile(cfg_.character_profile_path) or {}
        creator = str(body.get("creator") or listing_mod.load_listing(cfg_).get("creator", ""))
        try:
            volume = max(1, int(body.get("volume") or 1))
        except (TypeError, ValueError):
            volume = 1
        candidates = listing_mod.suggest(profile, entries, creator=creator, volume=volume)
        return jsonify({
            "candidates": [{**c, "issues": listing_mod.check_listing(c)} for c in candidates],
            "count": len(entries),
            "has_profile": bool(profile),
        })

    @app.post("/api/package/plan")
    def api_package_plan():
        """ZIPを作る前に、何セットに分かれるか・何枚余るかを返します（ファイルは書きません）。"""
        try:
            opts = _package_options(request.get_json(silent=True) or {})
            plan = pkg.plan_packages(current_config(), entries_or_error(), **opts)
        except (pkg.PackageError, CsvLoadError) as exc:
            return jsonify({"error": str(exc), "sets": [], "leftover": [], "missing": [], "total": 0})
        return jsonify(plan.to_dict())

    @app.post("/api/package")
    def api_package():
        cfg_ = current_config()
        try:
            opts = _package_options(request.get_json(silent=True) or {})
            entries = entries_or_error()
            # 作れない指定なら、main/tab も作らずに理由だけ返します。
            plan = pkg.plan_packages(cfg_, entries, **opts)
            if plan.error:
                raise pkg.PackageError(plan.error)
            main_path, main_size = pkg.build_main_image(cfg_)
            tab_path, tab_size = pkg.build_tab_image(cfg_)
            results = pkg.build_packages(cfg_, entries, **opts)
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
                "packages_status": packages_status(cfg_),
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


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def is_loopback(host: str) -> bool:
    """このPCからしか接続できないアドレスかどうか。"""
    return host in LOOPBACK_HOSTS


def startup_banner(host: str, port: int) -> list[str]:
    """起動時に表示する案内。

    Flask 既定の「This is a development server...」は、公開Webサイトの運用に
    使うなという一般的な注意書きで、ローカル専用のこのツールには当てはまりません。
    代わりに、実際に注意が必要な場合（外部から接続できるアドレスを指定したとき）
    だけ具体的な警告を出します。
    """
    line = "=" * 66
    banner = [
        line,
        "  LINEスタンプ生成 GUI",
        f"  http://{host}:{port}/",
        "",
    ]
    if is_loopback(host):
        banner.append("  このPCからのみ接続できます（外部には公開されていません）。")
    else:
        banner += [
            "  ⚠ 警告: このアドレスは同じネットワーク上の他の端末からも接続できます。",
            "     GUIからは画像生成API（課金）を実行できるため、",
            "     共有ネットワークでの使用は推奨しません。",
            "     通常は --host を付けず 127.0.0.1 のまま使ってください。",
        ]
    banner += ["  終了するには Ctrl+C を押してください。", line]
    return banner


def run_server(config, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    """GUI サーバーを起動します（既定ではこのPC専用）。"""
    # Flask は app.run() の際に CWD から .env を探して読み込みます。
    # どの .env を使うかは load_config() 側で決めたいので、その挙動は止めます。
    os.environ.setdefault("FLASK_SKIP_DOTENV", "1")

    from werkzeug.serving import make_server

    app = create_app(config)
    url = f"http://{host}:{port}/"

    # app.run() ではなく make_server を使います。動作は同じですが、
    # 本番運用向けの定型警告バナーが出ないぶん、案内が読みやすくなります。
    server = make_server(host, port, app, threaded=True)

    for row in startup_banner(host, port):
        print(row, flush=True)

    if open_browser:
        import webbrowser

        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n終了しました。生成済みの画像はすべて残っています。")
    finally:
        server.server_close()
