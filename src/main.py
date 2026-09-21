"""LINEスタンプ自動生成システムの CLI エントリポイント。

使い方:
    python -m src.main doctor
    python -m src.main preview
    python -m src.main generate --dry-run
    python -m src.main generate --id 001
    python -m src.main generate --start 1 --end 10
    python -m src.main render
    python -m src.main validate
    python -m src.main gallery
    python -m src.main package
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import gallery as gallery_mod
from . import image_processor as ip
from . import package_builder as pkg
from . import validator as vd
from .config import ConfigError, load_config
from .csv_loader import CsvLoadError, StickerEntry, filter_entries, load_stickers
from .image_generator import ImageGenerator, MasterImageMissingError, check_master_image
from .logger import RunLogger, StateStore
from .text_renderer import FontNotFoundError, TextStyle, render_text_image

EXIT_OK = 0
EXIT_ERROR = 1


# ======================================================================
# 共通ヘルパ
# ======================================================================
def _select_entries(config, args) -> list[StickerEntry]:
    entries = load_stickers(config.csv_path)
    ids = [args.id] if getattr(args, "id", None) else None
    return filter_entries(
        entries,
        ids=ids,
        start=getattr(args, "start", None),
        end=getattr(args, "end", None),
        limit=getattr(args, "limit", None),
    )


def _confirm(prompt: str) -> bool:
    if not sys.stdin.isatty():
        print("対話入力ができないため中止しました。--yes を付けて実行してください。")
        return False
    try:
        return input(f"{prompt} [y/N]: ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def render_final(config, entry: StickerEntry, style: TextStyle) -> tuple[Path, int, list[str]]:
    """原画 + セリフ → LINE規格の完成PNG。原画は変更しません。"""
    src = config.dir_generated / f"{entry.id}.png"
    canvas_w, canvas_h = config.sticker_size
    margin = config.margin
    gap = int(config.get("font.gap", 4))
    band_ratio = float(config.get("font.band_ratio", 0.40))
    position = str(config.get("font.position", "bottom"))

    character = ip.make_background_transparent(ip.load_rgba(src))

    max_text_w = canvas_w - margin * 2
    max_text_h = int((canvas_h - margin * 2) * band_ratio)
    text_img = render_text_image(entry.text, style, max_text_w, max_text_h)

    result = ip.compose_sticker(
        character,
        text_img,
        (canvas_w, canvas_h),
        margin,
        gap=gap,
        text_position=position,
    )
    out = config.dir_final / f"{entry.id}.png"
    path, size_bytes, save_warnings = ip.save_png(
        result.image, out, config.max_file_size_bytes
    )
    return path, size_bytes, result.warnings + save_warnings


# ======================================================================
# コマンド
# ======================================================================
def cmd_doctor(config, args) -> int:
    """実行環境と設定を点検します（APIは呼びません）。"""
    ok = True
    print("=== 環境チェック ===")
    print(f"Python           : {sys.version.split()[0]}")
    print(f"設定ファイル     : {config.path}")

    try:
        import PIL
        from PIL import Image

        print(f"Pillow           : {PIL.__version__}")
    except ImportError:
        Image = None
        print("Pillow           : NG (pip install -r requirements.txt)")
        ok = False

    # CSV
    try:
        entries = load_stickers(config.csv_path)
        print(f"CSV              : OK {len(entries)}件 ({config.csv_path})")
    except CsvLoadError as exc:
        print(f"CSV              : NG {exc}")
        ok = False

    # フォント
    try:
        style = TextStyle.from_config(config)
        print(f"日本語フォント   : OK {style.font_path}")
    except FontNotFoundError as exc:
        print(f"日本語フォント   : NG {exc}")
        ok = False

    # キャラクターマスター
    try:
        master = check_master_image(config)
        if Image is None:
            raise RuntimeError("Pillow が利用できません")
        with Image.open(master) as im:
            print(f"マスター画像     : OK {master} ({im.size[0]}x{im.size[1]}, {im.mode})")
    except MasterImageMissingError as exc:
        print(f"マスター画像     : NG\n{exc}")
        ok = False
    except Exception as exc:  # noqa: BLE001
        print(f"マスター画像     : NG 読み込めません ({exc})")
        ok = False

    # APIキー（値は表示しません）
    if config.api_key:
        print(f"APIキー          : OK (設定済み / {len(config.api_key)}文字)")
    else:
        print("APIキー          : 未設定 (.env の OPENAI_API_KEY)  ※dry-run は実行可能")

    print(f"プロバイダ       : {config.provider_name}")
    print(f"モデル           : {config.model}")
    print(f"画質             : {config.quality}")

    # LINE仕様
    w, h = config.sticker_size
    print("=== LINE仕様 (2026-09-21 公式確認) ===")
    print(f"スタンプ画像     : {w}x{h} px以内 / PNG / 透過 / 1MB以下 / 縦横偶数")
    print(f"メイン画像       : {config.main_size[0]}x{config.main_size[1]} px")
    print(f"タブ画像         : {config.tab_size[0]}x{config.tab_size[1]} px")
    print(f"1セットの枚数    : {config.get('package.valid_set_sizes')} のいずれか")
    print(f"余白             : {config.margin}px 以上")

    # 出力ディレクトリ
    config.ensure_output_dirs()
    print(f"出力ディレクトリ : OK ({config.root / 'output'})")

    print("\n結果: " + ("OK - 生成を開始できます" if ok else "NG - 上記を解消してください"))
    return EXIT_OK if ok else EXIT_ERROR


def cmd_init_character(config, args) -> int:
    """キャラクターマスター画像を1枚だけ生成します（API 1回 = 約$0.04）。

    既に画像がある場合は上書きしません（--force で上書き）。
    自分で用意した画像を data/character/character_master.png に置いても構いません。
    """
    from .prompt_generator import CONSISTENCY_RULE, NO_TEXT_RULE, load_master_prompt
    from .providers import create_provider

    out = config.master_image_path
    if out.exists() and not args.force:
        print(f"すでに存在します: {out}")
        print("作り直す場合は --force を付けてください。")
        return EXIT_OK

    prompt = "\n\n".join(
        [
            load_master_prompt(config.master_prompt_path),
            "POSE: standing straight and relaxed, facing forward, arms down naturally.\n"
            "FACIAL EXPRESSION: calm friendly smile.\n"
            "This is the reference sheet image that defines the character design.",
            CONSISTENCY_RULE,
            NO_TEXT_RULE,
        ]
    )

    if args.dry_run:
        print("[DRY-RUN] APIは呼び出しません。以下のプロンプトで生成します:")
        print("-" * 72)
        print(prompt)
        return EXIT_OK

    provider = create_provider(config)
    cost = provider.estimate_cost_usd(1)
    print(f"マスター画像を1枚生成します（概算 ${cost:.3f} USD / モデル={config.model}）")
    if not args.yes and not _confirm("実行しますか？"):
        print("中止しました。")
        return EXIT_OK

    out.parent.mkdir(parents=True, exist_ok=True)
    provider.generate(prompt=prompt, reference_image=None, output_path=str(out))
    print(f"生成しました: {out}")
    print("`python -m src.main doctor` で確認してください。")
    return EXIT_OK


def cmd_preview(config, args) -> int:
    """APIを呼ばずに、生成予定の内容とプロンプトを一覧表示します。"""
    entries = _select_entries(config, args)
    logger = RunLogger(config.log_path, echo=False)
    state = StateStore(config.state_path)
    generator = ImageGenerator(config, logger, state, dry_run=True)

    for entry in entries:
        prompt = generator.prompt_for(entry)
        exists = generator.output_path(entry).exists()
        print("=" * 72)
        print(f"ID        : {entry.id}{'  [生成済み]' if exists else ''}")
        print(f"セリフ    : {entry.text}")
        print(f"ポーズ    : {entry.action}")
        print(f"表情      : {entry.expression}")
        print(f"カテゴリ  : {entry.category}")
        if args.full:
            print("--- プロンプト ---")
            print(prompt)
        else:
            head = prompt.split("\n\n")[1] if "\n\n" in prompt else prompt
            print("--- プロンプト(抜粋) ---")
            print(head)

    print("=" * 72)
    pending = generator.pending_count(entries, force=False)
    print(f"対象 {len(entries)}件 / API呼び出し予定 {pending}件")
    _print_cost(generator, pending)
    if not args.full:
        print("完全なプロンプトを見るには --full を付けてください。")
    return EXIT_OK


def _print_cost(generator, pending: int) -> None:
    cost = generator.estimate_cost_usd(pending)
    if cost is None:
        print("概算コスト: 不明（APIキー未設定のため算出できません）")
        return
    print(
        f"概算コスト: 約 ${cost:.2f} USD "
        f"(モデル={generator.config.model}, 画質={generator.config.quality}, {pending}枚)"
    )
    print("※ 実際の請求額は OpenAI の最新単価に依存します。必ず公式の料金表をご確認ください。")


def cmd_generate(config, args) -> int:
    """画像生成 → セリフ合成 → LINE規格化 までを実行します。"""
    entries = _select_entries(config, args)
    if not entries:
        print("対象のスタンプがありません。")
        return EXIT_ERROR

    config.ensure_output_dirs()
    logger = RunLogger(config.log_path)
    state = StateStore(config.state_path)
    generator = ImageGenerator(config, logger, state, dry_run=args.dry_run)

    if args.dry_run:
        print(f"[DRY-RUN] APIは呼び出しません。対象 {len(entries)}件")
        for entry in entries:
            print("=" * 72)
            print(f"{entry.id} {entry.text} / {entry.action} / {entry.expression}")
            print(generator.prompt_for(entry))
        print("=" * 72)
        pending = generator.pending_count(entries, force=args.force)
        print(f"実行すると {pending}件のAPI呼び出しが発生します。")
        _print_cost(generator, pending)
        return EXIT_OK

    # --- マスター画像の存在確認（ここで止めてAPIを無駄撃ちしない） ---
    try:
        check_master_image(config)
    except MasterImageMissingError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    try:
        style = TextStyle.from_config(config)
    except FontNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    pending = generator.pending_count(entries, force=args.force)
    if pending > 0:
        print(f"対象 {len(entries)}件 / API呼び出し予定 {pending}件")
        _print_cost(generator, pending)
        if not args.yes and pending > 1 and not _confirm("生成を開始しますか？"):
            print("中止しました。")
            return EXIT_OK

    counts = {"generated": 0, "skipped": 0, "error": 0}
    failed: list[str] = []

    for entry in entries:
        result = generator.generate_one(entry, force=args.force)
        if result.status == "error":
            counts["error"] += 1
            failed.append(entry.id)
            continue
        counts[result.status] = counts.get(result.status, 0) + 1

        if args.no_render:
            continue
        try:
            path, size_bytes, warnings = render_final(config, entry, style)
            logger.event(entry.id, "TEXT RENDERED", f"{size_bytes / 1024:.0f}KB")
            for w in warnings:
                logger.warn(f"{entry.id} {w}")

            report = vd.validate_sticker(path, config)
            for issue in report.issues:
                logger.warn(f"{entry.id} {issue.message}")
            if report.ok:
                logger.event(entry.id, "VALIDATION PASS")
                logger.event(entry.id, "COMPLETE")
                state.set(entry.id, "complete")
            else:
                logger.event(entry.id, "VALIDATION FAIL", report.errors[0].message)
                state.set(entry.id, "validation_failed", report.errors[0].message)
                failed.append(entry.id)
        except Exception as exc:  # noqa: BLE001 - 1件の失敗で全体を止めない
            logger.error(entry.id, f"{type(exc).__name__}: {exc}")
            state.set(entry.id, "error", str(exc))
            counts["error"] += 1
            failed.append(entry.id)

    print("-" * 72)
    print(
        f"完了: 生成 {counts['generated']} / スキップ {counts['skipped']} "
        f"/ 失敗 {counts['error']}"
    )
    if failed:
        uniq = sorted(set(failed))
        print(f"失敗したID: {', '.join(uniq)}")
        print(f"再実行例  : python -m src.main generate --id {uniq[0]} --force")
        print(f"ログ      : {config.log_path}")
        return EXIT_ERROR
    return EXIT_OK


def cmd_render(config, args) -> int:
    """APIを呼ばず、既存の原画からセリフ合成のみやり直します。"""
    entries = _select_entries(config, args)
    config.ensure_output_dirs()
    logger = RunLogger(config.log_path)

    try:
        style = TextStyle.from_config(config)
    except FontNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    done = skipped = failed = 0
    for entry in entries:
        if not (config.dir_generated / f"{entry.id}.png").exists():
            skipped += 1
            continue
        try:
            path, size_bytes, warnings = render_final(config, entry, style)
            for w in warnings:
                logger.warn(f"{entry.id} {w}")
            logger.event(entry.id, "TEXT RENDERED", f"{path.name} {size_bytes / 1024:.0f}KB")
            done += 1
        except Exception as exc:  # noqa: BLE001
            logger.error(entry.id, f"{type(exc).__name__}: {exc}")
            failed += 1

    print(f"合成: {done}件 / 原画なしスキップ: {skipped}件 / 失敗: {failed}件")
    return EXIT_OK if failed == 0 else EXIT_ERROR


def cmd_validate(config, args) -> int:
    """完成画像がLINE仕様を満たすか検証します。"""
    entries = _select_entries(config, args) if (args.id or args.start or args.end) else None
    reports = vd.validate_all(config, entries)

    if not reports:
        print(f"検証対象がありません: {config.dir_final}")
        return EXIT_ERROR

    errors = [i for r in reports for i in r.errors]
    warnings = [i for r in reports for i in r.warnings]

    for issue in errors + warnings:
        print(str(issue))

    print("-" * 72)
    print(
        f"検証 {len(reports)}件 / エラー {len(errors)}件 / 警告 {len(warnings)}件"
    )
    if not errors:
        print("すべてLINE仕様を満たしています。")
    return EXIT_OK if not errors else EXIT_ERROR


def cmd_gallery(config, args) -> int:
    """output/gallery.html を生成します。"""
    entries = load_stickers(config.csv_path)
    path = gallery_mod.build_gallery(config, entries)
    print(f"ギャラリーを生成しました: {path}")
    print(f"ブラウザで開く: start {path}")
    return EXIT_OK


def cmd_package(config, args) -> int:
    """main/tab画像を作成し、LINE提出用ZIPを生成します。"""
    config.ensure_output_dirs()
    entries = _select_entries(config, args)

    main_path, main_size = pkg.build_main_image(config)
    print(f"main画像: {main_path} ({main_size / 1024:.0f}KB)")
    tab_path, tab_size = pkg.build_tab_image(config)
    print(f"tab画像 : {tab_path} ({tab_size / 1024:.0f}KB)")

    for report in (vd.validate_main(main_path, config), vd.validate_tab(tab_path, config)):
        for issue in report.issues:
            print(str(issue))

    results = pkg.build_packages(config, entries)
    print("-" * 72)
    for r in results:
        if r.size_bytes:
            print(f"{r.path.name}: {r.sticker_count}枚 / {r.size_bytes / 1024 / 1024:.2f}MB")
        for w in r.warnings:
            print(f"  {w}")
    return EXIT_OK


# ======================================================================
# 引数定義
# ======================================================================
def _add_selection_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--id", help="単一IDを指定 (例: 001)")
    p.add_argument("--start", type=int, help="開始ID(整数)")
    p.add_argument("--end", type=int, help="終了ID(整数)")
    p.add_argument("--limit", type=int, help="先頭から何件まで処理するか")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.main",
        description="LINEスタンプ自動生成システム",
    )
    parser.add_argument("--config", help="設定ファイルのパス")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="環境と設定を点検（APIを呼びません）")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser(
        "init-character", help="キャラクターマスター画像を1枚生成（API 1回）"
    )
    p.add_argument("--dry-run", action="store_true", help="APIを呼ばずプロンプトのみ表示")
    p.add_argument("--force", action="store_true", help="既存のマスター画像を上書きする")
    p.add_argument("--yes", "-y", action="store_true", help="確認をスキップする")
    p.set_defaults(func=cmd_init_character)

    p = sub.add_parser("preview", help="生成予定の一覧を表示（APIを呼びません）")
    _add_selection_args(p)
    p.add_argument("--full", action="store_true", help="プロンプト全文を表示")
    p.set_defaults(func=cmd_preview)

    p = sub.add_parser("generate", help="画像生成 → セリフ合成 → LINE規格化")
    _add_selection_args(p)
    p.add_argument("--dry-run", action="store_true", help="APIを呼ばずプロンプトのみ表示")
    p.add_argument("--force", action="store_true", help="既存画像があっても再生成する")
    p.add_argument("--yes", "-y", action="store_true", help="コスト確認をスキップする")
    p.add_argument("--no-render", action="store_true", help="原画生成のみでセリフ合成しない")
    p.set_defaults(func=cmd_generate)

    p = sub.add_parser("render", help="既存の原画からセリフ合成のみ再実行（APIを呼びません）")
    _add_selection_args(p)
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("validate", help="完成画像がLINE仕様を満たすか検証")
    _add_selection_args(p)
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("gallery", help="output/gallery.html を生成")
    p.set_defaults(func=cmd_gallery)

    p = sub.add_parser("package", help="main/tab画像とLINE提出用ZIPを生成")
    _add_selection_args(p)
    p.set_defaults(func=cmd_package)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows のコンソールで日本語が化けないようにします。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass

    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"設定エラー: {exc}", file=sys.stderr)
        return EXIT_ERROR

    try:
        return args.func(config, args)
    except (CsvLoadError, ConfigError, pkg.PackageError, FontNotFoundError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except MasterImageMissingError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("\n中断しました。生成済みの画像は残っているため、"
              "同じコマンドを再実行すれば途中から再開できます。")
        return EXIT_ERROR


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    raise SystemExit(main())
