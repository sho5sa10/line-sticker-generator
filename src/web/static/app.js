/* LINEスタンプ生成 GUI のフロントエンド。
   バックエンドは CLI と同じパイプラインを呼ぶだけなので、結果は常に一致します。 */
'use strict';

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  stickers: [],
  selected: new Set(),
  info: null,
  eventOffset: 0,
  poller: null,
  csvDirty: false,
};

/* ------------------------------------------------------------------ */
/* 共通ユーティリティ                                                  */
/* ------------------------------------------------------------------ */
function toast(message, bad = false) {
  const el = $('#toast');
  el.textContent = message;
  el.classList.toggle('bad', bad);
  el.classList.add('show');
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.remove('show'), 3200);
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  if (!res.ok) {
    throw new Error((data && data.error) || `${res.status} ${res.statusText}`);
  }
  return data;
}

function openLightbox(src, caption) {
  $('#lb-img').src = src;
  $('#lb-cap').textContent = caption;
  $('#lightbox').classList.add('open');
}
$('#lightbox').addEventListener('click', () => $('#lightbox').classList.remove('open'));
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') $('#lightbox').classList.remove('open');
});

/* ------------------------------------------------------------------ */
/* タブ                                                                */
/* ------------------------------------------------------------------ */
$('#tabs').addEventListener('click', (e) => {
  const tab = e.target.closest('.tab');
  if (!tab) return;
  $$('.tab').forEach((t) => t.classList.toggle('active', t === tab));
  $$('.panel').forEach((p) => p.classList.toggle('active', p.id === `panel-${tab.dataset.tab}`));
  if (tab.dataset.tab === 'design') refreshPreview();
  if (tab.dataset.tab === 'output') loadAssets();
});

/* ------------------------------------------------------------------ */
/* 起動時ロード                                                        */
/* ------------------------------------------------------------------ */
async function loadState() {
  const info = await api('/api/state');
  state.info = info;
  state.stickers = info.stickers;
  renderChips(info);
  renderGrid();
  renderCsvTable();
  fillDesignControls(info.font);
  fillDesignTargets();
  if (info.job && info.job.status === 'running') startPolling();
  const notes = [];
  if (info.csv_error) notes.push(`CSVエラー: ${info.csv_error}`);
  if (info.font_error) notes.push(`フォント: ${info.font_error}`);
  if (!info.master_ok) notes.push('マスター画像がありません。`python -m src.main init-character` か、自分で用意した透過PNGを data/character/character_master.png に置いてください。');
  if (!info.api_key_set) notes.push('APIキーが未設定です（.env の OPENAI_API_KEY）。dry-run と文字合成だけなら実行できます。');
  $('#grid-note').innerHTML = notes.map((n) => `⚠ ${n}`).join('<br>');
}

function renderChips(info) {
  const s = info.line_spec;
  const chips = [
    { label: `CSV ${info.stickers.length}件`, ok: !info.csv_error },
    { label: info.font_error ? 'フォント未検出' : `フォント ${info.font_path.split(/[\\/]/).pop()}`, ok: !info.font_error },
    { label: info.master_ok ? 'マスター画像 OK' : 'マスター画像なし', ok: info.master_ok },
    { label: info.api_key_set ? 'APIキー 設定済み' : 'APIキー 未設定', ok: info.api_key_set },
    { label: `${info.model} / ${info.quality}`, ok: null },
    { label: `${s.sticker[0]}×${s.sticker[1]} · 余白${s.margin}px · ${s.valid_set_sizes.join('/')}枚`, ok: null },
  ];
  $('#status-chips').innerHTML = chips
    .map((c) => `<span class="chip ${c.ok === null ? '' : c.ok ? 'ok' : 'ng'}">${escapeHtml(c.label)}</span>`)
    .join('');
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/* ------------------------------------------------------------------ */
/* スタンプ一覧                                                        */
/* ------------------------------------------------------------------ */
function renderGrid() {
  const html = state.stickers.map((s) => {
    const sel = state.selected.has(s.id);
    let thumb = '<span class="empty">未生成</span>';
    let tag = '';
    if (s.has_final) {
      thumb = `<img loading="lazy" src="/img/final/${s.id}.png?t=${s.final_mtime}" alt="${escapeHtml(s.text)}">`;
      tag = '<span class="tag final">完成</span>';
    } else if (s.has_raw) {
      thumb = `<img loading="lazy" src="/img/generated/${s.id}.png?t=${s.raw_mtime}" alt="${escapeHtml(s.text)}">`;
      tag = '<span class="tag raw">原画のみ</span>';
    }
    return `
      <div class="cell ${sel ? 'selected' : ''}" data-id="${s.id}">
        <input class="pick" type="checkbox" ${sel ? 'checked' : ''} aria-label="選択">
        ${tag}
        <div class="thumb" data-zoom="${s.id}">${thumb}</div>
        <div class="cid">${s.id}${s.size_kb ? ` · ${s.size_kb}KB` : ''}</div>
        <div class="ctext">${escapeHtml(s.text)}</div>
        <div class="meta">${escapeHtml(s.action || '')}</div>
        <div class="rowbtns">
          <button class="btn" data-act="regen" title="この1枚だけAPIで作り直します（課金されます）">作り直す</button>
          <button class="btn" data-act="rerender" title="APIを使わず文字だけ再合成します">文字のみ</button>
        </div>
      </div>`;
  }).join('');
  $('#grid').innerHTML = html;
  updateSelectionUi();
}

$('#grid').addEventListener('click', async (e) => {
  const cell = e.target.closest('.cell');
  if (!cell) return;
  const id = cell.dataset.id;
  const sticker = state.stickers.find((s) => s.id === id);

  if (e.target.classList.contains('pick')) {
    if (e.target.checked) state.selected.add(id); else state.selected.delete(id);
    cell.classList.toggle('selected', e.target.checked);
    updateSelectionUi();
    return;
  }
  const zoom = e.target.closest('[data-zoom]');
  if (zoom) {
    if (!sticker.has_final && !sticker.has_raw) return;
    const src = sticker.has_final
      ? `/img/final/${id}.png?t=${sticker.final_mtime}`
      : `/img/generated/${id}.png?t=${sticker.raw_mtime}`;
    openLightbox(src, `${id} ${sticker.text}`);
    return;
  }
  const act = e.target.dataset.act;
  if (act === 'regen') {
    const cost = await costFor(1);
    if (!confirm(`${id}「${sticker.text}」をAPIで作り直します。\n概算コスト: ${cost}\n\n実行しますか？`)) return;
    runGenerate([id], { force: true });
  } else if (act === 'rerender') {
    runRender([id]);
  }
});

$('.toolbar').addEventListener('click', (e) => {
  const mode = e.target.dataset.select;
  if (!mode) return;
  if (mode === 'all') state.stickers.forEach((s) => state.selected.add(s.id));
  else if (mode === 'none') state.selected.clear();
  else if (mode === 'missing') {
    state.selected.clear();
    state.stickers.filter((s) => !s.has_raw).forEach((s) => state.selected.add(s.id));
  } else if (mode === 'invert') {
    const next = new Set();
    state.stickers.forEach((s) => { if (!state.selected.has(s.id)) next.add(s.id); });
    state.selected = next;
  }
  renderGrid();
});

async function costFor(count) {
  if (!count) return '—';
  try {
    const d = await api(`/api/cost?count=${count}`);
    return d.usd === null ? '不明' : `約 $${d.usd.toFixed(2)} USD (${d.model} / ${d.quality})`;
  } catch (e) { return '不明'; }
}

async function updateSelectionUi() {
  const n = state.selected.size;
  $('#selection-count').textContent = `${n}枚選択中`;
  $('#btn-generate').disabled = n === 0;
  $('#btn-render').disabled = n === 0;

  const force = $('#opt-force').checked;
  const willCall = force
    ? n
    : state.stickers.filter((s) => state.selected.has(s.id) && !s.has_raw).length;
  $('#cost-label').textContent = willCall === 0
    ? 'API呼び出し 0枚（すべてキャッシュ）'
    : `API ${willCall}枚 / ${await costFor(willCall)}`;
}
$('#opt-force').addEventListener('change', updateSelectionUi);

$('#btn-generate').addEventListener('click', async () => {
  const ids = [...state.selected].sort();
  const dry = $('#opt-dryrun').checked;
  const force = $('#opt-force').checked;
  if (!dry) {
    const willCall = force ? ids.length : ids.filter((id) => !state.stickers.find((s) => s.id === id).has_raw).length;
    const cost = await costFor(willCall);
    if (!confirm(
      `${ids.length}枚を処理します。\nうちAPI呼び出し: ${willCall}枚\n概算コスト: ${cost}\n\n` +
      `※ 既に原画がある分は課金されません（強制再生成を除く）。\n実行しますか？`)) return;
  }
  runGenerate(ids, { force, dry_run: dry });
});

$('#btn-render').addEventListener('click', () => runRender([...state.selected].sort()));

/* ------------------------------------------------------------------ */
/* ジョブ実行と進捗                                                    */
/* ------------------------------------------------------------------ */
async function runGenerate(ids, opts = {}) {
  try {
    resetJobUi(opts.dry_run ? 'dry-run（APIを呼びません）' : '画像を生成しています');
    await api('/api/generate', { method: 'POST', body: { ids, ...opts } });
    startPolling();
  } catch (e) { toast(e.message, true); closeJobBar(); }
}

async function runRender(ids) {
  try {
    resetJobUi('文字を再合成しています（APIを呼びません）');
    await api('/api/render', { method: 'POST', body: { ids } });
    startPolling();
  } catch (e) { toast(e.message, true); closeJobBar(); }
}

function resetJobUi(title) {
  state.eventOffset = 0;
  $('#job-title').textContent = title;
  $('#job-log').innerHTML = '';
  $('#job-fill').style.width = '0%';
  $('#job-api').textContent = '';
  $('#btn-cancel').style.display = '';
  $('#job-bar').classList.add('open');
}
function closeJobBar() { $('#job-bar').classList.remove('open'); }
$('#btn-job-close').addEventListener('click', closeJobBar);
$('#btn-cancel').addEventListener('click', async () => {
  await api('/api/job/cancel', { method: 'POST' });
  toast('中止をリクエストしました。実行中の1枚が終わり次第停止します。');
});

function startPolling() {
  if (state.poller) clearInterval(state.poller);
  state.poller = setInterval(pollJob, 700);
  pollJob();
}

async function pollJob() {
  let data;
  try { data = await api(`/api/job?since=${state.eventOffset}`); }
  catch (e) { return; }
  const job = data.job;
  if (!job) return;

  state.eventOffset = job.event_offset;
  const pct = job.total ? Math.round((job.done / job.total) * 100) : 0;
  $('#job-progress').textContent = `${job.done} / ${job.total}`;
  $('#job-fill').style.width = `${pct}%`;
  $('#job-api').textContent = job.api_calls ? `API呼び出し ${job.api_calls}回` : 'API呼び出し 0回';

  const log = $('#job-log');
  job.events.forEach((ev) => {
    const div = document.createElement('div');
    div.className = ev.level;
    div.textContent = `${ev.time} ${ev.id ? ev.id + ' ' : ''}${ev.message}`;
    log.appendChild(div);
  });
  if (job.events.length) log.scrollTop = log.scrollHeight;

  if (job.status !== 'running') {
    clearInterval(state.poller);
    state.poller = null;
    $('#btn-cancel').style.display = 'none';
    $('#job-title').textContent = {
      finished: '完了しました', failed: '失敗しました', cancelled: '中止しました',
    }[job.status] || job.status;
    toast(job.status === 'finished' ? '処理が完了しました' : `処理が${$('#job-title').textContent}`,
          job.status === 'failed');
    await refreshStickers();
  }
}

async function refreshStickers() {
  const d = await api('/api/stickers');
  state.stickers = d.stickers;
  renderGrid();
}

/* ------------------------------------------------------------------ */
/* セリフCSV編集                                                       */
/* ------------------------------------------------------------------ */
function renderCsvTable() {
  $('#csv-body').innerHTML = state.stickers.map((s) => `
    <tr data-id="${s.id}">
      <td><input data-f="id" value="${escapeHtml(s.id)}"></td>
      <td><input data-f="text" value="${escapeHtml(s.text)}"></td>
      <td><input data-f="action" value="${escapeHtml(s.action)}"></td>
      <td><input data-f="expression" value="${escapeHtml(s.expression)}"></td>
      <td><input data-f="category" value="${escapeHtml(s.category)}"></td>
      <td><button class="btn small" data-act="del">削除</button></td>
    </tr>`).join('');
  $('#csv-count').textContent = `${state.stickers.length}件`;
}

$('#csv-body').addEventListener('input', (e) => {
  if (e.target.tagName !== 'INPUT') return;
  e.target.closest('tr').classList.add('dirty');
  state.csvDirty = true;
});

$('#csv-body').addEventListener('click', (e) => {
  if (e.target.dataset.act !== 'del') return;
  if (!confirm('この行を削除しますか？（保存するまでCSVは変更されません）')) return;
  e.target.closest('tr').remove();
  state.csvDirty = true;
  $('#csv-count').textContent = `${$$('#csv-body tr').length}件`;
});

$('#btn-row-add').addEventListener('click', () => {
  const rows = $$('#csv-body tr');
  const maxId = rows.reduce((m, r) => Math.max(m, parseInt(r.querySelector('[data-f=id]').value, 10) || 0), 0);
  const nextId = String(maxId + 1).padStart(3, '0');
  const tr = document.createElement('tr');
  tr.className = 'dirty';
  tr.innerHTML = `
    <td><input data-f="id" value="${nextId}"></td>
    <td><input data-f="text" value=""></td>
    <td><input data-f="action" value=""></td>
    <td><input data-f="expression" value=""></td>
    <td><input data-f="category" value="basic"></td>
    <td><button class="btn small" data-act="del">削除</button></td>`;
  $('#csv-body').appendChild(tr);
  tr.querySelector('[data-f=text]').focus();
  state.csvDirty = true;
});

$('#btn-csv-save').addEventListener('click', async () => {
  const rows = $$('#csv-body tr').map((tr) => {
    const o = {};
    tr.querySelectorAll('input').forEach((i) => { o[i.dataset.f] = i.value; });
    return o;
  });
  try {
    const d = await api('/api/stickers', { method: 'POST', body: { stickers: rows } });
    state.stickers = d.stickers;
    state.csvDirty = false;
    renderCsvTable();
    renderGrid();
    fillDesignTargets();
    toast(`保存しました（${d.count}件）`);
  } catch (e) { toast(e.message, true); }
});

window.addEventListener('beforeunload', (e) => {
  if (state.csvDirty) { e.preventDefault(); e.returnValue = ''; }
});

/* ------------------------------------------------------------------ */
/* 文字デザイン                                                        */
/* ------------------------------------------------------------------ */
const FONT_FIELDS = ['size', 'min_size', 'stroke_width', 'max_lines', 'band_ratio', 'gap'];

function fillDesignControls(font) {
  FONT_FIELDS.forEach((k) => {
    const el = $(`#f-${k}`);
    if (el && font[k] !== undefined && font[k] !== null) {
      el.value = font[k];
      const badge = $(`#v-${k}`);
      if (badge) badge.textContent = font[k];
    }
  });
  $('#f-fill').value = font.fill || '#FFFFFF';
  $('#f-stroke_fill').value = font.stroke_fill || '#000000';
  $('#f-position').value = font.position || 'bottom';
}

function fillDesignTargets() {
  const withRaw = state.stickers.filter((s) => s.has_raw);
  const list = withRaw.length ? withRaw : state.stickers;
  $('#design-target').innerHTML = list
    .map((s) => `<option value="${s.id}">${s.id} ${escapeHtml(s.text)}</option>`)
    .join('');
}

function currentStyleOverrides() {
  const o = {};
  FONT_FIELDS.forEach((k) => {
    const el = $(`#f-${k}`);
    if (!el) return;
    o[k] = k === 'band_ratio' ? parseFloat(el.value) : parseInt(el.value, 10);
  });
  o.fill = $('#f-fill').value;
  o.stroke_fill = $('#f-stroke_fill').value;
  o.position = $('#f-position').value;
  return o;
}

let previewTimer = null;
function schedulePreview() {
  FONT_FIELDS.forEach((k) => {
    const badge = $(`#v-${k}`);
    if (badge) badge.textContent = $(`#f-${k}`).value;
  });
  clearTimeout(previewTimer);
  previewTimer = setTimeout(refreshPreview, 180);
}

async function refreshPreview() {
  const id = $('#design-target').value;
  if (!id) return;
  const body = { id, style: currentStyleOverrides(), text: $('#design-text').value.trim() };
  try {
    const res = await fetch('/api/preview-text', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      $('#design-error').textContent = d.error || 'プレビューを作成できませんでした';
      return;
    }
    $('#design-error').textContent = '';
    const url = URL.createObjectURL(await res.blob());
    ['#design-preview', '#design-preview-1x', '#design-preview-sm'].forEach((sel) => {
      const img = $(sel);
      if (img.dataset.url) URL.revokeObjectURL(img.dataset.url);
      img.src = url;
      img.dataset.url = url;
    });
  } catch (e) {
    $('#design-error').textContent = e.message;
  }
}

['#design-target', '#design-text', '#f-fill', '#f-stroke_fill', '#f-position']
  .forEach((sel) => $(sel).addEventListener('input', schedulePreview));
FONT_FIELDS.forEach((k) => $(`#f-${k}`).addEventListener('input', schedulePreview));

$('#btn-swap-colors').addEventListener('click', () => {
  const a = $('#f-fill').value;
  $('#f-fill').value = $('#f-stroke_fill').value;
  $('#f-stroke_fill').value = a;
  schedulePreview();
});

async function saveDesign() {
  const d = await api('/api/settings/font', { method: 'POST', body: currentStyleOverrides() });
  toast(`設定を保存しました: ${d.saved_to.split(/[\\/]/).pop()}`);
  return d;
}

$('#btn-design-save').addEventListener('click', async () => {
  try { await saveDesign(); } catch (e) { toast(e.message, true); }
});

$('#btn-design-apply').addEventListener('click', async () => {
  try {
    await saveDesign();
    const ids = state.stickers.filter((s) => s.has_raw).map((s) => s.id);
    if (!ids.length) { toast('原画がまだありません', true); return; }
    runRender(ids);
  } catch (e) { toast(e.message, true); }
});

/* ------------------------------------------------------------------ */
/* 検証・出力                                                          */
/* ------------------------------------------------------------------ */
$('#btn-validate').addEventListener('click', async () => {
  const box = $('#validate-result');
  box.textContent = '検証中…';
  try {
    const d = await api('/api/validate', { method: 'POST' });
    if (!d.checked) { box.innerHTML = '<div class="line">完成画像がまだありません。</div>'; return; }
    const lines = [];
    lines.push(`<div class="line"><b>${d.checked}件を検証 / エラー ${d.errors} / 警告 ${d.warnings}</b></div>`);
    if (!d.errors && !d.warnings) {
      lines.push('<div class="line good">すべてLINE仕様を満たしています。</div>');
    }
    d.items.forEach((it) => it.issues.forEach((i) =>
      lines.push(`<div class="line ${i.severity}">[${i.severity}] ${escapeHtml(it.file)}: ${escapeHtml(i.message)}</div>`)));
    box.innerHTML = lines.join('');
  } catch (e) { box.innerHTML = `<div class="line ERROR">${escapeHtml(e.message)}</div>`; }
});

$('#btn-package').addEventListener('click', async () => {
  const box = $('#package-result');
  box.textContent = '作成中…';
  try {
    const d = await api('/api/package', { method: 'POST' });
    const lines = [
      `<div class="line">main: ${d.main.size_kb}KB ${d.main.ok ? '<span class="good">OK</span>' : '<span class="ERROR">NG</span>'}` +
      ` / tab: ${d.tab.size_kb}KB ${d.tab.ok ? '<span class="good">OK</span>' : '<span class="ERROR">NG</span>'}</div>`,
    ];
    d.packages.forEach((p) => {
      if (p.downloadable) {
        lines.push(`<div class="line"><a href="/api/download/${encodeURIComponent(p.name)}" download>${escapeHtml(p.name)}</a>` +
                   ` — ${p.count}枚 / ${p.size_mb}MB</div>`);
      }
      p.warnings.forEach((w) => lines.push(`<div class="line WARNING">${escapeHtml(w)}</div>`));
    });
    box.innerHTML = lines.join('');
    loadAssets();
  } catch (e) { box.innerHTML = `<div class="line ERROR">${escapeHtml(e.message)}</div>`; }
});

function loadAssets() {
  const t = Date.now();
  $('#asset-main').src = `/img/main.png?t=${t}`;
  $('#asset-tab').src = `/img/tab.png?t=${t}`;
}

$('#btn-gallery').addEventListener('click', async () => {
  try {
    const d = await api('/api/gallery', { method: 'POST' });
    $('#gallery-result').innerHTML = `<div class="line good">生成しました</div><div class="line">${escapeHtml(d.path)}</div>`;
  } catch (e) { $('#gallery-result').innerHTML = `<div class="line ERROR">${escapeHtml(e.message)}</div>`; }
});

$('#btn-log').addEventListener('click', async () => {
  const d = await api('/api/log');
  $('#log-view').textContent = d.lines.join('\n') || '（ログはまだありません）';
  $('#log-view').scrollTop = $('#log-view').scrollHeight;
});

/* ------------------------------------------------------------------ */
loadState().catch((e) => toast(e.message, true));
