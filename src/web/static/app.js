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
  validated: false,
  steps: [],
  pinnedStep: null,   // ユーザーが明示的に選んだステップ（自動判定より優先）
  master: null,
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
/* ヘッダーの高さをCSS変数へ反映（チップが折り返しても タブ が重ならない） */
/* ------------------------------------------------------------------ */
(function trackTopbarHeight() {
  const bar = document.querySelector('.topbar');
  const apply = () => document.documentElement.style.setProperty(
    '--topbar-h', `${Math.round(bar.getBoundingClientRect().height)}px`);
  apply();
  if (window.ResizeObserver) new ResizeObserver(apply).observe(bar);
  else window.addEventListener('resize', apply);
})();

/* ------------------------------------------------------------------ */
/* ツールチップ（data-tip 属性を持つ要素にホバーで説明を出す）          */
/* ------------------------------------------------------------------ */
(function setupTooltips() {
  const tip = $('#tip');
  let target = null;

  function place(el) {
    const r = el.getBoundingClientRect();
    tip.textContent = el.dataset.tip;
    tip.classList.add('show');
    const tr = tip.getBoundingClientRect();
    let left = r.left + r.width / 2 - tr.width / 2;
    left = Math.max(8, Math.min(left, window.innerWidth - tr.width - 8));
    // 下に入りきらなければ上に出す
    let top = r.bottom + 8;
    if (top + tr.height > window.innerHeight - 8) top = r.top - tr.height - 8;
    tip.style.left = `${left}px`;
    tip.style.top = `${Math.max(8, top)}px`;
  }

  document.addEventListener('mouseover', (e) => {
    const el = e.target.closest('[data-tip]');
    if (!el || el === target) return;
    target = el;
    place(el);
  });
  document.addEventListener('mouseout', (e) => {
    if (!target) return;
    if (e.relatedTarget && target.contains(e.relatedTarget)) return;
    target = null;
    tip.classList.remove('show');
  });
  window.addEventListener('scroll', () => { target = null; tip.classList.remove('show'); }, true);
})();

/* ------------------------------------------------------------------ */
/* 進め方ガイド                                                        */
/* ------------------------------------------------------------------ */
function readLS(key, fallback) {
  try { const v = localStorage.getItem(key); return v === null ? fallback : v; }
  catch (e) { return fallback; }
}
function writeLS(key, value) {
  try { localStorage.setItem(key, value); } catch (e) { /* プライベートモード等 */ }
}

function switchTab(name) {
  const tab = document.querySelector(`.tab[data-tab="${name}"]`);
  if (tab) tab.click();
}

/** 各ステップが属するタブ。ステップをクリックするとここへ移動します。 */
const STEP_TAB = {
  setup: 'start', character: 'start', try: 'grid', design: 'design',
  rest: 'grid', validate: 'output', package: 'output',
};

/** いまの状態から各ステップの達成状況を判定します。 */
function computeSteps(info) {
  const total = info.stickers.length;
  const raw = info.stickers.filter((s) => s.has_raw).length;
  const final = info.stickers.filter((s) => s.has_final).length;
  const ready = info.api_key_set && !info.font_error && !info.csv_error;

  return [
    {
      n: 1, key: 'setup', title: '準備を整える',
      desc: ready
        ? `APIキー・フォント・CSV(${total}件) すべてOK`
        : [!info.api_key_set && 'APIキー未設定', info.font_error && 'フォント未検出',
           info.csv_error && 'CSVエラー'].filter(Boolean).join(' / '),
      done: ready, blocked: !ready,
      action: { label: '準備の状態を見る', run: () => switchTab('start') },
      help: '.env に OPENAI_API_KEY を書いてから、このページを再読み込みしてください。',
    },
    {
      n: 2, key: 'character', title: 'キャラクターを決める',
      desc: info.master_ok
        ? 'マスター画像あり。差し替えれば別キャラのセットも作れます'
        : 'まだありません。画像を用意するか、AIに作ってもらえます',
      done: info.master_ok, blocked: false,
      action: { label: info.master_ok ? 'キャラクターを見直す' : 'キャラクターを用意する',
                run: () => switchTab('start') },
      help: '画像をアップロードするか、AIに1枚だけ作らせることができます。'
          + 'いまの画像は履歴として残るので、いつでも戻せます。',
    },
    {
      n: 3, key: 'try', title: 'まず1枚だけ試す',
      desc: raw ? `${raw}枚の画像ができています` : '001を1枚だけ作って絵柄を確認します',
      done: raw >= 1, blocked: false,
      action: { label: '001を1枚だけ生成', run: () => tryOne() },
    },
    {
      n: 4, key: 'design', title: '文字の見た目を整える',
      desc: '大きさ・色・縁取りをその場で確認できます（APIを使わないので無料）'
        + (raw ? '' : '。生成前でもマスター画像で試せます'),
      done: final >= 1, blocked: false, optional: true,
      action: { label: '文字デザインを開く', run: () => switchTab('design') },
    },
    {
      n: 5, key: 'rest', title: '残りをまとめて生成',
      desc: raw >= total && total
        ? `${total}枚すべて生成済み`
        : `残り${Math.max(total - raw, 0)}枚。料金は実行前に確認できます`,
      done: total > 0 && raw >= total, blocked: false,
      action: { label: '未生成をまとめて選ぶ', run: () => selectMissing() },
    },
    {
      n: 6, key: 'validate', title: 'LINE仕様を検証',
      desc: state.validated
        ? '検証に通りました'
        : 'サイズ・透過・容量・余白をまとめてチェックします',
      done: !!state.validated, blocked: false,
      action: { label: '検証する', run: () => { switchTab('output'); $('#btn-validate').click(); } },
    },
    {
      n: 7, key: 'package', title: '提出用ZIPを作る',
      desc: info.packages.length
        ? `${info.packages.length}個のZIPができています`
        : 'main画像・tab画像も一緒に作ります',
      done: info.packages.length > 0, blocked: false,
      action: { label: 'ZIPを作る', run: () => { switchTab('output'); $('#btn-package').click(); } },
    },
  ];
}

function renderGuide() {
  const info = state.info;
  if (!info) return;
  const steps = computeSteps(info);
  state.steps = steps;

  // 自動判定した「いまのステップ」。ユーザーが明示的に選んだ場合はそちらを優先します。
  const auto = steps.find((s) => !s.done && !s.optional) || null;
  const pinned = state.pinnedStep
    ? steps.find((s) => s.key === state.pinnedStep) || null
    : null;
  const shown = pinned || auto;

  $('#steps').innerHTML = steps.map((s) => {
    const cls = (shown && s.key === shown.key) ? 'current'
      : s.done ? 'done' : s.blocked ? 'blocked' : 'todo';
    return `<li class="step ${cls}" data-step="${s.key}"
        data-tip="${escapeHtml(s.help || s.desc)}${s.done ? '（完了済み。クリックでやり直せます）' : ''}">
      <span class="num"><span>${s.n}</span></span>
      <span>
        <span class="st">${escapeHtml(s.title)}${s.optional ? '<span class="sd">（任意）</span>' : ''}</span>
        <span class="sd">${escapeHtml(s.desc)}</span>
      </span>
    </li>`;
  }).join('');

  const box = $('#next-action');
  box.className = 'next';
  $('#btn-step-auto').hidden = !pinned;
  $('#next-label').textContent = pinned ? '選んだステップ' : '次にやること';

  const idx = shown ? steps.indexOf(shown) : steps.length;
  $('#btn-step-prev').disabled = idx <= 0;
  $('#btn-step-next').disabled = idx >= steps.length - 1;

  if (!shown) {
    box.classList.add('finished');
    $('#next-label').textContent = '完了';
    $('#next-title').textContent = 'すべて完了しました';
    $('#next-desc').textContent =
      '「検証・出力」タブからZIPをダウンロードして、LINE Creators Market に申請できます。'
      + 'やり直したいステップは、上のカードをクリックすればいつでも戻れます。';
    $('#btn-next').style.display = '';
    $('#btn-next').textContent = 'ZIPを確認する';
    $('#btn-next').onclick = () => switchTab('output');
    return;
  }

  if (shown.blocked) box.classList.add('blocked');
  else if (pinned && shown.done) box.classList.add('finished');

  $('#next-title').textContent = `${shown.n}. ${shown.title}`;
  $('#next-desc').textContent = shown.blocked ? (shown.help || shown.desc) : shown.desc;

  const btn = $('#btn-next');
  if (shown.action && !shown.blocked) {
    btn.style.display = '';
    btn.textContent = shown.action.label;
    btn.onclick = shown.action.run;
  } else {
    btn.style.display = 'none';
  }
}

/** ステップを選んで、そのタブへ移動します（完了済みでも戻れます）。 */
function goToStep(key) {
  state.pinnedStep = key;
  renderGuide();
  switchTab(STEP_TAB[key] || 'grid');
  if (key === 'rest') selectMissing();
}

$('#steps').addEventListener('click', (e) => {
  const li = e.target.closest('.step');
  if (li) goToStep(li.dataset.step);
});

$('#btn-step-prev').addEventListener('click', () => {
  const steps = state.steps || [];
  const shown = state.pinnedStep
    ? steps.find((s) => s.key === state.pinnedStep)
    : steps.find((s) => !s.done && !s.optional);
  const idx = shown ? steps.indexOf(shown) : steps.length;
  if (idx > 0) goToStep(steps[idx - 1].key);
});

$('#btn-step-next').addEventListener('click', () => {
  const steps = state.steps || [];
  const shown = state.pinnedStep
    ? steps.find((s) => s.key === state.pinnedStep)
    : steps.find((s) => !s.done && !s.optional);
  const idx = shown ? steps.indexOf(shown) : -1;
  if (idx >= 0 && idx < steps.length - 1) goToStep(steps[idx + 1].key);
});

$('#btn-step-auto').addEventListener('click', () => {
  state.pinnedStep = null;
  renderGuide();
  toast('いまの状態に合わせた表示に戻しました');
});

$('#btn-guide-toggle').addEventListener('click', () => {
  const guide = $('#guide');
  guide.hidden = !guide.hidden;
  writeLS('guide.hidden', guide.hidden ? '1' : '0');
});

function tryOne() {
  switchTab('grid');
  const first = state.stickers[0];
  if (!first) { toast('CSVにスタンプがありません', true); return; }
  state.selected = new Set([first.id]);
  renderGrid();
  $('#btn-generate').click();
}

function selectMissing() {
  switchTab('grid');
  state.selected = new Set(state.stickers.filter((s) => !s.has_raw).map((s) => s.id));
  renderGrid();
  if (!state.selected.size) toast('未生成のスタンプはありません');
  else toast(`未生成の${state.selected.size}枚を選びました。「選択した画像を生成」で開始できます`);
}

$('#btn-try-one').addEventListener('click', tryOne);

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
  if (tab.dataset.tab === 'start') loadMaster();
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
  $('#guide').hidden = readLS('guide.hidden', '0') === '1';
  renderGuide();
  await loadMaster();
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
/* はじめに: 1 準備チェック / 2 キャラクター                            */
/* ------------------------------------------------------------------ */
function renderSetupChecklist() {
  const info = state.info;
  if (!info) return;
  const items = [
    {
      ok: info.api_key_set,
      label: 'APIキー',
      good: '設定済み（値は画面には出しません）',
      bad: '未設定',
      how: '<code>.env</code> に <code>OPENAI_API_KEY=...</code> を書いて、このページを再読み込みしてください。'
         + '未設定でも dry-run と文字合成は使えます。',
    },
    {
      ok: !info.font_error,
      label: '日本語フォント',
      good: info.font_path,
      bad: info.font_error || '未検出',
      how: '<code>config/sticker_config.yaml</code> の <code>font.path</code> に .ttf / .ttc の絶対パスを設定してください。',
    },
    {
      ok: !info.csv_error,
      label: 'セリフCSV',
      good: `${info.stickers.length}件 読み込み済み`,
      bad: info.csv_error || 'エラー',
      how: '「セリフ編集」タブで内容を確認・修正できます。',
    },
    {
      ok: true,
      label: '生成の設定',
      good: `${info.model} / 画質 ${info.quality}`,
      bad: '',
      how: '画質は <code>.env</code> の <code>IMAGE_QUALITY</code>（low / medium / high）で変えられます。'
         + ' low が最も安価です。',
    },
  ];
  $('#setup-list').innerHTML = items.map((i) => `
    <div class="item ${i.ok ? 'ok' : 'ng'}">
      <span class="mark">${i.ok ? '✓' : '!'}</span>
      <span>
        <b>${escapeHtml(i.label)}</b>: ${escapeHtml(i.ok ? i.good : i.bad)}
        ${!i.ok || i.label === '生成の設定' ? `<span class="how">${i.how}</span>` : ''}
      </span>
    </div>`).join('');
}

async function loadMaster() {
  renderSetupChecklist();
  let m;
  try { m = await api('/api/master'); } catch (e) { toast(e.message, true); return; }
  state.master = m;

  const img = $('#master-img');
  const empty = $('#master-empty');
  if (m.exists && !m.error) {
    img.onload = () => { img.hidden = false; empty.hidden = true; };
    img.onerror = () => { img.hidden = true; empty.hidden = false; };
    img.src = `/img/master.png?t=${m.mtime}`;
    $('#master-meta').textContent = `${m.width}×${m.height} ${m.mode} / ${m.size_kb}KB\n${m.path}`;
  } else {
    img.hidden = true; empty.hidden = false;
    $('#master-meta').textContent = m.error || m.path;
  }

  if ($('#master-prompt') !== document.activeElement) $('#master-prompt').value = m.prompt;

  // 履歴
  const field = $('#master-history-field');
  field.hidden = m.backups.length === 0;
  $('#master-history').innerHTML = m.backups
    .slice().reverse()
    .map((b) => `<option value="${escapeHtml(b)}">${escapeHtml(b)}</option>`).join('');

  // 既存画像との食い違い警告
  const warn = $('#master-warn');
  if (m.raw_count > 0) {
    warn.hidden = false;
    warn.innerHTML = `
      <b>既に ${m.raw_count}枚のスタンプ画像があります。</b><br>
      キャラクターを変えても、作成済みの画像は自動では作り直されません（勝手に消さない方針です）。
      絵柄を揃えるには、作り直すか、いまの画像をいったん退避してください。
      <button class="btn" id="btn-archive"
              data-tip="output/archive/日時/ へ移動します。削除はしません">
        いまの画像を退避する（${m.raw_count + m.final_count}ファイル）
      </button>`;
  } else {
    warn.hidden = true;
  }

  const cost = await costFor(1);
  $('#master-cost').textContent = `概算コスト: ${cost}`;
}

$('#master-file').addEventListener('change', (e) => {
  $('#btn-master-upload').disabled = !e.target.files.length;
});

$('#btn-master-upload').addEventListener('click', async () => {
  const file = $('#master-file').files[0];
  if (!file) return;
  const form = new FormData();
  form.append('file', file);
  try {
    const res = await fetch('/api/master/upload', { method: 'POST', body: form });
    const d = await res.json();
    if (!res.ok) throw new Error(d.error || 'アップロードに失敗しました');
    toast(d.backup
      ? `マスター画像を差し替えました（前の画像は ${d.backup} として保存）`
      : 'マスター画像を設定しました');
    $('#master-file').value = '';
    $('#btn-master-upload').disabled = true;
    await afterMasterChanged();
  } catch (e) { toast(e.message, true); }
});

$('#btn-master-generate').addEventListener('click', async () => {
  const cost = await costFor(1);
  if (!confirm(`AIにキャラクターマスター画像を1枚作らせます。\n概算コスト: ${cost}\n\n`
    + 'いまの画像は履歴として残るので、気に入らなければ戻せます。\n実行しますか？')) return;
  try {
    resetJobUi('キャラクターマスター画像を作成しています');
    await api('/api/master/generate', { method: 'POST', body: {} });
    startPolling();
  } catch (e) { toast(e.message, true); closeJobBar(); }
});

$('#btn-master-restore').addEventListener('click', async () => {
  const name = $('#master-history').value;
  if (!name) return;
  if (!confirm(`${name} をマスター画像に戻します。\nいまの画像も履歴として残ります。`)) return;
  try {
    await api('/api/master/restore', { method: 'POST', body: { name } });
    toast('マスター画像を戻しました');
    await afterMasterChanged();
  } catch (e) { toast(e.message, true); }
});

$('#btn-prompt-save').addEventListener('click', async () => {
  try {
    await api('/api/master/prompt', { method: 'POST', body: { prompt: $('#master-prompt').value } });
    toast('プロンプトを保存しました');
  } catch (e) { toast(e.message, true); }
});

$('#card-character').addEventListener('click', async (e) => {
  if (e.target.id !== 'btn-archive') return;
  const m = state.master;
  if (!confirm(`作成済みの ${m.raw_count + m.final_count}ファイルを output/archive/ へ移動します。\n`
    + '削除はしないので、あとから戻せます。実行しますか？')) return;
  try {
    const d = await api('/api/generated/archive', { method: 'POST', body: {} });
    toast(`退避しました: ${d.archived_to}`);
    await refreshStickers();
    await loadMaster();
  } catch (e) { toast(e.message, true); }
});

/** マスター画像が変わったあとの再読み込み。 */
async function afterMasterChanged() {
  const info = await api('/api/state');
  state.info = info;
  state.stickers = info.stickers;
  renderChips(info);
  renderGuide();
  await loadMaster();
  refreshPreview();
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
  $('#grid-empty').hidden = state.stickers.some((s) => s.has_raw || s.has_final);
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
    if (job.kind === 'master') await afterMasterChanged();
    else await refreshStickers();
  }
}

async function refreshStickers() {
  const d = await api('/api/stickers');
  state.stickers = d.stickers;
  if (state.info) state.info.stickers = d.stickers;
  state.validated = false;  // 画像が変わったので検証をやり直す必要があります
  renderGrid();
  fillDesignTargets();
  renderGuide();
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
    if (state.info) state.info.stickers = d.stickers;
    state.csvDirty = false;
    renderCsvTable();
    renderGrid();
    fillDesignTargets();
    renderGuide();
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
  // 生成済みを先頭に並べますが、未生成もマスター画像で代用してプレビューできます。
  const withRaw = state.stickers.filter((s) => s.has_raw);
  const without = state.stickers.filter((s) => !s.has_raw);
  const keep = $('#design-target').value;
  $('#design-target').innerHTML = [...withRaw, ...without]
    .map((s) => `<option value="${s.id}">${s.id} ${escapeHtml(s.text)}${s.has_raw ? '' : '（未生成）'}</option>`)
    .join('');
  if (keep && state.stickers.some((s) => s.id === keep)) $('#design-target').value = keep;
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
      $('#design-source').textContent = '';
      return;
    }
    $('#design-error').textContent = '';
    $('#design-source').textContent = res.headers.get('X-Preview-Source') === 'master'
      ? 'このスタンプはまだ生成していないため、キャラクターはマスター画像で代用しています。文字の大きさ・配置はそのまま本番に使われます。'
      : '';
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
    state.validated = d.checked > 0 && d.errors === 0;
    renderGuide();
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
    if (state.info) {
      state.info.has_main = true;
      state.info.has_tab = true;
      state.info.packages = d.packages.filter((p) => p.downloadable).map((p) => p.name);
      renderGuide();
    }
    loadAssets();
  } catch (e) { box.innerHTML = `<div class="line ERROR">${escapeHtml(e.message)}</div>`; }
});

/** main/tab はまだ作っていないことが多いので、404を壊れた画像として見せません。 */
function loadAssets() {
  const t = Date.now();
  [['main', '#asset-main'], ['tab', '#asset-tab']].forEach(([name, sel]) => {
    const img = $(sel);
    const empty = $(`${sel}-empty`);
    const has = state.info && state.info[`has_${name}`];
    if (!has) { img.hidden = true; empty.hidden = false; return; }
    img.onload = () => { img.hidden = false; empty.hidden = true; };
    img.onerror = () => { img.hidden = true; empty.hidden = false; };
    img.src = `/img/${name}.png?t=${t}`;
  });
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
