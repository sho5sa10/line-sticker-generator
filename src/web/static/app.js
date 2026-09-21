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
  // かんたん入力
  groups: [],
  presets: {},
  profile: {},
  manualEdit: false,   // 説明文を手で書き換えたら true（選択を変えても上書きしない）
  promptDirty: false,  // 保存していない変更がある
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
    // 失敗の原因が「サーバーが古いまま」のことがあるので、あわせて確認します。
    const outdated = await checkServer();
    const msg = (data && data.error) || `${res.status} ${res.statusText}`;
    throw new Error(outdated ? `${msg}（GUIの再起動が必要です。画面上部の案内を見てください）` : msg);
  }
  return data;
}

/**
 * サーバー側プログラムが起動後に更新されていないか確認します。
 * 画面は常に最新のファイルが読み込まれますが、サーバーは起動時のまま動くため、
 * 食い違うと保存などが正しく動かなくなります。
 * 確認用の窓口（/api/server）自体が無い＝それより古いサーバー、とみなします。
 */
async function checkServer() {
  let outdated = false;
  try {
    const res = await fetch('/api/server', { cache: 'no-store' });
    if (res.status === 404) outdated = true;
    else if (res.ok) outdated = !!(await res.json()).outdated;
  } catch (e) {
    return false;  // サーバー停止中などは別の問題なのでここでは判定しません
  }
  $('#outdated-banner').hidden = !outdated;
  return outdated;
}
checkServer();
setInterval(checkServer, 15000);

/**
 * 時間のかかる処理のあいだ、ボタンを「処理中」の見た目にして押せなくします。
 * 何も変化がないと、押しても反応していないように見えるためです。
 */
async function withBusy(btn, busyLabel, fn) {
  if (btn.disabled) return undefined;
  const label = btn.textContent;
  btn.disabled = true;
  btn.classList.add('busy');
  btn.textContent = busyLabel;
  try {
    return await fn();
  } finally {
    btn.disabled = false;
    btn.classList.remove('busy');
    btn.textContent = label;
  }
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
  const tabs = document.querySelector('.tabs');
  const apply = () => {
    const root = document.documentElement.style;
    root.setProperty('--topbar-h', `${Math.round(bar.getBoundingClientRect().height)}px`);
    // タブの高さも測り、付いてくるプレビューがタブの下に止まるようにします。
    root.setProperty('--tabs-h', `${Math.round(tabs.getBoundingClientRect().height)}px`);
  };
  apply();
  if (window.ResizeObserver) {
    const ro = new ResizeObserver(apply);
    ro.observe(bar);
    ro.observe(tabs);
  } else {
    window.addEventListener('resize', apply);
  }
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
  // APIキーは必須ではありません（手持ち画像の取り込みだけでも最後まで進められます）。
  const ready = !info.font_error && !info.csv_error;
  const aiReady = info.api_key_set && info.master_ok;

  return [
    {
      n: 1, key: 'setup', title: '準備を整える',
      desc: !ready
        ? [info.font_error && 'フォント未検出', info.csv_error && 'CSVエラー'].filter(Boolean).join(' / ')
        : info.api_key_set
          ? `フォント・CSV(${total}件)・APIキー すべてOK`
          : `フォント・CSV(${total}件) OK。APIキーなし＝手持ち画像で進めます`,
      done: ready, blocked: !ready,
      action: { label: '準備の状態を見る', run: () => switchTab('start') },
      help: 'フォントとCSVが必要です。APIキーはAIで画像を作るときだけ必要です。',
    },
    {
      n: 2, key: 'character', title: 'キャラクターを決める',
      desc: info.master_ok
        ? 'マスター画像あり。差し替えれば別キャラのセットも作れます'
        : 'まだありません。画像を用意するか、AIに作ってもらえます',
      done: info.master_ok, blocked: false,
      // AIを使わない（APIキーなし）なら、マスター画像は無くても進められます。
      optional: !info.api_key_set,
      action: { label: info.master_ok ? 'キャラクターを見直す' : 'キャラクターを用意する',
                run: () => switchTab('start') },
      help: '画像をアップロードするか、AIに1枚だけ作らせることができます。'
          + 'いまの画像は履歴として残るので、いつでも戻せます。',
    },
    {
      n: 3, key: 'try', title: 'まず1枚用意する',
      desc: raw
        ? `${raw}枚の画像ができています`
        : aiReady
          ? '001を1枚だけAIで作るか、手持ちの画像を取り込みます'
          : '手持ちの画像を取り込みます（無料）',
      done: raw >= 1, blocked: false,
      action: aiReady
        ? { label: '001を1枚だけ生成', run: () => tryOne() }
        : { label: '画像を取り込む（無料）', run: () => startBulkImport() },
    },
    {
      n: 4, key: 'design', title: '文字の見た目を整える',
      desc: '大きさ・色・縁取りをその場で確認できます（APIを使わないので無料）'
        + (raw ? '' : '。生成前でもマスター画像で試せます'),
      done: final >= 1, blocked: false, optional: true,
      action: { label: '文字デザインを開く', run: () => switchTab('design') },
    },
    {
      n: 5, key: 'rest', title: '残りをそろえる',
      desc: raw >= total && total
        ? `${total}枚すべてそろいました`
        : `残り${Math.max(total - raw, 0)}枚。`
          + (aiReady ? 'AIで作る（料金は実行前に確認）か、画像を取り込みます' : '手持ちの画像を取り込みます'),
      done: total > 0 && raw >= total, blocked: false,
      action: aiReady
        ? { label: '未生成をまとめて選ぶ', run: () => selectMissing() }
        : { label: '残りの画像を取り込む', run: () => startBulkImport() },
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
        <span class="st">${escapeHtml(s.title)}${s.optional ? '<span class="opt-tag">（任意）</span>' : ''}</span>
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
  if (tab.dataset.tab === 'output') { loadAssets(); updatePlan(); }
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
  if (!info.master_ok && info.api_key_set) notes.push('キャラクターマスター画像がありません（AIで作るときに必要です）。「はじめに」タブで用意できます。');
  if (!info.api_key_set) notes.push('APIキーが未設定のため、AIでの画像生成は使えません。手持ちの画像を取り込めば、文字入れ・検証・ZIP作成まで無料で進められます。');
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

  if (!state.promptDirty && $('#master-prompt') !== document.activeElement) {
    $('#master-prompt').value = m.prompt_ja;
    await loadProfile();
  }
  renderPromptInfo(m);

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

/* ---------- かんたん入力（選択肢から説明文を作る） ---------- */
async function loadProfile() {
  let d;
  try { d = await api('/api/master/profile'); } catch (e) { return; }
  state.groups = d.groups;
  state.presets = d.presets;
  state.presetInfo = d.preset_info || {};
  state.presetCategories = d.preset_categories || [];
  state.presetOrder = d.preset_order || Object.keys(d.presets);
  state.profile = d.profile || {};
  // 保存された選択内容と説明文が食い違う＝手で書き換えてある
  setManualEdit(!d.text_matches_profile && !!$('#master-prompt').value.trim());
  $('#profile-extra').value = state.profile.extra || '';
  renderProfile();
}

function isGroupVisible(g, profile) {
  return Object.entries(g.when || {}).every(([k, allowed]) => allowed.includes(profile[k]));
}

/** 表示されなくなった項目（例: 動物にしたときの髪型）の選択を外します。 */
function pruneProfile() {
  for (const g of state.groups) {
    if (!isGroupVisible(g, state.profile)) delete state.profile[g.key];
  }
}

function renderProfile() {
  // ひな形は「人／動物／そのほか」に分けて並べ、選んだ理由をツールチップで出します。
  const btn = (name) => {
    const note = (state.presetInfo[name] || {}).note || '';
    return `<button type="button" class="opt preset" data-preset="${escapeHtml(name)}"
      ${note ? `data-tip="${escapeHtml(note)}"` : ''}>${escapeHtml(name)}</button>`;
  };
  const names = state.presetOrder || Object.keys(state.presets);
  const cats = state.presetCategories.length ? state.presetCategories : [''];
  $('#preset-row').innerHTML = cats.map((cat) => {
    const inCat = names.filter((n) => !cat || (state.presetInfo[n] || {}).category === cat);
    if (!inCat.length) return '';
    return `<div class="preset-cat">${cat ? `<span class="preset-cat-name">${escapeHtml(cat)}</span>` : ''}
            <div class="chips-row">${inCat.map(btn).join('')}</div></div>`;
  }).join('');

  $('#profile-groups').innerHTML = state.groups
    .filter((g) => isGroupVisible(g, state.profile))
    .map((g) => {
      const cur = state.profile[g.key];
      const chips = g.options.map((o) => {
        const on = g.type === 'multi' ? (cur || []).includes(o) : cur === o;
        return `<button type="button" class="opt${on ? ' on' : ''}" data-key="${g.key}" data-val="${escapeHtml(o)}">${escapeHtml(o)}</button>`;
      }).join('');
      const note = g.type === 'multi' ? '<small>いくつでも</small>' : '<small>もう一度押すと解除</small>';
      return `<div class="easy-row"><span class="easy-label">${escapeHtml(g.label)}${note}</span>
              <div class="chips-row">${chips}</div></div>`;
    }).join('');
}

function setManualEdit(on) {
  state.manualEdit = on;
  $('#manual-note').hidden = !on;
  $('#btn-recompose').hidden = !on;
}

let composeTimer = null;
function onProfileChange() {
  state.promptDirty = true;
  pruneProfile();
  renderProfile();
  if (state.manualEdit) return;   // 手で書き換えた説明文は勝手に上書きしません
  clearTimeout(composeTimer);
  composeTimer = setTimeout(composeFromProfile, 120);
}

async function composeFromProfile() {
  try {
    const d = await api('/api/master/compose', { method: 'POST', body: { profile: state.profile } });
    $('#master-prompt').value = d.text;
  } catch (e) { toast(e.message, true); }
}

$('#preset-row').addEventListener('click', (e) => {
  const name = e.target.dataset.preset;
  if (!name) return;
  if (state.manualEdit && !confirm(`「${name}」の内容で説明文を作り直します。手で書き換えた部分は消えますがよろしいですか？`)) return;
  state.profile = JSON.parse(JSON.stringify(state.presets[name]));
  $('#profile-extra').value = '';
  setManualEdit(false);
  onProfileChange();
  toast(`「${name}」を読み込みました。気になるところだけ変えてください`);
});

$('#profile-groups').addEventListener('click', (e) => {
  const { key, val } = e.target.dataset;
  if (!key) return;
  const g = state.groups.find((x) => x.key === key);
  if (g.type === 'multi') {
    const list = new Set(state.profile[key] || []);
    if (list.has(val)) list.delete(val); else list.add(val);
    // 選択肢の並び順にそろえる（説明文の語順が安定するように）
    const ordered = g.options.filter((o) => list.has(o));
    if (ordered.length) state.profile[key] = ordered; else delete state.profile[key];
  } else if (state.profile[key] === val) {
    delete state.profile[key];   // もう一度押すと解除
  } else {
    state.profile[key] = val;
  }
  onProfileChange();
});

$('#profile-extra').addEventListener('input', (e) => {
  const v = e.target.value;
  if (v.trim()) state.profile.extra = v; else delete state.profile.extra;
  onProfileChange();
});

$('#master-prompt').addEventListener('input', () => {
  state.promptDirty = true;
  setManualEdit(true);
});

$('#btn-recompose').addEventListener('click', () => {
  if (!confirm('手で書き換えた説明文を捨てて、選択内容から作り直します。よろしいですか？')) return;
  setManualEdit(false);
  onProfileChange();
});

/** 日本語版が使われているか、実際にAIへ送る全文はどうなるかを表示します。 */
function renderPromptInfo(m) {
  $('#prompt-mode').textContent = m.prompt_mode === 'ja'
    ? `使用中: 日本語の説明（${m.prompt_ja_path.split(/[\\/]/).pop()}）`
    : 'まだ保存されていません。いまは英語の初期プロンプトが使われています。保存すると日本語の説明に切り替わります。';
  $('#master-prompt-full').textContent = m.full_prompt;
}

$('#btn-prompt-save').addEventListener('click', async () => {
  try {
    const d = await api('/api/master/prompt', {
      method: 'POST', body: { prompt_ja: $('#master-prompt').value, profile: state.profile },
    });
    state.promptDirty = false;
    renderPromptInfo(d);
    toast('キャラクターの説明を保存しました。これから作る画像に使われます');
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
          <button class="btn" data-act="upload"
                  data-tip="この番号に手持ちの画像を入れます。文字入れ・検証まで自動で行います（無料）">画像を入れる</button>
        </div>
        <div class="rowbtns">
          <button class="btn" data-act="regen"
                  data-tip="この1枚だけAIで作り直します（課金されます）。前の画像は退避されます">AIで作り直す</button>
          <button class="btn" data-act="rerender"
                  data-tip="APIを使わず文字だけ貼り直します（無料）">文字のみ</button>
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
  if (act === 'upload') {
    const input = $('#cell-file');
    input.dataset.target = id;
    input.value = '';
    input.click();
    return;
  }
  if (act === 'regen') {
    const cost = await costFor(1);
    if (!confirm(`${id}「${sticker.text}」をAPIで作り直します。\n概算コスト: ${cost}\n\n実行しますか？`)) return;
    runGenerate([id], { force: true });
  } else if (act === 'rerender') {
    runRender([id]);
  }
});

// ボタンの帯が画面の外に出たら、選択数だけを右上に出します
function updateSelectionFloat() {
  const tabs = document.querySelector('.tabs').getBoundingClientRect();
  $('#selection-float').hidden = $('#selection-count').getBoundingClientRect().bottom > tabs.bottom;
}
window.addEventListener('scroll', updateSelectionFloat, { passive: true });
window.addEventListener('resize', updateSelectionFloat);
document.querySelector('.tabs').addEventListener('click', () => setTimeout(updateSelectionFloat, 0));
$('#selection-float').addEventListener('click', () => {
  window.scrollTo({ top: 0, behavior: 'smooth' });
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
  const valid = (state.info && state.info.line_spec.valid_set_sizes) || [8, 16, 24, 32, 40];
  const countEl = $('#selection-count');
  countEl.innerHTML = n && valid.includes(n)
    ? `<b>${n}</b>枚選択中（このまま1セットにできます）`
    : `<b>${n}</b>枚選択中`;
  countEl.classList.toggle('has', n > 0);
  const floatEl = $('#selection-float');
  floatEl.innerHTML = `<b>${n}</b>枚選択中`;
  floatEl.classList.toggle('has', n > 0);
  updateSelectionFloat();
  if (packageMode() === 'selected') updatePlan();
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
/* 手持ち画像の取り込み（APIを呼ばない＝無料）                          */
/* ------------------------------------------------------------------ */
function startBulkImport() {
  switchTab('grid');
  const input = $('#bulk-file');
  input.value = '';
  input.click();
}
$('#btn-bulk-import').addEventListener('click', startBulkImport);
$('#btn-empty-import').addEventListener('click', startBulkImport);

/** 1枚だけ取り込む（カードの「画像を入れる」）。 */
$('#cell-file').addEventListener('change', async (e) => {
  const file = e.target.files[0];
  const id = e.target.dataset.target;
  if (!file || !id) return;
  const sticker = state.stickers.find((s) => s.id === id);
  if (sticker && sticker.has_raw
      && !confirm(`${id} にはすでに画像があります。\n置き換えますか？（前の画像は output/archive/replaced/ に残ります）`)) {
    return;
  }
  const form = new FormData();
  form.append('file', file);
  try {
    const res = await fetch(`/api/stickers/${encodeURIComponent(id)}/upload`, { method: 'POST', body: form });
    const d = await res.json();
    if (!res.ok) throw new Error(d.error || d.message || '取り込みに失敗しました');
    const extra = d.issues && d.issues.length ? `（注意: ${d.issues[0]}）` : '';
    toast(d.ok ? `${id} に画像を入れました ${extra}` : `${id}: ${d.message} ${extra}`, !d.ok);
    await refreshStickers();
  } catch (err) { toast(err.message, true); }
});

/** まとめて取り込む。大量でも止まらないよう、8枚ずつ送って進捗を出します。 */
$('#bulk-file').addEventListener('change', async (e) => {
  const files = Array.from(e.target.files);
  if (!files.length) return;

  const existing = new Set(state.stickers.filter((s) => s.has_raw).map((s) => s.id));
  const overwrite = files.filter((f) => {
    const m = f.name.replace(/\.[^.]+$/, '').match(/^\D*(\d{1,4})\D*$/);
    return m && existing.has(String(parseInt(m[1], 10)).padStart(3, '0'));
  }).length;
  if (overwrite && !confirm(`${files.length}枚のうち ${overwrite}枚は、すでに画像がある番号です。\n`
      + '置き換えますか？（前の画像は output/archive/replaced/ に残ります）')) return;

  resetJobUi('画像を取り込んでいます（APIは使いません）');
  $('#btn-cancel').style.display = 'none';
  $('#job-api').textContent = 'API呼び出し 0回';
  const log = $('#job-log');
  const line = (cls, text) => {
    const div = document.createElement('div');
    div.className = cls;
    div.textContent = text;
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
  };

  let done = 0, ok = 0, ng = 0, skip = 0;
  const CHUNK = 8;
  for (let i = 0; i < files.length; i += CHUNK) {
    const form = new FormData();
    files.slice(i, i + CHUNK).forEach((f) => form.append('files', f));
    try {
      const res = await fetch('/api/stickers/import', { method: 'POST', body: form });
      const d = await res.json();
      if (!res.ok) throw new Error(d.error || '取り込みに失敗しました');
      d.results.forEach((r) => {
        if (r.ok) ok++; else ng++;
        line(r.ok ? 'ok' : 'error', `${r.id} ${r.file} … ${r.message}${r.issues.length ? '（' + r.issues[0] + '）' : ''}`);
      });
      d.skipped.forEach((s) => { skip++; line('skip', `${s.file} … ${s.reason}`); });
    } catch (err) {
      ng += Math.min(CHUNK, files.length - i);
      line('error', err.message);
    }
    done = Math.min(i + CHUNK, files.length);
    $('#job-progress').textContent = `${done} / ${files.length}`;
    $('#job-fill').style.width = `${Math.round((done / files.length) * 100)}%`;
  }

  $('#job-title').textContent = `取り込み完了: 成功 ${ok} / 失敗 ${ng} / スキップ ${skip}`;
  toast(`取り込み完了: 成功 ${ok}枚`, ng > 0 && ok === 0);
  await refreshStickers();
});

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
      <td><textarea data-f="text" rows="1">${escapeHtml(s.text)}</textarea></td>
      <td><input data-f="action" value="${escapeHtml(s.action)}"></td>
      <td><input data-f="expression" value="${escapeHtml(s.expression)}"></td>
      <td><input data-f="category" value="${escapeHtml(s.category)}"></td>
      <td><button class="btn small" data-act="del">削除</button></td>
    </tr>`).join('');
  $('#csv-count').textContent = `${state.stickers.length}件`;
  $$('#csv-body textarea').forEach(autoGrow);
}

/** 改行したセリフの行数に合わせて入力欄の高さを変えます。 */
function autoGrow(el) {
  el.style.height = 'auto';
  el.style.height = `${el.scrollHeight}px`;
}

$('#csv-body').addEventListener('input', (e) => {
  if (e.target.tagName === 'TEXTAREA') autoGrow(e.target);
  if (e.target.tagName !== 'INPUT' && e.target.tagName !== 'TEXTAREA') return;
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
    tr.querySelectorAll('input, textarea').forEach((i) => { o[i.dataset.f] = i.value; });
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
  if (font.font_id) state.fontId = font.font_id;
  loadFonts();
}

/** 選べるフォントの一覧を読み込みます。 */
async function loadFonts(selectId) {
  let d;
  try { d = await api('/api/fonts'); } catch (e) { return; }
  state.fonts = d.fonts;
  state.freeFonts = d.free_fonts || [];
  state.freeCategories = d.free_categories || [];
  const current = selectId || $('#f-font').value || state.fontId || d.current;
  $('#f-font').innerHTML = d.fonts
    .map((f) => `<option value="${escapeHtml(f.id)}">${escapeHtml(f.label)}</option>`).join('');
  if (current && d.fonts.some((f) => f.id === current)) $('#f-font').value = current;
  showFontNote();
  renderFontStore();
}

function showFontNote() {
  const f = (state.fonts || []).find((x) => x.id === $('#f-font').value);
  $('#font-note').textContent = f ? f.note : '';
  checkFontGlyphs();
}

/** 選んだフォントに、セリフで使っている文字がそろっているかを調べて知らせます。 */
let glyphTimer = null;
function checkFontGlyphs() {
  clearTimeout(glyphTimer);
  glyphTimer = setTimeout(async () => {
    const box = $('#font-missing');
    const id = $('#f-font').value;
    if (!id) { box.hidden = true; return; }
    let d;
    try { d = await api(`/api/fonts/check?font_id=${encodeURIComponent(id)}`); } catch (e) { box.hidden = true; return; }
    if (!d.missing.length) { box.hidden = true; return; }
    const chars = d.missing.slice(0, 12).map((c) => `「${escapeHtml(c)}」`).join('')
      + (d.missing.length > 12 ? ` ほか${d.missing.length - 12}文字` : '');
    const ids = d.affected_ids.length > 8
      ? `${d.affected_ids.slice(0, 8).join(', ')} ほか${d.affected_ids.length - 8}件`
      : d.affected_ids.join(', ');
    box.innerHTML = `このフォントには ${chars} がありません。`
      + `<b>${d.affected_ids.length}件</b>のスタンプで、その文字が「□」になります（${escapeHtml(ids)}）。`
      + '別のフォントを選ぶか、セリフを変えてください。';
    box.hidden = false;
  }, 150);
}

$('#f-font').addEventListener('change', () => { showFontNote(); schedulePreview(); });

/* ---------- フォントを増やす（無料フォントの追加） ---------- */
function renderFontStore() {
  const cats = state.freeCategories || [];
  $('#font-store-list').innerHTML = cats.map((cat) => {
    const items = (state.freeFonts || []).filter((f) => f.category === cat);
    if (!items.length) return '';
    return `<div class="fs-cat">${escapeHtml(cat)}</div>` + items.map((f) => `
      <div class="fs-item">
        <span class="fs-name">${escapeHtml(f.label)} <span class="hint">${f.size_mb}MB</span></span>
        <span class="fs-note">${escapeHtml(f.note)}</span>
        ${f.installed
          ? '<span class="fs-done">追加済み</span>'
          : `<button type="button" class="btn small" data-install="${escapeHtml(f.id)}">追加</button>`}
      </div>`).join('');
  }).join('');
}

$('#btn-font-store').addEventListener('click', () => {
  $('#font-store').hidden = !$('#font-store').hidden;
});

$('#font-store-list').addEventListener('click', (e) => {
  const btn = e.target.closest('[data-install]');
  if (!btn) return;
  const id = btn.dataset.install;
  const info = (state.freeFonts || []).find((f) => f.id === id);
  withBusy(btn, 'ダウンロード中…', async () => {
    try {
      await api('/api/fonts/install', { method: 'POST', body: { id } });
      await loadFonts(id);   // 追加したフォントをそのまま選んでプレビューします
      schedulePreview();
      toast(`「${info ? info.label : id}」を追加しました。プレビューで確認して、気に入ったら「この設定を保存」を押してください`);
    } catch (err) { toast(err.message, true); }
  });
});

function fillDesignTargets() {
  // 生成済みを先頭に並べますが、未生成もマスター画像で代用してプレビューできます。
  const withRaw = state.stickers.filter((s) => s.has_raw);
  const without = state.stickers.filter((s) => !s.has_raw);
  const keep = $('#design-target').value;
  $('#design-target').innerHTML = [...withRaw, ...without]
    .map((s) => `<option value="${s.id}">${s.id} ${escapeHtml(s.text)}${s.has_raw ? '' : '（未生成）'}</option>`)
    .join('');
  if (keep && state.stickers.some((s) => s.id === keep)) $('#design-target').value = keep;
  // 書きかけのセリフ（未保存）があるときは上書きしません。
  if ($('#btn-text-save').disabled) syncDesignText();
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
  if ($('#f-font').value) o.font_id = $('#f-font').value;
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
  const body = { id, style: currentStyleOverrides(), text: $('#design-text').value.replace(/^\s+|\s+$/g, '') };
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

/* セリフを直接書き換える（改行も保存） */
function syncDesignText() {
  const s = state.stickers.find((x) => x.id === $('#design-target').value);
  $('#design-text').value = s ? s.text : '';
  $('#design-text').dataset.original = s ? s.text : '';
  $('#btn-text-save').disabled = true;
  $('#text-save-note').textContent = '';
}
$('#design-target').addEventListener('change', syncDesignText);
$('#design-text').addEventListener('input', () => {
  const changed = $('#design-text').value.trim() !== ($('#design-text').dataset.original || '').trim();
  $('#btn-text-save').disabled = !changed || !$('#design-text').value.trim();
  $('#text-save-note').textContent = changed ? '保存するまで、スタンプには反映されません' : '';
});
$('#btn-text-save').addEventListener('click', async () => {
  const id = $('#design-target').value;
  try {
    const d = await api(`/api/stickers/${encodeURIComponent(id)}`, {
      method: 'PATCH', body: { text: $('#design-text').value },
    });
    const i = state.stickers.findIndex((x) => x.id === id);
    if (i >= 0) state.stickers[i] = d.sticker;
    if (state.info) state.info.stickers = state.stickers;
    renderCsvTable();
    renderGrid();
    syncDesignText();
    toast(`${id} のセリフを保存しました。スタンプに反映するには「保存して全部に再適用」または「文字のみ」を押してください`);
  } catch (e) { toast(e.message, true); }
});

/* おまかせ提案: キャラの色と雰囲気から文字スタイルの候補を出す */
$('#btn-suggest').addEventListener('click', () => withBusy($('#btn-suggest'), '読み取り中…', async () => {
  let d;
  try { d = await api('/api/design/suggest'); } catch (e) { toast(e.message, true); return; }
  state.suggestions = d.suggestions;
  $('#suggest-list').innerHTML = d.suggestions.map((sg, i) => `
    <button type="button" class="sg" data-i="${i}" data-tip="${escapeHtml(sg.reason)}">
      <span class="sg-sample" style="color:${sg.fill};
        text-shadow:${[...Array(8)].map((_, k) => {
          const a = (Math.PI * 2 * k) / 8;
          return `${(Math.cos(a) * 2).toFixed(1)}px ${(Math.sin(a) * 2).toFixed(1)}px 0 ${sg.stroke_fill}`;
        }).join(',')}">あア</span>
      <span><span class="sg-name">${escapeHtml(sg.name)}</span><br>
        <span class="sg-font">${escapeHtml(sg.font_label)}・縁取り${sg.stroke_width}px</span></span>
    </button>`).join('');
  toast(`${d.source || 'キャラクター'} の色から${d.suggestions.length}通り提案しました。押すとプレビューに反映されます`);
}));

$('#suggest-list').addEventListener('click', (e) => {
  const b = e.target.closest('.sg');
  if (!b) return;
  const sg = state.suggestions[Number(b.dataset.i)];
  $$('#suggest-list .sg').forEach((x) => x.classList.toggle('on', x === b));
  $('#f-fill').value = sg.fill.toLowerCase();
  $('#f-stroke_fill').value = sg.stroke_fill.toLowerCase();
  $('#f-stroke_width').value = sg.stroke_width;
  if (sg.font_id && [...$('#f-font').options].some((o) => o.value === sg.font_id)) {
    $('#f-font').value = sg.font_id;
    showFontNote();
  }
  schedulePreview();
  toast('プレビューに反映しました。気に入ったら「この設定を保存」を押してください');
});

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
$('#btn-validate').addEventListener('click', () => withBusy($('#btn-validate'), '検証中…', async () => {
  const box = $('#validate-result');
  box.innerHTML = '<div class="line working">全スタンプを検証しています。数秒かかります…</div>';
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
    toast(d.errors ? `検証しました: エラー ${d.errors}件` : `${d.checked}件を検証しました。エラーはありません`, d.errors > 0);
  } catch (e) {
    box.innerHTML = `<div class="line ERROR">${escapeHtml(e.message)}</div>`;
    toast(e.message, true);
  }
  box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}));

$('#btn-package').addEventListener('click', () => withBusy($('#btn-package'), '作成中…', async () => {
  const box = $('#package-result');
  box.innerHTML = '<div class="line working">main画像・tab画像・ZIPを作っています。100枚分で10秒ほどかかります…</div>';
  try {
    const d = await api('/api/package', { method: 'POST', body: packageOptions() });
    const lines = [
      `<div class="line">main: ${d.main.size_kb}KB ${d.main.ok ? '<span class="good">OK</span>' : '<span class="ERROR">NG</span>'}` +
      ` / tab: ${d.tab.size_kb}KB ${d.tab.ok ? '<span class="good">OK</span>' : '<span class="ERROR">NG</span>'}</div>`,
    ];
    d.packages.forEach((p) => {
      if (p.downloadable) {
        lines.push(`<div class="line dl"><a class="btn small primary" href="/api/download/${encodeURIComponent(p.name)}" download>ダウンロード</a>` +
                   ` ${escapeHtml(p.name)} — ${p.count}枚 / ${p.size_mb}MB</div>`);
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
    updatePlan();
    const zips = d.packages.filter((p) => p.downloadable).length;
    toast(zips ? `ZIPを${zips}個作りました。「ダウンロード」から保存できます` : 'ZIPを作れませんでした。下の警告を確認してください', !zips);
  } catch (e) {
    box.innerHTML = `<div class="line ERROR">${escapeHtml(e.message)}</div>`;
    toast(e.message, true);
  }
  box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}));

/* ---------- セットの作り方（何枚ずつZIPにするか） ---------- */
function packageMode() {
  const el = document.querySelector('input[name="setmode"]:checked');
  return el ? el.value : 'auto';
}

/** サーバーに渡す「セットの作り方」。 */
function packageOptions() {
  if (packageMode() === 'selected') return { ids: [...state.selected].sort() };
  const size = $('#set-size').value;
  return { set_size: size ? Number(size) : null };
}

function rangeText(set) {
  return set.first === set.last ? set.first : `${set.first}〜${set.last}`;
}

let planTimer = null;
function updatePlan() {
  clearTimeout(planTimer);
  planTimer = setTimeout(async () => {
    $('#setsize-row').classList.toggle('off', packageMode() !== 'auto');
    const box = $('#package-plan');
    let d;
    try {
      d = await api('/api/package/plan', { method: 'POST', body: packageOptions() });
    } catch (e) { box.textContent = e.message; box.classList.add('bad'); return; }

    const lines = [];
    if (d.sets.length) {
      // 例: 40枚×2セット＋16枚×1セット
      const counts = {};
      d.sets.forEach((s) => { counts[s.count] = (counts[s.count] || 0) + 1; });
      const summary = Object.keys(counts).map(Number).sort((a, b) => b - a)
        .map((c) => `${c}枚×${counts[c]}セット`).join('＋');
      lines.push(`<b>${summary}</b> のZIPを作ります（合計${d.total}枚）`);
      lines.push(`<small>${d.sets.map(rangeText).join(' ／ ')}</small>`);
    }
    if (d.error) lines.push(escapeHtml(d.error));
    if (d.leftover.length && !d.error) {
      const ids = d.leftover.length > 8
        ? `${d.leftover.slice(0, 8).join(', ')} ほか${d.leftover.length - 8}枚`
        : d.leftover.join(', ');
      lines.push(`残りの<b>${d.leftover.length}枚</b>はどのセットにも入りません<small>（${escapeHtml(ids)}）</small>`);
    }
    if (d.missing.length && packageMode() === 'auto') {
      lines.push(`<small>まだ完成画像が無い${d.missing.length}枚は含みません</small>`);
    }
    box.innerHTML = lines.map((l) => `<div>${l}</div>`).join('');
    box.classList.toggle('bad', !!d.error || !d.sets.length);
    $('#btn-package').disabled = !!d.error || !d.sets.length;
  }, 120);
}

document.querySelectorAll('input[name="setmode"]').forEach((r) => r.addEventListener('change', () => {
  writeLS('package.mode', packageMode());
  updatePlan();
}));
$('#set-size').addEventListener('change', () => {
  writeLS('package.size', $('#set-size').value);
  updatePlan();
});
$('#btn-go-select').addEventListener('click', () => {
  document.querySelector('input[name="setmode"][value="selected"]').checked = true;
  writeLS('package.mode', 'selected');
  switchTab('grid');
  toast('ZIPに入れたいスタンプにチェックを付けてから、「検証・出力」タブに戻ってください');
});
// 前回選んだ作り方を復元
(function restorePackageChoice() {
  const mode = readLS('package.mode', 'auto');
  const radio = document.querySelector(`input[name="setmode"][value="${mode}"]`);
  if (radio) radio.checked = true;
  const size = readLS('package.size', '');
  if ([...$('#set-size').options].some((o) => o.value === size)) $('#set-size').value = size;
})();

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

$('#btn-gallery').addEventListener('click', () => withBusy($('#btn-gallery'), '生成中…', async () => {
  try {
    const d = await api('/api/gallery', { method: 'POST' });
    $('#gallery-result').innerHTML = `<div class="line good">生成しました</div><div class="line">${escapeHtml(d.path)}</div>`;
    toast('gallery.html を生成しました');
  } catch (e) {
    $('#gallery-result').innerHTML = `<div class="line ERROR">${escapeHtml(e.message)}</div>`;
    toast(e.message, true);
  }
}));

$('#btn-log').addEventListener('click', async () => {
  const d = await api('/api/log');
  $('#log-view').textContent = d.lines.join('\n') || '（ログはまだありません）';
  $('#log-view').scrollTop = $('#log-view').scrollHeight;
});

/* ------------------------------------------------------------------ */
loadState().catch((e) => toast(e.message, true));
