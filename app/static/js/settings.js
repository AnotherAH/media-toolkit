// settings.js: the Settings tab (S-01 to S-10, G-12, W-03).
// One page of section cards with a sticky rail. Every control saves itself
// (bindSetting); there are no Save buttons. Hardware, packs and models come
// from the shared machine state (core STATE), so every screen says the same
// thing about the GPU (G-12). There is no way to rerun first-time setup (W-03).
import {
  el, bdi, on, get, post, registerHook, CONFIG, DEFAULTS, SETTINGS_META, STATE, isConfigLoaded,
  loadConfig, refreshSettingsMeta, saveSettings, bindSetting, validators, rateToMBps, mbpsToRate,
  showSaveState, setFieldError, loadHardware, loadModels, loadPacks, loadAbout, loadCapabilities,
  recheckHardware, installPack, packInstallRunning, openPath, revealPath, openUrl, pickFolder, pickFile,
  copyText, bytes, mbOf, num, plural, scrollToEl, currentTab,
} from './core.js';
import {
  icon, button, linkButton, callout, disclosure, pageHead, card, flash, setBusy, clearBusy, withBusy,
  toast, toastError, confirmDialog, spinner,
} from './ui.js';
import { allJobs, runAction } from './jobs.js';

/* ================================================================ constants */

const SECTIONS = [
  ['set-folders', 'Folders'], ['set-downloads', 'Downloads'], ['set-transcription', 'Transcription'],
  ['set-signin', 'Sign-in'], ['set-advanced', 'Advanced'], ['set-about', 'About'],
];
// Download tab choices that 'Reset to defaults' puts back (D-06 key map).
const DL_KEYS = [
  'dl_mode', 'dl_quality', 'dl_compatible', 'dl_audio_codec', 'dl_container', 'dl_subtitles', 'dl_subtitle_langs',
  'dl_auto_subs', 'dl_embed_subs', 'sponsorblock', 'embed_chapters', 'dl_split_chapters', 'recode_encoder',
  'recode_quality', 'normalize_audio', 'write_description', 'write_comments', 'dl_max_comments',
  'dl_write_thumbnail', 'convert_thumbnails', 'dl_write_link', 'write_info_json',
];
const BROWSERS = {
  firefox: 'Firefox', librewolf: 'LibreWolf', chrome: 'Chrome', edge: 'Edge', brave: 'Brave',
  opera: 'Opera', vivaldi: 'Vivaldi', chromium: 'Chromium', whale: 'Whale',
};
const LOGIN_SITES = [
  { label: 'YouTube', url: 'https://accounts.google.com/ServiceLogin?service=youtube', domain: 'youtube.com' },
  { label: 'Instagram', url: 'https://www.instagram.com/accounts/login/', domain: 'instagram.com' },
  { label: 'TikTok', url: 'https://www.tiktok.com/login', domain: 'tiktok.com' },
  { label: 'X', url: 'https://x.com/login', domain: 'x.com' },
  { label: 'Facebook', url: 'https://www.facebook.com/login', domain: 'facebook.com' },
];
const IMPERSONATE = [['chrome', 'Chrome'], ['edge', 'Edge'], ['safari', 'Safari'], ['firefox', 'Firefox']];
const PRESETS = [
  ['title_id', 'Title [video ID]'], ['title', 'Title'], ['channel_title', 'Channel – Title'],
  ['date_title', 'Upload date – Title'], ['custom', 'Custom…'],
];
// The example video for the 'File names' preview (S-10).
const SAMPLE = {
  title: 'Big Buck Bunny', id: 'aqz-KE-bpKQ', ext: 'mp4', uploader: 'Blender', channel: 'Blender',
  upload_date: '20141110', playlist_title: 'Blender Open Movies', playlist_index: '01', resolution: '1920x1080',
  height: '1080', width: '1920', duration_string: '10:35', extractor: 'youtube', webpage_url_domain: 'youtube.com',
};
const NODE_URL = 'https://nodejs.org/';
// '23 Sep' as the copy has it (en-GB Intl now says 'Sept').
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const dayMonth = (epoch) => { const d = new Date(Number(epoch) * 1000); return Number.isFinite(d.getTime()) ? `${d.getDate()} ${MONTHS[d.getMonth()]}` : ''; };

/* ================================================================ state */

const ui = {};                 // element references
const bindings = {};
let lastDetect = null;         // this session's browser check (S-05)
let signinMode = 'idle';       // idle | change | pick | wait | done
let loginSite = null;
let ffmpegFromHere = false;
let gpuError = '';
let ffmpegError = '';
let modelTimer = null;
let modelPoke = false;
let lastPoke = 0;
const modelErrors = {};        // name -> message
const railState = { current: '', pinned: '', holdPin: false };

/* ================================================================ small builders */

/** A two-column settings row (S-02): label on the left, control on the right. */
function row(label, control, opts = {}) {
  const labelEl = label ? el(opts.forId ? 'label.label' : 'span.label', { for: opts.forId || null }, label) : null;
  return el('div.setting-row', { class: opts.cls || null, id: opts.id || null, hidden: !!opts.hidden },
    el('div.setting-label', labelEl, opts.labelHelp ? el('span.help', opts.labelHelp) : null),
    el('div.setting-control', control, opts.help ? el('span.help', opts.help) : null));
}
/** A full-width checkbox row: the checkbox's own text is the label. */
function checkRow(id, text, help, opts = {}) {
  const input = el('input', { type: 'checkbox', id });
  const lab = el('label.check', input, el('span.check-text', el('span', text), help ? el('span.help', help) : null));
  const r = el('div.setting-row.full', { hidden: !!opts.hidden }, el('div.setting-control', lab));
  return { row: r, input };
}
const textInput = (id, attrs = {}) => el('input', { type: 'text', id, spellcheck: 'false', autocomplete: 'off', ...attrs });
const numInput = (id, attrs = {}) => el('input.input-num', { type: 'number', id, inputmode: 'decimal', ...attrs });
const withUnit = (input, unit) => el('div.row.nowrap.set-unit', input, el('span.muted', unit));
/** Text as a sentence: 'Internal Server Error' -> 'Internal Server Error.' */
const sentence = (t) => { const s = String(t || '').trim(); return !s || /[.!?…]$/.test(s) ? s : `${s}.`; };
/** One inline notice for a section whose data failed to load (G-09), with Try again. */
function loadNotice(title, err, retry) {
  const again = button('Try again', 'secondary', { size: 'sm', onClick: () => withBusy(again, 'Trying…', retry).catch(() => {}) });
  return callout('warn', { title, text: sentence(err?.text || err?.message), actions: [again] });
}
const saveStateOf = (cardEl) => cardEl?.querySelector('.card-head .save-state');

/* ================================================================ mount */

export function mount(section) {
  ui.section = section;
  ui.configNotice = el('div.set-notice', { role: 'status' });
  const main = el('div.set-main',
    ui.configNotice, buildHealth(), buildFolders(), buildDownloads(), buildTranscription(), buildSignin(), buildAdvanced(), buildAbout());
  section.append(
    pageHead('Settings'),
    el('div.set-layout', buildRail(), main),
  );

  on('config', ({ source }) => {
    renderFileNames();
    renderSignin();
    renderSources();
    renderSystem();
    if (source === 'load') { renderFolderProblems(); setConfigLocked(false); fillFocusedPaths(); }
  });
  on('settings-meta', () => { renderFolderProblems(); renderSignin(); });
  on('hardware', () => { renderTranscription(); renderEngine(); renderHealth(); renderSystem(); });
  on('packs', () => { renderTranscription(); renderHealth(); });
  on('pack-done', onPackDone);
  on('models', renderModels);
  // A transcript that downloads a speech model shows it here as it arrives, and
  // a transcript that ended any way other than done still leaves the model on
  // disk (the shared layer refreshes after 'done').
  on('jobs', ({ jobs }) => {
    if (modelTimer || modelPoke || currentTab() !== 'settings' || Date.now() - lastPoke < 2000) return;
    const fetching = jobs.some((j) => j.kind === 'transcript' && j.status === 'running'
      && (j.steps || []).some((st) => st.key === 'model' && st.state === 'active'));
    if (fetching) { modelPoke = true; lastPoke = Date.now(); loadModels().catch(() => {}).finally(() => { modelPoke = false; }); }
  });
  on('job-transition', ({ job, to }) => {
    if (job.kind !== 'transcript' || to === 'done' || !['error', 'cancelled', 'skipped'].includes(to)) return;
    loadModels().catch(() => {});
    loadHardware().catch(() => {});
  });
  on('capabilities', () => { renderNetworkCaps(); renderSources(); renderHealth(); renderSystem(); });
  on('about', () => { renderAbout(); renderSystem(); });
  on('load-error', ({ what }) => {
    if (what === 'hardware' || what === 'packs') renderTranscription();
    if (what === 'models') renderModels();
    if (what === 'about') renderAbout();
    if (what === 'capabilities') { renderNetworkCaps(); renderHealth(); }
  });

  registerHook('settings.reveal', reveal);
  setupRail();

  // About is not part of the startup loaders; it is cheap, so fetch it now.
  loadAbout().catch(() => {});
  renderAll();
}

/** Settings: focus the first field (G-05). */
export function focus() {
  // Not before the saved folders are known: an empty focused field would save itself empty.
  if (isConfigLoaded()) ui.dlDir?.focus({ preventScroll: true });
  else titleOf('set-folders')?.focus({ preventScroll: true });
}

/**
 * Settings arrived while a folder field had focus (Settings opened during
 * startup, or after Try again): bindSetting leaves a focused field alone, which
 * would keep it empty and then save the empty value on blur. Fill it once.
 */
function fillFocusedPaths() {
  for (const [key, input] of [['download_dir', ui.dlDir], ['transcript_dir', ui.trDir]]) {
    if (document.activeElement !== input || input.value || !CONFIG[key]) continue;
    input.blur();
    bindings[key].apply();
    input.focus({ preventScroll: true });
  }
}

/** Fresh numbers every time the tab is opened (UI-16, FIN-14). */
export function onShow() {
  loadModels().catch(() => {});
  loadPacks().catch(() => {});
  if (!STATE.about) loadAbout().catch(() => {});
  if (STATE.loadErrors.capabilities) loadCapabilities().catch(() => {});
  if (!isConfigLoaded()) retryConfig();
  requestAnimationFrame(() => {
    updateRail();
    // The tab was hidden when the paths arrived: show their ends now.
    for (const input of [ui.dlDir, ui.trDir]) if (document.activeElement !== input) input.scrollLeft = input.scrollWidth;
  });
}

/**
 * The settings themselves could not be read (G-09): one notice at the top of
 * the page with Try again, instead of a page of empty fields. The shell's
 * startup load does not report a failure, so Settings asks again when opened.
 */
async function retryConfig() {
  try {
    await loadConfig();
    setConfigLocked(false);
  } catch (e) {
    // Until the saved values are known, the fields that hold them stay locked:
    // leaving an empty field would otherwise save the empty value over them.
    setConfigLocked(true);
    ui.configNotice.replaceChildren(loadNotice("Couldn't read your settings", e, retryConfig));
  }
}
function setConfigLocked(locked) {
  if (!locked) ui.configNotice.replaceChildren();
  for (const c of [ui.foldersCard, ui.dlCard, ui.signinCard, ui.advCard]) {
    c.inert = locked;
    c.classList.toggle('is-locked', locked);
  }
}

function renderAll() {
  renderHealth(); renderFolderProblems(); renderFileNames(); renderTranscription(); renderModels();
  renderSignin(); renderSources(); renderNetworkCaps(); renderEngine(); renderAbout(); renderSystem();
}

/* ================================================================ rail (S-08) */

function buildRail() {
  ui.rail = el('nav.set-rail', { 'aria-label': 'Settings sections' });
  ui.railLinks = {};
  for (const [id, label] of SECTIONS) {
    const a = el('a', { href: `#${id}` }, label);
    a.addEventListener('click', (e) => { e.preventDefault(); goSection(id); });
    ui.railLinks[id] = a;
    ui.rail.append(a);
  }
  return ui.rail;
}
function goSection(id) {
  const target = document.getElementById(id);
  if (!target) return;
  railState.pinned = id;
  railState.holdPin = true;
  setRailCurrent(id);
  scrollToEl(target, 'start');
  const h = document.getElementById(`${id}-title`);
  if (h) { h.tabIndex = -1; h.focus({ preventScroll: true }); }
}
function setRailCurrent(id) {
  if (railState.current === id) return;
  railState.current = id;
  for (const [sid, a] of Object.entries(ui.railLinks)) {
    if (sid === id) a.setAttribute('aria-current', 'true');
    else a.removeAttribute('aria-current');
  }
  // In the narrow chip row, keep the current chip in view without moving the page.
  const a = ui.railLinks[id];
  if (a && ui.rail.scrollWidth > ui.rail.clientWidth) {
    const r = a.getBoundingClientRect(), n = ui.rail.getBoundingClientRect();
    if (r.left < n.left || r.right > n.right) ui.rail.scrollLeft += r.left - n.left - 16;
  }
}
/** Highlight the section under the top of the page; the last one at the very bottom. */
function updateRail() {
  if (ui.section.hidden) return;
  if (railState.pinned) { setRailCurrent(railState.pinned); return; }
  const line = 52 + 120;
  let current = SECTIONS[0][0];
  for (const [id] of SECTIONS) {
    const node = document.getElementById(id);
    if (node && node.getBoundingClientRect().top <= line) current = id;
  }
  const doc = document.documentElement;
  if (window.scrollY > 0 && window.innerHeight + window.scrollY >= doc.scrollHeight - 4) current = SECTIONS[SECTIONS.length - 1][0];
  setRailCurrent(current);
}
function setupRail() {
  let queued = false;
  addEventListener('scroll', () => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => { queued = false; updateRail(); });
  }, { passive: true });
  // A click pins its section until the scroll it caused ends; the user's own
  // scrolling (wheel, keys, touch) takes over again.
  addEventListener('scrollend', () => {
    if (!railState.pinned) return;
    const node = document.getElementById(railState.pinned);
    const r = node?.getBoundingClientRect();
    if (!r || r.bottom < 60 || r.top > window.innerHeight) railState.pinned = '';
    railState.holdPin = false;
    updateRail();
  });
  const release = () => { if (railState.pinned && !railState.holdPin) { railState.pinned = ''; updateRail(); } };
  addEventListener('wheel', release, { passive: true });
  addEventListener('touchstart', release, { passive: true });
  addEventListener('keydown', (e) => { if (['PageDown', 'PageUp', 'ArrowDown', 'ArrowUp', 'Home', 'End', ' '].includes(e.key)) { railState.holdPin = false; release(); } });
  on('tab', ({ id }) => { if (id === 'settings') { railState.pinned = ''; requestAnimationFrame(updateRail); } });
}

/* ================================================================ reveal (G-07 fix buttons) */

function reveal(target) {
  requestAnimationFrame(() => {
    let node = null, focusEl = null, flashEl = null, block = 'start';
    const problems = SETTINGS_META.folder_problems || {};
    switch (target) {
      case 'signin':
        node = ui.signinCard; focusEl = firstVisible([ui.ckDetect, ui.ckChange]) || titleOf('set-signin'); break;
      case 'proxy':
        ui.network.open = true; node = ui.proxy; focusEl = ui.proxy; flashEl = ui.proxy.closest('.setting-row'); block = 'center'; break;
      case 'choose_folder':
      case 'folders': {
        const onlyTr = problems.transcript_dir && !problems.download_dir;
        node = ui.foldersCard; focusEl = onlyTr ? ui.trChange : ui.dlChange; flashEl = ui.foldersCard; break;
      }
      case 'health':
        node = ui.health.hidden ? ui.foldersCard : ui.health;
        focusEl = ui.health.hidden ? titleOf('set-folders') : firstVisible([ui.ffBtn, ui.nodeBtn]) || ui.health; break;
      case 'transcription':
        node = ui.trCard; focusEl = firstVisible([ui.gpuBtn]) || titleOf('set-transcription'); break;
      case 'sites':
        node = ui.siteSearch; focusEl = ui.siteSearch; flashEl = ui.siteSearch.closest('.setting-row'); block = 'center'; break;
      case 'about':
        node = ui.aboutCard; focusEl = titleOf('set-about'); break;
      default:
        node = typeof target === 'string' ? document.querySelector(target) : null; focusEl = node;
    }
    if (!node) return;
    for (let d = node.closest('details'); d; d = d.parentElement?.closest('details')) d.open = true;
    const sectionId = node.closest('.set-main > .card')?.id;
    if (sectionId) { railState.pinned = sectionId; railState.holdPin = true; setRailCurrent(sectionId); }
    scrollToEl(node, block);
    if (focusEl) {
      if (!focusEl.matches('input, select, textarea, button, a, [tabindex]')) focusEl.tabIndex = -1;
      focusEl.focus({ preventScroll: true });
    }
    flash(flashEl || node);
  });
}
const titleOf = (id) => { const h = document.getElementById(`${id}-title`); if (h) h.tabIndex = -1; return h; };
const firstVisible = (list) => list.find((n) => n && !n.hidden && n.isConnected && !n.closest('[hidden]') && !n.disabled) || null;

/* ================================================================ health banner (S-03) */

function buildHealth() {
  ui.ffText = el('div.callout-text', 'ffmpeg is missing, so most downloads will fail.');
  ui.ffProgress = el('div.health-progress', { hidden: true },
    el('div.bar', { role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-label': 'Installing ffmpeg' }, el('i')),
    el('span.help.tabular'));
  ui.ffError = el('div.field-error', { hidden: true });
  ui.ffBtn = button('Repair', 'secondary', { size: 'sm', onClick: repairFfmpeg });
  ui.ffRow = el('div.callout-row', { hidden: true }, el('div.health-main', ui.ffText, ui.ffProgress, ui.ffError), ui.ffBtn);
  ui.nodeBtn = button('How to install', 'secondary', { size: 'sm', icon: 'external', onClick: () => openUrl(NODE_URL).catch(toastError) });
  ui.nodeRow = el('div.callout-row', { hidden: true },
    el('div.callout-text', 'Some YouTube videos may fail or download in lower quality until Node.js is installed.'), ui.nodeBtn);
  ui.health = el('div#set-health.set-health', { hidden: true },
    el('div.callout.warn', { role: 'status' }, icon('alert'), el('div.callout-body', el('div.callout-rows', ui.ffRow, ui.nodeRow))));
  return ui.health;
}
function renderHealth() {
  const packs = STATE.packs;
  const pr = packs?.progress || {};
  const ffBusy = !!pr.busy && pr.task === 'ffmpeg';
  const ffMissing = (packs && packs.ffmpeg_installed === false) || (STATE.hw && STATE.hw.ffmpeg === false);
  ui.ffRow.hidden = !(ffMissing || ffBusy || ffmpegFromHere || ffmpegError);
  ui.ffProgress.hidden = !ffBusy;
  if (ffBusy) {
    const pct = Math.max(0, Math.min(100, Math.round(pr.percent || 0)));
    const bar = ui.ffProgress.querySelector('.bar');
    bar.setAttribute('aria-valuenow', String(pct));
    bar.firstChild.style.width = `${pct}%`;
    ui.ffProgress.querySelector('.help').textContent = pr.message || `Installing ffmpeg · ${pct}%`;
    if (!ui.ffBtn._idle) setBusy(ui.ffBtn, 'Repairing…');
  } else if (!ffmpegFromHere && ui.ffBtn._idle) clearBusy(ui.ffBtn);
  const otherBusy = packInstallRunning() && !ffBusy && !ffmpegFromHere;
  if (!ui.ffBtn._idle) {
    ui.ffBtn.disabled = otherBusy;
    ui.ffBtn.title = otherBusy ? 'Another download is running. Try again when it finishes.' : '';
  }
  ui.ffError.hidden = !ffmpegError;
  ui.ffError.textContent = ffmpegError;
  ui.nodeRow.hidden = !(STATE.cap && !STATE.cap.js_runtime);
  ui.health.hidden = ui.ffRow.hidden && ui.nodeRow.hidden;
}
async function repairFfmpeg() {
  ffmpegError = '';
  ffmpegFromHere = true;
  setBusy(ui.ffBtn, 'Repairing…');
  renderHealth();
  installPack('ffmpeg');   // progress arrives as 'packs' events; the outcome as 'pack-done'
}
function onPackDone({ kind, result }) {
  const ok = result && result.ok !== false;
  if (kind === 'ffmpeg' && ffmpegFromHere) {
    ffmpegFromHere = false;
    clearBusy(ui.ffBtn);
    if (ok) {
      ffmpegError = '';
      toast('ffmpeg is installed.', { tone: 'ok' });
      // Jobs that failed only because ffmpeg was missing start again on their own.
      for (const j of allJobs()) {
        if (j.status === 'error' && j.error?.code === 'ffmpeg_missing') post(`/api/jobs/${j.id}/retry`, {}).catch(() => {});
      }
    } else ffmpegError = result?.error || "Couldn't install ffmpeg.";
  }
  if (kind === 'gpu') {
    if (ok) {
      gpuError = '';
      toast('GPU support is installed. Transcription now runs on your graphics card.', { tone: 'ok' });
    } else {
      gpuError = result?.error || "Couldn't install GPU support.";
      if (currentTab() !== 'settings') toast(gpuError, { tone: 'err' });
    }
  }
  renderHealth();
  renderTranscription();
}

/* ================================================================ folders (S-02) */

function buildFolders() {
  const folder = (key, id, changeId, label) => {
    const input = textInput(id, { dir: 'auto', class: 'path-input' });
    const change = button('Change…', 'secondary', { size: 'sm', id: changeId });
    const open = button('Open', 'quiet', { size: 'sm', title: `Open the ${label.toLowerCase()} folder` });
    // A path longer than the field shows its end, the folder's own name; title= has it all.
    const showEnd = () => requestAnimationFrame(() => { if (document.activeElement !== input) input.scrollLeft = input.scrollWidth; });
    bindings[key] = bindSetting(input, key, {
      fromServer: (v) => { input.title = v || ''; showEnd(); return v; },
      onSaved: () => { input.title = input.value; showEnd(); },
    });
    input.addEventListener('blur', showEnd);
    change.addEventListener('click', () => chooseFolder(key, input, change));
    open.addEventListener('click', async () => {
      try { await revealPath(CONFIG[key] || input.value); } catch (e) { toastError(e); }
    });
    return { input, change, row: row(label, el('div.path-row', input, change, open), { forId: id }) };
  };
  const dl = folder('download_dir', 'setDlDir', 'setDlChange', 'Downloads');
  const tr = folder('transcript_dir', 'setTrDir', 'setTrChange', 'Transcripts');
  Object.assign(ui, { dlDir: dl.input, trDir: tr.input, dlChange: dl.change, trChange: tr.change });
  ui.foldersCard = card({
    id: 'set-folders', title: 'Folders', saveState: true, cls: 'set-card',
    body: [dl.row, tr.row, row('', el('span.help', 'Live recordings are saved in Downloads too.'), { cls: 'cont' })],
  });
  return ui.foldersCard;
}
async function chooseFolder(key, input, btn) {
  let picked = '';
  try {
    picked = await withBusy(btn, 'Opening…', () => pickFolder(input.value.trim() || CONFIG[key] || ''));
  } catch (e) {
    setFieldError(input, `Couldn't open the folder picker: ${e.text || e.message}`);
    return;
  }
  if (!picked) return;
  input.value = picked;
  input.title = picked;
  await bindings[key].save();       // Change… saves at once (S-01)
}
/** A folder that can't be used (UI-4): the reason sits under its row. */
function renderFolderProblems() {
  const problems = SETTINGS_META.folder_problems || {};
  for (const [key, input] of [['download_dir', ui.dlDir], ['transcript_dir', ui.trDir]]) {
    const msg = problems[key] || '';
    if (msg) { setFieldError(input, msg); input.dataset.problem = '1'; }
    else if (input.dataset.problem) { setFieldError(input, ''); delete input.dataset.problem; }
  }
}

/* ================================================================ downloads (S-02, S-10) */

function buildDownloads() {
  const art = checkRow('setEmbed', 'Add cover art and title/artist details to files');
  bindings.embed = bindSetting(art.input, 'embed_thumbnail', {
    fromServer: (_v, c) => !!(c.embed_thumbnail && c.embed_metadata),
    patch: (v) => ({ embed_thumbnail: v, embed_metadata: v }),
  });
  const mtime = checkRow('setMtime', 'Date files by when the video was uploaded',
    'Off: files are dated today, so new downloads appear at the top in File Explorer.');
  bindings.set_mtime = bindSetting(mtime.input, 'set_mtime');

  ui.resetBtn = button('Reset to defaults', 'quiet', { size: 'sm', onClick: resetDownloadOptions });
  const options = row('Download options',
    el('div.set-inline', el('span.set-text.is-sm', 'Quality, format and extras you pick on the Download tab are remembered.'), ui.resetBtn));

  // S-10: file names as presets, the raw pattern only under Custom…
  ui.preset = el('select.select-md#setFilePreset', {}, PRESETS.map(([v, l]) => el('option', { value: v }, l)));
  bindings.filename_preset = bindSetting(ui.preset, 'filename_preset', { onSaved: renderFileNames });
  ui.preset.addEventListener('change', renderFileNames);
  ui.example = el('span.help.tabular', { dir: 'auto' });
  ui.pattern = textInput('setPattern', { class: 'mono-input', dir: 'ltr' });
  bindings.output_template = bindSetting(ui.pattern, 'output_template', {
    patch: (v) => ({ output_template: v.trim(), filename_preset: 'custom' }),
    validate: (v) => (v.trim() ? '' : 'Enter a file name pattern.'),
    onSaved: renderFileNames,
  });
  ui.pattern.addEventListener('input', renderFileNames);
  ui.patternRow = el('div.set-pattern', { hidden: true },
    el('label.label', { for: 'setPattern' }, 'File name pattern'),
    ui.pattern,
    el('span.help', "Advanced: uses yt-dlp's output template syntax."));
  const names = row('File names', [ui.preset, ui.example, ui.patternRow], { forId: 'setFilePreset' });

  ui.dlCard = card({
    id: 'set-downloads', title: 'Downloads', saveState: true, cls: 'set-card',
    body: [art.row, mtime.row, options, names],
  });
  return ui.dlCard;
}
async function resetDownloadOptions() {
  const patch = {};
  for (const k of DL_KEYS) if (k in DEFAULTS) patch[k] = DEFAULTS[k];
  const state = saveStateOf(ui.dlCard);
  showSaveState(state, 'saving');
  try {
    await withBusy(ui.resetBtn, 'Resetting…', () => saveSettings(patch, 'settings-reset'));
    showSaveState(state, 'saved');
  } catch (e) {
    showSaveState(state, 'error', `Couldn't save: ${e.text || e.message}`);
  }
}
/** Fill a yt-dlp output template with the sample video, for the example line. */
function sampleName(template) {
  return String(template || '').replace(/%\(([^)]*)\)([-#0+ ]*\d*(?:\.\d+)?[A-Za-z])/g, (_m, spec, conv) => {
    const [head, def = ''] = spec.split('|');
    const [fields, fmt] = head.split('>');
    for (const f of fields.split(',')) {
      let v = SAMPLE[f.trim()];
      if (v === undefined) continue;
      if (fmt && /^\d{8}$/.test(v)) v = fmt.replace('%Y', v.slice(0, 4)).replace('%m', v.slice(4, 6)).replace('%d', v.slice(6, 8));
      const width = /\.(\d+)B$/.exec(conv);
      return width ? String(v).slice(0, Number(width[1])) : String(v);
    }
    return def || 'NA';
  });
}
function renderFileNames() {
  if (!ui.preset) return;
  const preset = ui.preset.value || 'title_id';
  const custom = preset === 'custom';
  ui.patternRow.hidden = !custom;
  const template = custom ? (ui.pattern.value || CONFIG.output_template) : (SETTINGS_META.filename_presets?.[preset] || CONFIG.output_template);
  const name = sampleName(template);
  ui.example.textContent = name ? `Example: ${name}` : '';
}

/* ================================================================ transcription (S-04, G-12) */

function buildTranscription() {
  ui.gpuText = el('div.gpu-text');
  ui.gpuBtn = button('Download GPU support', 'secondary', { onClick: downloadGpu });
  ui.gpuLicense = el('p.help.gpu-license');
  ui.gpuProgress = el('div.gpu-progress', { hidden: true },
    el('div.bar', { role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-label': 'Downloading GPU support' }, el('i')),
    el('span.help.tabular'));
  ui.gpuError = el('div.gpu-error');
  ui.gpuRemove = button('Remove', 'quiet', { size: 'sm', onClick: removeGpu });
  ui.gpuInstalled = el('div.list-row.gpu-installed', { hidden: true }, el('span.list-text'), ui.gpuRemove);
  ui.gpuBox = el('div.gpu-block',
    el('div.gpu-line', ui.gpuText, ui.gpuBtn), ui.gpuLicense, ui.gpuProgress, ui.gpuError, ui.gpuInstalled);

  ui.modelList = el('div.list-rows.model-list');
  ui.modelEmpty = el('p.help', { hidden: true });
  ui.modelNotice = el('div');
  ui.trCard = card({
    id: 'set-transcription', title: 'Transcription', cls: 'set-card',
    body: [
      ui.gpuBox,
      el('hr.divider'),
      el('h3.set-h3#setModelsTitle', { tabindex: '-1' }, 'Speech models on this PC'),
      ui.modelNotice, ui.modelList, ui.modelEmpty,
      el('p.mt-3', linkButton('Choose the speech model and language in Options on the Transcript tab.', () => runAction('smaller_model', null))),
    ],
  });
  return ui.trCard;
}
function renderTranscription() {
  if (!ui.gpuText) return;
  const hw = STATE.hw;
  const packs = STATE.packs;
  const pr = packs?.progress || {};
  if (!hw) {
    const err = STATE.loadErrors.hardware;
    ui.gpuText.replaceChildren(err
      ? loadNotice("Couldn't check this PC's hardware", err, loadHardware)
      : el('span.skeleton-row', spinner(), 'Checking this PC…'));
    for (const n of [ui.gpuBtn, ui.gpuLicense, ui.gpuProgress, ui.gpuInstalled]) n.hidden = true;
    return;
  }
  const name = hw.nvidia_name || '';
  const size = hw.gpu_pack_size_mb || packs?.gpu_pack_size_mb || 0;
  const gpuBusy = !!pr.busy && pr.task === 'gpu';
  if (hw.gpu_ready) ui.gpuText.replaceChildren('Transcription runs on your ', bdi(name), '. Fast.');
  else if (name) ui.gpuText.replaceChildren('Transcription runs on your processor. Your ', bdi(name), ` can do it many times faster after a one-time ${num(size)} MB download.`);
  else ui.gpuText.replaceChildren(`Transcription runs on your processor (${num(hw.cpu_threads || 0)} threads). Videos with captions are still instant.`);

  const offer = !!name && !hw.gpu_ready && !gpuBusy;
  ui.gpuBtn.hidden = !offer;
  const otherBusy = packInstallRunning() && !gpuBusy;
  ui.gpuBtn.disabled = otherBusy;
  ui.gpuBtn.title = otherBusy ? 'Another download is running. Try again when it finishes.' : '';
  // The licence goes next to the button, before anything is downloaded.
  const lic = packs?.gpu_pack_license;
  ui.gpuLicense.hidden = !(offer && lic?.text);
  if (lic?.text) {
    ui.gpuLicense.replaceChildren(lic.text, ' ',
      lic.url ? linkButton(lic.name || 'License terms', () => openUrl(lic.url).catch(toastError)) : '');
  }
  ui.gpuProgress.hidden = !gpuBusy;
  if (gpuBusy) {
    const pct = Math.max(0, Math.min(100, Math.round(pr.percent || 0)));
    const bar = ui.gpuProgress.querySelector('.bar');
    bar.setAttribute('aria-valuenow', String(pct));
    bar.firstChild.style.width = `${pct}%`;
    ui.gpuProgress.querySelector('.help').textContent = pr.message || `Downloading GPU support · ${pct}%`;
  }
  ui.gpuError.replaceChildren(gpuError && !gpuBusy ? callout('err', gpuError) : '');
  const installed = !!packs?.gpu_pack_installed && !gpuBusy;
  ui.gpuInstalled.hidden = !installed;
  if (installed) {
    ui.gpuInstalled.firstChild.textContent = `GPU support · Installed · ${num(packs.gpu_pack_disk_mb || size)} MB`;
  }
}
function downloadGpu() {
  gpuError = '';
  installPack('gpu');       // polls from the first moment (UI-9); outcome via 'pack-done'
  renderTranscription();
  requestAnimationFrame(() => {
    // Keep keyboard focus in the section while the button is replaced by the bar.
    if (ui.gpuBtn.hidden) { const h = titleOf('set-transcription'); h?.focus({ preventScroll: true }); }
  });
}
async function removeGpu() {
  const mb = num(STATE.packs?.gpu_pack_disk_mb || STATE.hw?.gpu_pack_size_mb || 0);
  const yes = await confirmDialog({
    title: `Remove GPU support (${mb} MB)?`,
    body: 'Transcription will run on the processor until you download it again.',
    confirm: 'Remove',
  });
  if (!yes) return;
  try {
    const r = await withBusy(ui.gpuRemove, 'Removing…', () => post('/api/packs/gpu/remove', {}));
    if (r.ok === false) { gpuError = r.error || "Couldn't remove GPU support."; }
    else { gpuError = ''; toast(r.message || 'GPU support is removed.', { tone: 'ok' }); }
  } catch (e) {
    gpuError = e.text || e.message;
  }
  await Promise.allSettled([loadPacks(), loadHardware()]);
  renderTranscription();
}

/* ---------------------------------------------------------------- models */

const modelRows = new Map();   // name -> {row, text, action, bar, err, state}
function modelCatalog(name) {
  return (STATE.hw?.models || []).find((m) => m.id === name) || null;
}
function renderModels() {
  if (!ui.modelList) return;
  const m = STATE.models;
  const err = STATE.loadErrors.models;
  ui.modelNotice.replaceChildren(!m && err ? loadNotice("Couldn't list the speech models", err, loadModels) : '');
  if (!m) { ui.modelEmpty.hidden = true; return; }
  const downloads = new Map((m.downloads || []).map((d) => [d.name, d]));
  const items = [];
  for (const r of m.models || []) {
    const dl = downloads.get(r.name);
    downloads.delete(r.name);
    const busy = r.downloading || (dl && dl.busy);
    items.push({ name: r.name, label: r.label || r.name, size: r.size_label || bytes((r.size_mb || 0) * 1048576), state: busy ? 'downloading' : r.ok ? 'ready' : 'damaged', dl, row: r });
  }
  // A model that is downloading has no folder yet on the first poll.
  for (const d of downloads.values()) {
    if (d.busy) items.push({ name: d.name, label: d.label || d.name, size: modelCatalog(d.name)?.size_label || '', state: 'downloading', dl: d });
    else if (d.error) modelErrors[d.name] = `Couldn't download ${d.label || d.name}: ${d.error}`;
  }
  const seen = new Set();
  let prev = null;
  for (const it of items) {
    seen.add(it.name);
    let entry = modelRows.get(it.name);
    if (!entry) {
      entry = { row: el('div.list-row.model-row'), text: el('span.list-text'), err: el('div.field-error'), state: '' };
      entry.bar = el('div.bar', { role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-label': `Downloading ${it.label}` }, el('i'));
      entry.main = el('div.list-main', entry.text, entry.bar, entry.err);
      entry.row.append(entry.main);
      modelRows.set(it.name, entry);
    }
    patchModelRow(entry, it);
    const want = prev ? prev.nextSibling : ui.modelList.firstChild;
    if (entry.row !== want) ui.modelList.insertBefore(entry.row, want);
    prev = entry.row;
  }
  for (const [name, entry] of modelRows) if (!seen.has(name)) { entry.row.remove(); modelRows.delete(name); }

  ui.modelEmpty.hidden = items.length > 0;
  if (!items.length) {
    const chosen = modelCatalog(CONFIG.whisper_model) || modelCatalog(STATE.hw?.recommended_model) || { label: 'Large v3 Turbo', size_label: '1.6 GB' };
    ui.modelEmpty.textContent = `None yet. The first video without captions downloads one (${chosen.size_label} for ${chosen.label}).`;
  }
  const anyBusy = items.some((it) => it.state === 'downloading');
  if (anyBusy && !modelTimer) modelTimer = setInterval(() => loadModels().catch(() => {}), 1000);
  if (!anyBusy && modelTimer) { clearInterval(modelTimer); modelTimer = null; }
}
function patchModelRow(entry, it) {
  const { state, label, size, dl } = it;
  let text;
  if (state === 'ready') text = `${label} · ${size} · Ready`;
  else if (state === 'damaged') text = `${label} · Damaged, will download again when needed`;
  else text = `${label} · Downloading${dl?.bytes_total ? ` · ${mbOf(dl.bytes_done, dl.bytes_total)}` : '…'}`;
  if (entry.text.textContent !== text) entry.text.textContent = text;
  entry.bar.hidden = state !== 'downloading';
  if (state === 'downloading') {
    const pct = dl?.bytes_total ? Math.round(100 * dl.bytes_done / dl.bytes_total) : 0;
    entry.bar.setAttribute('aria-valuenow', String(pct));
    entry.bar.firstChild.style.width = `${pct}%`;
  }
  const errMsg = modelErrors[it.name] || '';
  entry.err.hidden = !errMsg;
  entry.err.textContent = errMsg;
  if (entry.state !== state) {
    // The action only changes with the state, so a focused button survives polling.
    entry.state = state;
    entry.action?.remove();
    entry.action = null;
    if (state === 'ready') entry.action = button('Delete', 'quiet', { size: 'sm', onClick: () => deleteModel(it, entry) });
    if (state === 'damaged') entry.action = button('Download again now', 'secondary', { size: 'sm', onClick: () => redownloadModel(it, entry) });
    if (entry.action) entry.row.append(entry.action);
  }
}
async function deleteModel(it, entry) {
  const yes = await confirmDialog({
    title: `Delete ${it.label} (${it.size})?`,
    body: 'It downloads again the next time a video without captions needs it.',
    confirm: 'Delete',
  });
  if (!yes) return;
  delete modelErrors[it.name];
  try {
    await withBusy(entry.action, 'Deleting…', () => post('/api/models/delete', { name: it.name }));
  } catch (e) {
    modelErrors[it.name] = `Couldn't delete it: ${e.text || e.message}`;
  }
  await loadModels().catch(() => {});
  loadHardware().catch(() => {});   // the Transcript options mark downloaded models
  renderModels();
  // The row (and its button) is gone: keep focus in the list, not on the page.
  if (!entry.row.isConnected || !document.activeElement || document.activeElement === document.body) {
    (ui.modelList.querySelector('button') || document.getElementById('setModelsTitle'))?.focus();
  }
}
async function redownloadModel(it, entry) {
  delete modelErrors[it.name];
  try {
    await withBusy(entry.action, 'Starting…', () => post('/api/models/download', { name: it.name }));
  } catch (e) {
    modelErrors[it.name] = `Couldn't download it: ${e.text || e.message}`;
  }
  await loadModels().catch(() => {});
  renderModels();
}

/* ================================================================ sign-in (S-05) */

function buildSignin() {
  ui.ckState = el('span.signin-state');
  const status = el('div.signin-status', el('span.label', 'Status'), ui.ckState);
  ui.ckDetect = button("Use my browser's sign-ins", 'primary', { onClick: detectBrowsers });
  ui.ckWindow = button('Sign in with a new window…', 'secondary', { onClick: () => { signinMode = 'pick'; renderSignin(); ui.siteBtns[0]?.focus(); } });
  ui.ckChange = button('Change', 'quiet', { onClick: () => { signinMode = 'change'; renderSignin(); ui.ckDetect.focus(); } });
  ui.ckKeep = button('Cancel', 'quiet', { onClick: () => { signinMode = 'idle'; renderSignin(); ui.ckChange.focus(); } });
  ui.ckSignout = button('Sign out', 'quiet', { onClick: signOut });
  ui.ckActions = el('div.row.signin-actions', ui.ckDetect, ui.ckWindow, ui.ckChange, ui.ckKeep, ui.ckSignout);

  // Sign in with a new window: pick a site, sign in there, press Done.
  ui.siteBtns = LOGIN_SITES.map((s) => button(s.label, 'secondary', { size: 'sm', onClick: (e) => openLogin(s, e.currentTarget) }));
  const otherBtn = button('Other…', 'secondary', { size: 'sm' });
  ui.otherUrl = textInput('setLoginUrl', { type: 'url', placeholder: 'e.g. https://example.com/login', dir: 'ltr', 'aria-label': 'Sign-in page address' });
  ui.otherGo = button('Open', 'secondary');
  ui.otherRow = el('div.row.nowrap.signin-other', { hidden: true }, ui.otherUrl, ui.otherGo);
  otherBtn.addEventListener('click', () => { ui.otherRow.hidden = false; ui.otherUrl.focus(); });
  const goOther = () => {
    const url = ui.otherUrl.value.trim();
    if (!/^https?:\/\/\S+$/i.test(url)) { setFieldError(ui.otherUrl, 'Use a web address that starts with https://'); ui.otherUrl.focus(); return; }
    setFieldError(ui.otherUrl, '');
    let domain = '';
    try { domain = new URL(url).hostname.replace(/^(www|accounts|m)\./, ''); } catch (_) {}
    openLogin({ label: domain, url, domain }, ui.otherGo);
  };
  ui.otherGo.addEventListener('click', goOther);
  ui.otherUrl.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); goOther(); } });
  ui.ckPickCancel = button('Cancel', 'quiet', { size: 'sm', onClick: cancelLogin });
  ui.stepPick = el('div.signin-pick',
    el('p.label', 'Which site?'),
    el('div.row', ui.siteBtns, otherBtn),
    ui.otherRow,
    el('div.row', ui.ckPickCancel));
  ui.waitText = el('p');
  ui.ckDone = button("Done, I've signed in", 'primary', { onClick: harvest });
  ui.stepWait = el('div.signin-wait', ui.waitText,
    el('div.row', ui.ckDone, button('Cancel', 'quiet', { onClick: cancelLogin })));
  ui.stepDone = el('div.signin-done');
  ui.stepError = el('div.field-error', { role: 'alert', hidden: true });
  ui.ckStep = el('div.signin-step', { hidden: true }, ui.stepPick, ui.stepWait, ui.stepDone, ui.stepError);
  ui.detectOut = el('div.detect-results', { 'aria-live': 'polite' });

  ui.signinCard = card({
    id: 'set-signin', title: 'Sign-in for private videos', saveState: true, cls: 'set-card',
    sub: 'Only needed for private, members-only or age-restricted videos. Public videos work without it.',
    body: [status, ui.ckActions, ui.ckStep, ui.detectOut],
  });
  return ui.signinCard;
}
const browserLabel = (id) => BROWSERS[id] || (STATE.cap?.browsers || []).find((b) => b.id === id)?.label || id;
function signinSummary() {
  const c = CONFIG;
  if (c.cookies_browser) {
    const hit = lastDetect?.tested?.find((t) => t.browser === c.cookies_browser && t.ok);
    const d = hit?.domains || [];
    const more = d.length > 2 ? ` +${d.length - 2}` : '';
    return { on: true, text: `Using your ${browserLabel(c.cookies_browser)} sign-ins${d.length ? ` · signed in to ${d.slice(0, 2).join(', ')}${more}` : ''}` };
  }
  if (c.cookies_file) {
    const t = SETTINGS_META.cookies_saved;
    return { on: true, text: t ? `Using a sign-in saved on ${dayMonth(t)}` : 'Using a saved sign-in' };
  }
  return { on: false, text: 'Not set up' };
}
function renderSignin() {
  if (!ui.ckState) return;
  const s = signinSummary();
  ui.ckState.replaceChildren(icon(s.on ? 'check' : 'circle', { cls: s.on ? 'is-on' : '' }), el('span', s.text));
  ui.ckState.classList.toggle('is-on', s.on);
  const stepOpen = ['pick', 'wait', 'done'].includes(signinMode);
  const showSetup = !stepOpen && (!s.on || signinMode === 'change');
  ui.ckDetect.hidden = !showSetup;
  ui.ckWindow.hidden = !showSetup;
  ui.ckChange.hidden = stepOpen || !s.on || signinMode === 'change';
  ui.ckKeep.hidden = stepOpen || !s.on || signinMode !== 'change';
  ui.ckSignout.hidden = stepOpen || !s.on;
  ui.ckActions.hidden = [ui.ckDetect, ui.ckWindow, ui.ckChange, ui.ckKeep, ui.ckSignout].every((b) => b.hidden);
  ui.ckStep.hidden = !stepOpen;
  ui.stepPick.hidden = signinMode !== 'pick';
  ui.stepWait.hidden = signinMode !== 'wait';
  ui.stepDone.hidden = signinMode !== 'done';
}
async function detectBrowsers() {
  ui.detectOut.replaceChildren();
  const state = saveStateOf(ui.signinCard);
  let r;
  try {
    r = await withBusy(ui.ckDetect, 'Checking…', () => post('/api/cookies/detect', {}));
  } catch (e) {
    ui.detectOut.replaceChildren(el('p.field-error', `Couldn't check your browsers: ${e.text || e.message}`));
    return;
  }
  lastDetect = r;
  const rows = (r.tested || []).map((t) => el(`div.detect-row${t.ok ? '.is-ok' : '.is-bad'}`,
    icon(t.ok ? 'check' : 'x'),
    el('span', t.ok
      ? `${t.label || browserLabel(t.browser)} · ${plural(t.count, 'cookie')}${t.domains?.length ? ` · signed in to ${t.domains.join(', ')}` : ''}`
      : `${t.label || browserLabel(t.browser)} · ${t.error || "Couldn't read it."}`)));
  if (!(r.working || []).length && r.advice) rows.push(el('p.help', r.advice));
  ui.detectOut.replaceChildren(...rows);
  if (r.best) {
    showSaveState(state, 'saving');
    try {
      // One sign-in source at a time: choosing a browser clears the cookies file.
      await saveSettings({ cookies_browser: r.best, cookies_profile: '', cookies_file: '' }, 'signin');
      showSaveState(state, 'saved');
      signinMode = 'idle';
    } catch (e) {
      showSaveState(state, 'error', `Couldn't save: ${e.text || e.message}`);
    }
  }
  renderSignin();
  // The button that was pressed is gone once a sign-in is in use: keep focus in the section.
  if (ui.ckDetect.hidden) firstVisible([ui.ckChange, ui.ckSignout])?.focus();
}
async function openLogin(site, btn) {
  loginSite = site;
  ui.stepError.hidden = true;
  let r;
  try {
    r = await withBusy(btn, 'Opening…', () => post('/api/cookies/login', { url: site.url }));
  } catch (e) {
    r = { ok: false, error: e.text || e.message };
  }
  if (!r.ok) { ui.stepError.textContent = r.error || "Couldn't open the sign-in window."; ui.stepError.hidden = false; return; }
  ui.waitText.textContent = `${r.browser || 'The browser'} opened. Sign in there, then come back and press Done.`;
  signinMode = 'wait';
  renderSignin();
  ui.ckDone.focus();
}
async function harvest() {
  ui.stepError.hidden = true;
  let r;
  try {
    r = await withBusy(ui.ckDone, 'Checking…', () => post('/api/cookies/harvest', {}));
  } catch (e) {
    r = { ok: false, error: e.text || e.message };
  }
  if (!r.ok) { ui.stepError.textContent = r.error || "Couldn't read the sign-in window."; ui.stepError.hidden = false; return; }
  const where = loginSite?.domain || (r.sites || [])[0] || 'that site';
  ui.stepDone.replaceChildren(callout('ok', `Signed in. Private videos from ${where} should work now.`),
    el('div.row.mt-2', button('Close', 'quiet', { size: 'sm', onClick: () => { signinMode = 'idle'; renderSignin(); titleOf('set-signin')?.focus(); } })));
  signinMode = 'done';
  lastDetect = null;
  ui.detectOut.replaceChildren();     // the browser check no longer describes what is in use
  await Promise.allSettled([loadConfig(), refreshSettingsMeta()]);
  renderSignin();
  ui.stepDone.querySelector('button')?.focus();
}
function cancelLogin() {
  signinMode = 'idle';
  ui.stepError.hidden = true;
  ui.otherRow.hidden = true;
  renderSignin();
  firstVisible([ui.ckWindow, ui.ckChange])?.focus();
}
async function signOut() {
  const yes = await confirmDialog({ title: 'Stop using saved sign-ins?', body: 'Private videos will stop downloading.', confirm: 'Sign out' });
  if (!yes) return;
  try {
    await withBusy(ui.ckSignout, 'Signing out…', () => post('/api/cookies/clear', {}));
  } catch (e) {
    toastError(e);
    return;
  }
  lastDetect = null;
  signinMode = 'idle';
  ui.detectOut.replaceChildren();
  await Promise.allSettled([loadConfig(), refreshSettingsMeta()]);
  renderSignin();
  ui.ckDetect.focus();
}

/* ================================================================ advanced (S-06) */

function buildAdvanced() {
  ui.network = disclosure('Network', { sum: 'For sites that throttle or block you', id: 'set-network', body: buildNetwork() });
  const files = disclosure('Files', { sum: 'File names and temporary files', body: buildFiles() });
  const engine = disclosure('Transcription engine', { sum: 'Only change these if transcription fails', body: buildEngine() });
  const sources = disclosure('Other sign-in methods', { sum: 'Browser profile, cookies file, pasted cookies', body: buildSources() });
  for (const d of [ui.network, files, engine, sources]) d.classList.add('adv-group');
  ui.advCard = card({
    id: 'set-advanced', title: 'Advanced', saveState: true, cls: 'set-card',
    body: [el('div.adv-groups', ui.network, files, engine, sources)],
  });
  return ui.advCard;
}

function buildNetwork() {
  const rate = numInput('setRate', { min: '0', step: '0.5', 'aria-describedby': 'setRateHelp' });
  bindings.rate_limit = bindSetting(rate, 'rate_limit', {
    fromServer: (v) => rateToMBps(v), toServer: (v) => mbpsToRate(v), validate: validators.min(0, 'Enter 0 or more.'),
  });
  const frag = numInput('setFrag', { min: '1', max: '16', step: '1', inputmode: 'numeric' });
  bindings.concurrent_fragments = bindSetting(frag, 'concurrent_fragments', {
    toServer: (v) => Number(v), validate: (v) => (v === '' ? 'Choose between 1 and 16.' : validators.range(1, 16)(v)),
  });
  ui.proxy = textInput('setProxy', { placeholder: 'e.g. socks5://127.0.0.1:1080', dir: 'ltr', class: 'input-md' });
  bindings.proxy = bindSetting(ui.proxy, 'proxy', { toServer: (v) => v.trim(), validate: (v) => validators.proxy(v.trim()) });
  const sleep = numInput('setSleep', { min: '0', step: '0.5' });
  bindings.sleep_requests = bindSetting(sleep, 'sleep_requests', {
    toServer: (v) => (v === '' ? 0 : Number(v)), validate: validators.min(0, 'Enter 0 or more.'),
  });
  ui.impersonate = el('select.select-short#setImpersonate', {}, el('option', { value: '' }, 'Off'));
  bindings.impersonate = bindSetting(ui.impersonate, 'impersonate', {
    // An exact target saved by 1.1 ('chrome-133:macos-15') shows as its family and is
    // never re-posted unless the user picks something (FIN-5).
    fromServer: (v) => String(v || '').toLowerCase().split(/[-:]/)[0],
  });
  const geo = textInput('setGeo', { maxlength: '2', class: 'input-tiny', dir: 'ltr', autocapitalize: 'characters' });
  bindings.geo_bypass_country = bindSetting(geo, 'geo_bypass_country', {
    toServer: (v) => v.trim().toUpperCase(), validate: (v) => validators.country(v.trim()),
  });
  const ipv4 = checkRow('setIpv4', 'Use IPv4 only');
  bindings.force_ipv4 = bindSetting(ipv4.input, 'force_ipv4');
  const aria = checkRow('setAria2c', 'Download with aria2c', '', { hidden: true });
  bindings.external_downloader = bindSetting(aria.input, 'external_downloader', {
    fromServer: (v) => v === 'aria2c', toServer: (v) => (v ? 'aria2c' : ''),
  });
  ui.ariaRow = aria.row;
  ui.impRow = row('Look like a web browser', ui.impersonate, { forId: 'setImpersonate', help: 'Try this if a site refuses the download.' });
  return [
    row('Limit download speed', withUnit(rate, 'MB/s'), { forId: 'setRate', help: el('span#setRateHelp', 'Leave empty for no limit.') }),
    row('Connections per download', frag, { forId: 'setFrag', help: 'More can be faster. Some sites block you above 4.' }),
    row('Proxy', ui.proxy, { forId: 'setProxy' }),
    row('Wait between requests', withUnit(sleep, 'seconds'), { forId: 'setSleep', help: 'Use this if a site blocks you for downloading too fast, e.g. large playlists.' }),
    ui.impRow,
    row('Request the version for country', geo, { forId: 'setGeo', help: 'Two-letter code such as US. Works on some sites only; use a proxy or VPN for real region locks.' }),
    ipv4.row,
    aria.row,
  ];
}
/** Browser families this PC can imitate, built once per answer (FIN-5: never resets the choice). */
function renderNetworkCaps() {
  if (!ui.impersonate) return;
  const cap = STATE.cap;
  const have = new Set(cap?.impersonate || []);
  const families = IMPERSONATE.filter(([v]) => have.has(v));
  const sig = families.map(([v]) => v).join(',');
  if (ui.impersonate.dataset.sig !== sig) {
    ui.impersonate.dataset.sig = sig;
    ui.impersonate.replaceChildren(el('option', { value: '' }, 'Off'), ...families.map(([v, l]) => el('option', { value: v }, l)));
    bindings.impersonate.apply();
  }
  // A control that would have no effect is hidden (principles).
  ui.impRow.hidden = !!cap && !families.length && !CONFIG.impersonate;
  ui.ariaRow.hidden = !(cap?.aria2c || CONFIG.external_downloader === 'aria2c');
}

function buildFiles() {
  const simple = checkRow('setRestrict', 'Simple file names (English letters and numbers only)',
    'Titles in Persian, Arabic, Chinese and other non-Latin scripts become underscores.');
  bindings.restrict_filenames = bindSetting(simple.input, 'restrict_filenames');
  const temp = checkRow('setTemp', 'Download into a temporary folder first', 'Keeps unfinished files out of your Downloads folder.');
  bindings.use_temp_dir = bindSetting(temp.input, 'use_temp_dir');
  return [simple.row, temp.row];
}

function buildEngine() {
  ui.device = el('select.select-md#setDevice', {},
    el('option', { value: 'auto' }, 'Automatic (recommended)'),
    el('option', { value: 'cuda' }, 'NVIDIA graphics card'),
    el('option', { value: 'cpu' }, 'Processor (CPU)'));
  bindings.whisper_device = bindSetting(ui.device, 'whisper_device');
  ui.compute = el('select.select-md#setCompute', {},
    el('option', { value: 'auto' }, 'Automatic (recommended)'),
    el('option', { value: 'float16' }, 'Half precision (float16)'),
    el('option', { value: 'int8_float16' }, 'Mixed (int8 + float16)'),
    el('option', { value: 'int8' }, 'Low memory (int8)'),
    el('option', { value: 'float32' }, 'Full precision (float32)'));
  bindings.whisper_compute = bindSetting(ui.compute, 'whisper_compute');
  ui.computeRow = row('Precision', ui.compute, { forId: 'setCompute', help: 'Leave on Automatic unless transcription fails or runs out of memory.', hidden: true });
  ui.recheck = button('Re-check hardware', 'secondary', { size: 'sm', onClick: recheck });
  return [
    row('Run transcription on', ui.device, { forId: 'setDevice' }),
    ui.computeRow,
    row('', el('div.stack-1', el('div', ui.recheck), el('span.help', 'Use this after updating your graphics driver.'))),
  ];
}
function renderEngine() {
  if (!ui.device) return;
  const hw = STATE.hw;
  const hasNvidia = !!hw?.nvidia_name;
  const cuda = ui.device.querySelector('option[value=cuda]');
  // Only after the hardware answered: until then nothing is known to be missing.
  const missing = !!hw && !hasNvidia;
  cuda.disabled = missing;
  cuda.textContent = missing ? 'NVIDIA graphics card (no NVIDIA card found)' : 'NVIDIA graphics card';
  ui.computeRow.hidden = !hasNvidia;
}
async function recheck() {
  try {
    await withBusy(ui.recheck, 'Checking…', () => recheckHardware());
    toast('Done. The next transcription picks the fastest setting again.', { tone: 'ok' });
  } catch (e) {
    toastError(e);
  }
}

/* ---------------------------------------------------------------- other sign-in methods */

function buildSources() {
  const radio = (value, text) => el('input', { type: 'radio', name: 'setCkSource', value, 'aria-label': text });
  ui.srcNone = radio('none', 'Nothing (public videos only)');
  ui.srcBrowser = radio('browser', 'My browser');
  ui.srcFile = radio('file', 'A cookies file');
  ui.ckBrowser = el('select.select-short#setCkBrowser', { 'aria-label': 'Browser' });
  ui.ckProfile = textInput('setCkProfile', { placeholder: 'Profile (optional)', 'aria-label': 'Browser profile (optional)', class: 'input-short' });
  ui.ckFile = textInput('setCkFile', { dir: 'auto', 'aria-label': 'Cookies file' });
  ui.ckPick = button('Choose file…', 'secondary', { size: 'sm', onClick: chooseCookiesFile });
  const opt = (input, text, extra) => el('div.src-option',
    el('label.check', input, el('span.check-text', text)), extra ? el('div.src-extra', extra) : null);
  ui.sources = el('fieldset.src-list', {},
    el('legend.sr-only', 'Use sign-ins from'),
    opt(ui.srcNone, 'Nothing (public videos only)'),
    opt(ui.srcBrowser, 'My browser', el('div.row', ui.ckBrowser, ui.ckProfile)),
    opt(ui.srcFile, 'A cookies file', el('div.path-row', ui.ckFile, ui.ckPick)));
  ui.sources.addEventListener('change', (e) => {
    if (e.target.name === 'setCkSource') saveSource(e.target.value);
    if (e.target === ui.ckBrowser) { ui.srcBrowser.checked = true; saveSource('browser'); }
  });
  ui.ckProfile.addEventListener('change', () => { if (ui.srcBrowser.checked) saveSource('browser'); });
  ui.ckFile.addEventListener('change', () => { ui.srcFile.checked = true; saveSource('file'); });
  for (const input of [ui.ckProfile, ui.ckFile]) {
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); input.dispatchEvent(new Event('change')); } });
  }

  ui.paste = el('textarea#setCkPaste.paste-box', {
    rows: '4', spellcheck: 'false', autocomplete: 'off', 'aria-describedby': 'setCkPasteHelp',
    placeholder: 'e.g. sessionid=abc123; csrftoken=def456',
  });
  ui.pasteSite = textInput('setCkSite', { placeholder: 'e.g. youtube.com', dir: 'ltr', class: 'input-short' });
  ui.pasteBtn = button('Save these cookies', 'secondary', { onClick: importCookies });
  const pasteControl = [
    el('div.field', ui.paste,
      el('span.help#setCkPasteHelp', 'Accepts a whole cookies.txt, or a “name=value; name=value” header copied from your browser.')),
    // The button sits beside the site field; an error goes under both.
    el('div.field.paste-foot', el('label.label', { for: 'setCkSite' }, 'Which site? (for a header)'),
      el('div.row.nowrap.paste-row', ui.pasteSite, ui.pasteBtn)),
  ];
  return [
    row('Use sign-ins from', ui.sources),
    row('Paste cookies', pasteControl, { forId: 'setCkPaste' }),
  ];
}
function currentSource() {
  if (CONFIG.cookies_browser) return 'browser';
  if (CONFIG.cookies_file) return 'file';
  return 'none';
}
function renderSources() {
  if (!ui.sources) return;
  // Browsers found on this PC; every supported one until that is known (or when none were found).
  const browsers = (STATE.cap?.browsers || []).map((b) => [b.id, b.label]);
  if (!browsers.length) browsers.push(...Object.entries(BROWSERS));
  if (CONFIG.cookies_browser && !browsers.some(([id]) => id === CONFIG.cookies_browser)) browsers.push([CONFIG.cookies_browser, browserLabel(CONFIG.cookies_browser)]);
  const sig = browsers.map(([id]) => id).join(',');
  if (ui.ckBrowser.dataset.sig !== sig) {
    ui.ckBrowser.dataset.sig = sig;
    ui.ckBrowser.replaceChildren(...browsers.map(([id, label]) => el('option', { value: id }, label)));
  }
  if (!isConfigLoaded() || ui.sources.contains(document.activeElement)) return;
  const src = currentSource();
  ui.srcNone.checked = src === 'none';
  ui.srcBrowser.checked = src === 'browser';
  ui.srcFile.checked = src === 'file';
  if (CONFIG.cookies_browser) ui.ckBrowser.value = CONFIG.cookies_browser;
  ui.ckProfile.value = CONFIG.cookies_profile || '';
  ui.ckFile.value = CONFIG.cookies_file || '';
  ui.ckFile.title = CONFIG.cookies_file || '';
}
/** One source at a time: choosing one clears the others (S-06). */
async function saveSource(kind) {
  const state = saveStateOf(ui.advCard);
  let patch, control = null;
  if (kind === 'none') patch = { cookies_browser: '', cookies_profile: '', cookies_file: '' };
  if (kind === 'browser') {
    if (!ui.ckBrowser.value) return;
    patch = { cookies_browser: ui.ckBrowser.value, cookies_profile: ui.ckProfile.value.trim(), cookies_file: '' };
    control = ui.ckBrowser;
  }
  if (kind === 'file') {
    const path = ui.ckFile.value.trim();
    control = ui.ckFile;
    if (!path) { setFieldError(ui.ckFile, 'Choose a cookies file.'); ui.ckFile.focus(); return; }
    patch = { cookies_file: path, cookies_browser: '', cookies_profile: '' };
  }
  setFieldError(ui.ckFile, '');
  setFieldError(ui.ckBrowser, '');
  showSaveState(state, 'saving');
  try {
    await saveSettings(patch, 'sources');
    showSaveState(state, 'saved');
    lastDetect = null;
    ui.detectOut.replaceChildren();
    refreshSettingsMeta().catch(() => {});
  } catch (e) {
    showSaveState(state, '');
    const target = e.field === 'cookies_file' ? ui.ckFile : e.field === 'cookies_browser' ? ui.ckBrowser : control || ui.ckFile;
    setFieldError(target, e.field ? e.message : `Couldn't save: ${e.text || e.message}`);
  }
}
async function chooseCookiesFile() {
  let path = '';
  try {
    path = await withBusy(ui.ckPick, 'Opening…', () => pickFile(ui.ckFile.value.trim(), 'cookies'));
  } catch (e) {
    setFieldError(ui.ckFile, `Couldn't open the file picker: ${e.text || e.message}`);
    return;
  }
  if (!path) return;
  ui.ckFile.value = path;
  ui.ckFile.title = path;
  ui.srcFile.checked = true;
  saveSource('file');
}
async function importCookies() {
  const text = ui.paste.value;
  setFieldError(ui.paste, '');
  setFieldError(ui.pasteSite, '');
  if (!text.trim()) { setFieldError(ui.paste, 'Paste the cookies first.'); ui.paste.focus(); return; }
  try {
    const r = await withBusy(ui.pasteBtn, 'Saving…', () => post('/api/cookies/import', { text, site: ui.pasteSite.value.trim() }));
    ui.paste.value = '';
    toast(`Saved ${plural(r.cookies || 0, 'cookie')}.`, { tone: 'ok' });
    lastDetect = null;
    ui.detectOut.replaceChildren();
    await Promise.allSettled([loadConfig(), refreshSettingsMeta()]);
  } catch (e) {
    const target = e.field === 'site' ? ui.pasteSite : ui.paste;
    setFieldError(target, e.field ? e.message : `Couldn't save: ${e.text || e.message}`);
    target.focus();
  }
}

/* ================================================================ about (S-07, S-09) */

function buildAbout() {
  ui.version = el('div.about-version', 'Media Toolkit');
  ui.appCheck = button('Check for updates', 'secondary', { size: 'sm', onClick: checkAppUpdate });
  ui.appResult = el('div.update-result', { 'aria-live': 'polite' });
  ui.ytdlp = el('span.set-text.tabular');
  ui.ytdlpCheck = button('Check for update', 'secondary', { size: 'sm', onClick: updateYtdlp });
  ui.ytdlpCheck.hidden = true;
  ui.ytdlpResult = el('div.update-result', { 'aria-live': 'polite' });
  ui.aboutNotice = el('div');
  ui.openData = button('Open data folder', 'secondary', { size: 'sm', icon: 'folder', onClick: () => openAbout('data') });
  ui.openLog = button('Open log file', 'secondary', { size: 'sm', icon: 'file', onClick: () => openAbout('log') });
  ui.openLicenses = button('Third-party licenses', 'secondary', { size: 'sm', icon: 'file', onClick: () => openAbout('notices') });
  ui.openLicenses.hidden = true;

  ui.siteSearch = textInput('siteSearch', { placeholder: 'e.g. instagram', class: 'input-md', 'aria-controls': 'siteResults' });
  ui.siteResults = el('div#siteResults.site-results', { 'aria-live': 'polite' });
  let timer = null, seq = 0;
  const run = async () => {
    const q = ui.siteSearch.value.trim();
    const my = ++seq;
    if (!q) { ui.siteResults.replaceChildren(); return; }
    try {
      const r = await get(`/api/sites?q=${encodeURIComponent(q)}&limit=200`);
      if (my !== seq) return;      // an older, slower answer never overwrites a newer one
      renderSites(r);
    } catch (e) {
      if (my === seq) ui.siteResults.replaceChildren(el('p.field-error', `Couldn't search: ${e.text || e.message}`));
    }
  };
  ui.siteSearch.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(run, 180); });

  ui.sysRows = el('dl.sys-rows');
  ui.sysCopy = button('Copy system details', 'secondary', { size: 'sm', icon: 'copy', onClick: copySystem });
  const system = disclosure('System details', { body: [ui.sysRows, el('div.mt-3', ui.sysCopy)] });
  system.classList.add('sys-details');

  ui.aboutCard = card({
    id: 'set-about', title: 'About', cls: 'set-card',
    body: [
      ui.aboutNotice,
      el('div.about-head', el('div.stack-1', ui.version, ui.appResult), ui.appCheck),
      row('Site support', el('div.stack-1', el('div.set-inline', ui.ytdlp, ui.ytdlpCheck), ui.ytdlpResult)),
      row('Data folder', el('div.row', ui.openData, ui.openLog, ui.openLicenses), { labelHelp: 'Settings, speech models and logs' }),
      row('Check if a site is supported', [ui.siteSearch, ui.siteResults], { forId: 'siteSearch' }),
      system,
    ],
  });
  return ui.aboutCard;
}
function renderAbout() {
  if (!ui.version) return;
  const a = STATE.about;
  const err = STATE.loadErrors.about;
  ui.aboutNotice.replaceChildren(!a && err ? loadNotice("Couldn't read the app details", err, loadAbout) : '');
  ui.version.textContent = a?.version ? `Media Toolkit ${a.version}` : 'Media Toolkit';
  ui.ytdlp.textContent = `yt-dlp ${a?.yt_dlp || STATE.hw?.yt_dlp || ''}`.trim();
  ui.ytdlpCheck.hidden = !a?.can_update_ytdlp;
  ui.openData.disabled = !a?.data_dir;
  ui.openLog.disabled = !a?.log_path;
  ui.openLicenses.hidden = !a?.notices_path;
}
async function openAbout(what) {
  const a = STATE.about || {};
  const btn = { data: ui.openData, log: ui.openLog, notices: ui.openLicenses }[what];
  try {
    await withBusy(btn, 'Opening…', () => (what === 'data' ? revealPath(a.data_dir) : openPath(what === 'log' ? a.log_path : a.notices_path)));
  } catch (e) {
    toastError(e);
  }
}
function resultLine(node, tone, text, action) {
  node.replaceChildren(el(`p.update-line.is-${tone}`, icon(tone === 'ok' ? 'check' : tone === 'err' ? 'alert' : 'info'), el('span', text), action || null));
}
async function checkAppUpdate() {
  ui.appResult.replaceChildren();
  let r;
  try {
    r = await withBusy(ui.appCheck, 'Checking…', () => get('/api/update-check'));
  } catch (e) {
    resultLine(ui.appResult, 'err', e.text || e.message);
    return;
  }
  if (!r.ok) { resultLine(ui.appResult, 'err', r.message || "Couldn't check for updates."); return; }
  if (r.update_available) {
    resultLine(ui.appResult, 'info', r.message || `Version ${r.latest} is available.`,
      r.url ? button('Open download page', 'secondary', { size: 'sm', icon: 'external', onClick: () => openUrl(r.url).catch(toastError) }) : null);
  } else resultLine(ui.appResult, 'ok', r.message || "You're on the latest version.");
}
async function updateYtdlp() {
  ui.ytdlpResult.replaceChildren();
  let r;
  try {
    r = await withBusy(ui.ytdlpCheck, 'Checking…', () => post('/api/update-ytdlp', {}));
  } catch (e) {
    resultLine(ui.ytdlpResult, 'err', `Couldn't update: ${e.text || e.message}`);
    return;
  }
  // 'Up to date (2026.08.19)' · 'Updated to {v}. Restart Media Toolkit to use it.' · "Couldn't update: {reason}"
  resultLine(ui.ytdlpResult, r.ok ? 'ok' : 'err', r.message || (r.ok ? `Up to date (${r.version})` : "Couldn't update."));
  if (r.ok && r.restart_required) loadAbout().catch(() => {});
}
// Well-known sites first, so 'you' finds YouTube before anything obscure.
const POPULAR = ['YouTube', 'Instagram', 'TikTok', 'X', 'Twitter', 'Facebook', 'Twitch', 'Vimeo', 'Reddit', 'Kick',
  'SoundCloud', 'Dailymotion', 'Bilibili', 'Rumble', 'Bandcamp', 'Mixcloud', 'Niconico', 'VK', 'Bluesky', 'Threads'];
const popularity = (name) => { const i = POPULAR.findIndex((p) => p.toLowerCase() === String(name).toLowerCase()); return i < 0 ? POPULAR.length : i; };
function renderSites(r) {
  // Ask for more than we show, rank, then show at most 20 (S-07).
  const matches = (r.matches || []).slice().sort((a, b) => popularity(a) - popularity(b)).slice(0, 20);
  if (!matches.length) {
    ui.siteResults.replaceChildren(el('p.help', 'Not in the list, but many sites still work. Paste the link on the Download tab to try it.'));
    return;
  }
  const list = el('ul.site-list', matches.map((m) => el('li', icon('check'), el('span', bdi(m), ' is supported'))));
  const more = Math.max(r.count || 0, (r.matches || []).length) - matches.length;
  ui.siteResults.replaceChildren(list, more > 0 ? el('p.help', `+${num(more)} more`) : '');
}

/* ---------------------------------------------------------------- system details */

function systemFacts() {
  const hw = STATE.hw || {}, cap = STATE.cap, a = STATE.about || {}, packs = STATE.packs;
  const gpu = (hw.gpus || []).slice().sort((x, y) => (y.vram_mb || 0) - (x.vram_mb || 0))[0];
  // The engine proven for the chosen model; otherwise the one proven for any model.
  const model = CONFIG.whisper_model || hw.recommended_model;
  const cached = hw.cached || {};
  const other = cached[model] ? '' : Object.keys(cached).find((k) => cached[k]?.device) || '';
  const tested = cached[model] || cached[other];
  const withModel = other ? ` (tested with ${modelCatalog(other)?.label || other})` : ' (tested)';
  const vendors = [];
  for (const id of cap?.hardware || []) {
    const v = /nvenc$/.test(id) ? 'NVIDIA' : /qsv$/.test(id) ? 'Intel' : /amf$/.test(id) ? 'AMD' : '';
    if (v && !vendors.includes(v)) vendors.push(v);
  }
  const ffmpegOk = packs ? packs.ffmpeg_installed !== false : hw.ffmpeg !== false;
  return [
    ['Graphics card', gpu ? `${gpu.name} · ${Math.round((gpu.vram_mb || 0) / 1024)} GB` : STATE.hw ? 'No NVIDIA card found' : ''],
    ['Processor', hw.cpu_threads ? `${num(hw.cpu_threads)} threads` : ''],
    ['Transcription engine', tested
      ? `${tested.device === 'cuda' ? 'GPU' : 'Processor'} · ${tested.compute_type}${withModel}`
      : 'Not tested yet. Checked on the first transcription.'],
    ['Hardware video encoding', cap ? (vendors.length ? vendors.join(', ') : 'None, uses the processor') : ''],
    ['YouTube helper (Node.js)', cap ? (cap.js_runtime ? 'Installed' : 'Not installed') : ''],
    ['Browsers with sign-ins', cap ? ((cap.browsers || []).map((b) => b.label).join(', ') || 'None found') : ''],
    ['ffmpeg', STATE.hw || packs ? (ffmpegOk ? 'Included' : 'Missing') : ''],
    ['yt-dlp', a.yt_dlp || hw.yt_dlp || ''],
    ['Python', a.python || hw.python || ''],
  ];
}
function renderSystem() {
  if (!ui.sysRows) return;
  const facts = systemFacts();
  if (ui.sysRows.childElementCount !== facts.length * 2) {
    ui.sysRows.replaceChildren(...facts.flatMap(([k]) => [el('dt', k), el('dd.tabular')]));
  }
  const dds = ui.sysRows.querySelectorAll('dd');
  facts.forEach(([, v], i) => { const t = v || '…'; if (dds[i].textContent !== t) dds[i].textContent = t; });
}
async function copySystem() {
  const a = STATE.about || {};
  const home = STATE.setup?.suggestions?.home || '';
  const lines = [`Media Toolkit ${a.version || ''}`.trim(), ...systemFacts().map(([k, v]) => `${k}: ${v || 'unknown'}`)];
  if (a.frozen !== undefined) lines.push(`Build: ${a.frozen ? (a.portable ? 'installed, portable' : 'installed') : 'from source'}`);
  if (a.data_dir) lines.push(`Data folder: ${a.data_dir}`);
  let text = lines.join('\n');
  // Never share the user's name: the home folder becomes %USERPROFILE%.
  if (home) text = text.split(home).join('%USERPROFILE%');
  const ok = await copyText(text);
  toast(ok ? 'System details copied.' : "Couldn't copy. Try again.", { tone: ok ? 'ok' : 'err' });
}
