"""output/gallery.html を生成します（100枚の一覧確認用）。"""

from __future__ import annotations

import html
import os
from pathlib import Path

_TEMPLATE = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LINEスタンプ プレビュー</title>
<style>
  :root {{
    --bg: #f4f5f7; --card: #ffffff; --fg: #1b1c1e; --muted: #6b7078;
    --line: #e2e4e8; --accent: #06c755;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #17181b; --card: #212328; --fg: #f0f1f3; --muted: #9aa0a8;
      --line: #32353c;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 24px 16px 64px; background: var(--bg); color: var(--fg);
    font-family: "Yu Gothic UI", "Hiragino Sans", "Noto Sans JP", system-ui, sans-serif;
  }}
  header {{ max-width: 1200px; margin: 0 auto 24px; }}
  h1 {{ font-size: 22px; margin: 0 0 8px; }}
  .meta {{ color: var(--muted); font-size: 13px; line-height: 1.7; }}
  .grid {{
    max-width: 1200px; margin: 0 auto; display: grid; gap: 14px;
    grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));
  }}
  .card {{
    background: var(--card); border: 1px solid var(--line); border-radius: 12px;
    padding: 10px; text-align: center; cursor: zoom-in;
  }}
  .card:hover {{ border-color: var(--accent); }}
  .thumb {{
    width: 100%; height: 140px; display: flex; align-items: center;
    justify-content: center; border-radius: 8px;
    background-image:
      linear-gradient(45deg, #d9dce1 25%, transparent 25%),
      linear-gradient(-45deg, #d9dce1 25%, transparent 25%),
      linear-gradient(45deg, transparent 75%, #d9dce1 75%),
      linear-gradient(-45deg, transparent 75%, #d9dce1 75%);
    background-size: 14px 14px;
    background-position: 0 0, 0 7px, 7px -7px, -7px 0;
  }}
  .thumb img {{ max-width: 100%; max-height: 140px; }}
  .missing {{ color: var(--muted); font-size: 12px; }}
  .id {{ color: var(--muted); font-size: 11px; margin-top: 8px; }}
  .text {{ font-size: 13px; font-weight: 700; margin-top: 2px; word-break: break-all; }}
  .badge {{
    display: inline-block; font-size: 10px; padding: 1px 6px; border-radius: 99px;
    border: 1px solid var(--line); color: var(--muted); margin-top: 6px;
  }}
  #lightbox {{
    position: fixed; inset: 0; background: rgba(0,0,0,.82); display: none;
    align-items: center; justify-content: center; flex-direction: column;
    gap: 16px; cursor: zoom-out; padding: 24px; z-index: 10;
  }}
  #lightbox.open {{ display: flex; }}
  #lightbox img {{
    max-width: min(92vw, 740px); max-height: 74vh; image-rendering: auto;
    background-image:
      linear-gradient(45deg, #888 25%, transparent 25%),
      linear-gradient(-45deg, #888 25%, transparent 25%),
      linear-gradient(45deg, transparent 75%, #888 75%),
      linear-gradient(-45deg, transparent 75%, #888 75%);
    background-size: 20px 20px;
    background-position: 0 0, 0 10px, 10px -10px, -10px 0;
  }}
  #lightbox .cap {{ color: #fff; font-size: 16px; font-weight: 700; text-align: center; }}
</style>
</head>
<body>
<header>
  <h1>LINEスタンプ プレビュー</h1>
  <div class="meta">
    全 {total} 件 / 画像あり {present} 件 / 未生成 {absent} 件<br>
    生成日時: {generated_at}<br>
    画像をクリックすると拡大表示します。
  </div>
</header>
<div class="grid">
{cards}
</div>
<div id="lightbox"><img id="lb-img" alt=""><div class="cap" id="lb-cap"></div></div>
<script>
  const box = document.getElementById('lightbox');
  const img = document.getElementById('lb-img');
  const cap = document.getElementById('lb-cap');
  document.querySelectorAll('.card[data-src]').forEach(function (el) {{
    el.addEventListener('click', function () {{
      img.src = el.dataset.src;
      img.alt = el.dataset.caption;
      cap.textContent = el.dataset.caption;
      box.classList.add('open');
    }});
  }});
  box.addEventListener('click', function () {{ box.classList.remove('open'); }});
  document.addEventListener('keydown', function (e) {{
    if (e.key === 'Escape') box.classList.remove('open');
  }});
</script>
</body>
</html>
"""


def _rel(path: Path, base: Path) -> str:
    return os.path.relpath(path, base).replace("\\", "/")


def build_gallery(config, entries) -> Path:
    """完成画像（無ければ原画）のサムネイル一覧HTMLを生成します。"""
    from datetime import datetime

    out = config.gallery_path
    base = out.parent
    out.parent.mkdir(parents=True, exist_ok=True)

    cards: list[str] = []
    present = 0

    for entry in entries:
        final_png = config.dir_final / f"{entry.id}.png"
        raw_png = config.dir_generated / f"{entry.id}.png"
        src: Path | None = None
        badge = ""
        if final_png.exists():
            src, badge = final_png, "final"
        elif raw_png.exists():
            src, badge = raw_png, "原画のみ"

        label = html.escape(f"{entry.id} {entry.text}")
        if src is None:
            cards.append(
                f'<div class="card">'
                f'<div class="thumb"><span class="missing">未生成</span></div>'
                f'<div class="id">{html.escape(entry.id)}</div>'
                f'<div class="text">{html.escape(entry.text)}</div>'
                f'<div class="badge">未生成</div>'
                f"</div>"
            )
            continue

        present += 1
        rel = html.escape(_rel(src, base))
        cards.append(
            f'<div class="card" data-src="{rel}" data-caption="{label}">'
            f'<div class="thumb"><img loading="lazy" src="{rel}" alt="{label}"></div>'
            f'<div class="id">{html.escape(entry.id)}</div>'
            f'<div class="text">{html.escape(entry.text)}</div>'
            f'<div class="badge">{html.escape(badge)}</div>'
            f"</div>"
        )

    total = len(entries)
    out.write_text(
        _TEMPLATE.format(
            total=total,
            present=present,
            absent=total - present,
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            cards="\n".join(cards),
        ),
        encoding="utf-8",
    )
    return out
