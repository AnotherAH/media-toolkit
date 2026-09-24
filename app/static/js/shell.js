// shell.js: the frame around the tabs. Tabs and keyboard (G-05), no
// automatic tab switches (G-03), the top-bar status chip and badges (G-04),
// window title signals (G-14), the app-wide drop guard (T-07), liveness
// heartbeats, and the startup order (G-09).
import {
  $, $$, el, on, emit, _bindShell, callHook, hasHook, loadSetup, loadHardware, loadConfig,
  loadPacks, loadModels, loadCapabilities, refreshSettingsMeta, STATE, SETTINGS_META, TOKEN, plural, num,
} from './core.js';
import { dismissNewestToast, icon, spinner, button, iconButton } from './ui.js';
import {
  connect, counts, allJobs, markFailuresSeen, clearUnseenTranscripts, revealSetting, ACTIVE,
} from './jobs.js';

export const TABS = ['download', 'transcript', 'live', 'queue', 'settings'];
const TAB_NAMES = { download: 'Download', transcript: 'Transcript', live: 'Live', queue: 'Queue', settings: 'Settings' };
const modules = {};
let active = 'download';

/* ================================================================ tabs */

const section = (id) => $(`#tab-${id}`);
const navButton = (id) => $(`.tabs > button[data-tab="${id}"]`);

/** Focus the tab's primary input (G-05): its focus() export, else [data-autofocus]. */
function focusTab(id) {
  const mod = modules[id];
  if (mod?.focus) { mod.focus(); return; }
  const target = section(id)?.querySelector('[data-autofocus]');
  target?.focus({ preventScroll: true });
}

/**
 * Switch tab. Every call is an explicit user action (G-03): background events
 * never call this. opts: {focus = true, scroll = true}.
 */
export function goTab(id, opts = {}) {
  if (!TABS.includes(id)) return;
  const { focus = true, scroll = true } = opts;
  const prev = active;
  if (id !== prev) {
    for (const t of TABS) {
      const sec = section(t);
      if (sec) sec.hidden = t !== id;
      const b = navButton(t);
      if (t === id) b?.setAttribute('aria-current', 'page');
      else b?.removeAttribute('aria-current');
    }
    active = id;
    modules[prev]?.onHide?.();
  }
  if (scroll) window.scrollTo(0, 0);
  if (id === 'queue') markFailuresSeen();
  if (id === 'transcript' && !modules.transcript?.onShow) clearUnseenTranscripts();
  modules[id]?.onShow?.({ prev });
  emit('tab', { id, prev });
  if (focus) focusTab(id);
  renderChrome();
}
export const currentTab = () => active;

const dots = {};
function setTabDot(tab, onOff, label = '') {
  const b = navButton(tab);
  const dot = b?.querySelector('.tab-dot');
  if (!dot) return;
  dots[tab] = onOff ? (label || '1 new') : '';
  dot.hidden = !onOff;
  labelTab(tab);
}
const badgeLabels = {};
function labelTab(tab) {
  const b = navButton(tab);
  if (!b) return;
  const extra = [badgeLabels[tab], dots[tab]].filter(Boolean).join(', ');
  if (extra) b.setAttribute('aria-label', `${TAB_NAMES[tab]}, ${extra}`);
  else b.removeAttribute('aria-label');
}
function setBadge(tab, n, label) {
  const badge = navButton(tab)?.querySelector('.badge');
  if (!badge) return;
  badge.hidden = !n;
  const t = String(n || '');
  if (badge.textContent !== t) badge.textContent = t;
  badgeLabels[tab] = n ? label : '';
  labelTab(tab);
}

/* ================================================================ status chip (G-04) */

let chipAction = null;
function renderChip() {
  const chip = $('#statusChip');
  const c = counts();
  const packs = STATE.packs;
  const pr = packs?.progress || {};
  const ffmpegMissing = (packs && packs.ffmpeg_installed === false) || (STATE.hw && STATE.hw.ffmpeg === false);
  const badFolder = Object.keys(folderProblems()).length > 0;
  let cls = '', dot = '', text = '', label = '';
  if (pr.busy) {
    text = `${pr.task === 'ffmpeg' ? 'Installing ffmpeg' : 'Installing GPU support'} · ${Math.round(pr.percent || 0)}%`;
    chipAction = () => revealSetting(pr.task === 'ffmpeg' ? 'health' : 'transcription');
  } else if (ffmpegMissing || badFolder) {
    cls = 'warn'; text = 'Repair needed';
    label = `Repair needed. ${ffmpegMissing ? 'ffmpeg is missing' : "A folder can't be used"}. Open Settings.`;
    chipAction = () => revealSetting(ffmpegMissing ? 'health' : 'folders');
  } else if (c.unseenFailed) {
    cls = 'err'; text = `${num(c.unseenFailed)} failed`;
    label = `${plural(c.unseenFailed, 'job')} failed. Open the Queue.`;
    chipAction = () => goTab('queue');
  } else if (c.active) {
    const onlyRec = c.activeWork === 0 && c.recording > 0 && c.active === c.recording;
    if (onlyRec) { cls = 'rec'; dot = 'rec'; text = 'Recording'; label = 'Recording. Open the Queue.'; }
    else { dot = 'run'; text = `${num(c.active)} running`; label = `${num(c.active)} running. Open the Queue.`; }
    chipAction = () => goTab('queue');
  } else chipAction = null;

  if (!text) { chip.hidden = true; return; }
  chip.hidden = false;
  const sig = `${cls}|${dot}|${text}`;
  if (chip._sig !== sig) {
    chip._sig = sig;
    chip.className = `status-chip${cls ? ` ${cls}` : ''}`;
    chip.replaceChildren();
    if (dot) chip.append(Object.assign(document.createElement('span'), { className: 'dot' }));
    else if (cls === 'warn') chip.append(icon('alert'));
    else if (cls === 'err') chip.append(icon('x'));
    else chip.append(spinner());
    chip.append(text);
  }
  if (label) chip.setAttribute('aria-label', label);
  else chip.removeAttribute('aria-label');
}

/* ================================================================ app banner */
// Only when something is broken app-wide: a folder that can't be used (UI-4)
// or a settings file that was damaged and replaced. Settings shows the detail.

const FOLDER_NAMES = { download_dir: 'Downloads folder', transcript_dir: 'Transcripts folder' };
let noticeDismissed = false;
const folderProblems = () => SETTINGS_META.folder_problems || STATE.setup?.folder_problems || {};
function renderBanner() {
  const box = $('#appBanner');
  const probs = Object.entries(folderProblems());
  const notice = !noticeDismissed ? (SETTINGS_META.notice || STATE.setup?.notice || '') : '';
  if (!probs.length && !notice) { box.hidden = true; box.replaceChildren(); return; }
  const rows = probs.map(([key, msg]) => el('div.callout-row',
    el('div.callout-text', `${FOLDER_NAMES[key] || key}: ${msg}`),
    button('Choose folder', 'secondary', { size: 'sm', onClick: () => revealSetting('folders') })));
  if (notice) {
    rows.push(el('div.callout-row', el('div.callout-text', notice),
      iconButton('x', 'Dismiss', () => { noticeDismissed = true; renderBanner(); })));
  }
  box.replaceChildren(el('div.callout.warn', { role: 'status' }, icon('alert'), el('div.callout-body', el('div.callout-rows', rows))));
  box.hidden = false;
}

/* ================================================================ title (G-14) */

const unseenBlur = { done: 0, failed: 0 };
function renderTitle() {
  const jobs = allJobs().filter((j) => ACTIVE.includes(j.status));
  let title = 'Media Toolkit';
  const work = jobs.filter((j) => j.kind !== 'live');
  if (work.length) {
    const running = work.filter((j) => j.status === 'running' && !j.indeterminate);
    const pct = running.length ? Math.round(100 * running.reduce((a, j) => a + (Number(j.progress) || 0), 0) / running.length) : null;
    const kinds = new Set(work.map((j) => j.kind));
    const verb = kinds.size > 1 ? 'running' : kinds.has('transcript') ? 'transcribing' : 'downloading';
    title = `${pct !== null ? `${pct}% · ` : ''}${num(work.length)} ${verb} · Media Toolkit`;
  } else if (jobs.some((j) => j.kind === 'live' && j.live_phase === 'recording')) {
    title = '● Recording · Media Toolkit';
  } else if (unseenBlur.failed) title = `⚠ ${num(unseenBlur.failed)} failed · Media Toolkit`;
  else if (unseenBlur.done) title = '✓ Done · Media Toolkit';
  if (document.title !== title) document.title = title;
}

function renderChrome() {
  const c = counts();
  setBadge('queue', c.active, `${num(c.active)} active`);
  setBadge('live', c.recording, `${plural(c.recording, 'recording')} in progress`);
  renderChip();
  renderTitle();
}

/* ================================================================ keyboard (G-05) */

const modalOpen = () => !!document.querySelector('dialog[open]:modal');
function onKey(e) {
  if (e.ctrlKey && !e.shiftKey && !e.altKey && !e.metaKey && /^[1-5]$/.test(e.key)) {
    if (modalOpen()) return;
    e.preventDefault();
    goTab(TABS[Number(e.key) - 1]);
    return;
  }
  if (e.key === 'Escape') {
    // Dialogs and menus close themselves natively.
    if (document.querySelector('dialog[open]') || document.querySelector(':popover-open')) return;
    // Esc never clears a field (a search box would otherwise empty itself).
    if (e.target instanceof HTMLInputElement && e.target.type === 'search') e.preventDefault();
    dismissNewestToast();
  }
}

/* ================================================================ drag and drop (T-07) */

function setupDrop() {
  const overlay = $('#dropOverlay');
  const count = $('#dropCount');
  let depth = 0;
  const hasFiles = (e) => Array.from(e.dataTransfer?.types || []).includes('Files');
  const hide = () => { depth = 0; overlay.hidden = true; };
  document.addEventListener('dragenter', (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth++;
    if (modalOpen()) return;
    const n = Array.from(e.dataTransfer.items || []).filter((i) => i.kind === 'file').length;
    count.textContent = n ? plural(n, 'file') : '';
    overlay.hidden = false;
  });
  document.addEventListener('dragover', (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();                       // a drop must never navigate the window away
    e.dataTransfer.dropEffect = modalOpen() ? 'none' : 'copy';
  });
  document.addEventListener('dragleave', (e) => {
    if (!hasFiles(e)) return;
    depth = Math.max(0, depth - 1);
    if (!depth || !e.relatedTarget) hide();
  });
  document.addEventListener('drop', (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    hide();
    if (modalOpen()) return;
    const files = Array.from(e.dataTransfer.files || []);
    if (!files.length) return;
    if (hasHook('drop')) callHook('drop', files);
    else goTab('transcript');
  });
  addEventListener('dragend', hide);
  addEventListener('blur', () => { if (!overlay.hidden) hide(); });
}

/* ================================================================ liveness */

function heartbeat() {
  const beat = () => fetch('/api/heartbeat', { method: 'POST', headers: { 'X-MT-Token': TOKEN }, keepalive: true }).catch(() => {});
  beat();
  setInterval(beat, 3000);
  addEventListener('pagehide', () => { try { navigator.sendBeacon('/api/goodbye'); } catch (_) {} });
}

/* ================================================================ startup (G-09) */

/**
 * start({tabs: {download, transcript, live, queue, settings}, wizard})
 * Each tab module exports mount(section); optional focus(), onShow({prev}), onHide().
 * wizard exports open(setup) -> Promise<{tab}> (resolves when it closes).
 */
export async function start({ tabs, wizard }) {
  _bindShell({ goTab, currentTab, setTabDot });
  heartbeat();
  document.addEventListener('keydown', onKey);
  setupDrop();

  for (const b of $$('.tabs > button')) b.addEventListener('click', () => goTab(b.dataset.tab));
  $('#statusChip').addEventListener('click', () => chipAction?.());

  for (const id of TABS) {
    modules[id] = tabs[id];
    try { tabs[id]?.mount?.(section(id)); } catch (e) {
      console.error(`${id} tab failed to start`, e);
      section(id).append(Object.assign(document.createElement('p'), { className: 'help', textContent: `This tab couldn't start: ${e.message}` }));
    }
  }

  on('jobs', renderChrome);
  on('unseen', renderChrome);
  on('packs', renderChip);
  on('hardware', renderChip);
  on('setup', () => { renderBanner(); renderChip(); });
  on('settings-meta', () => { renderBanner(); renderChip(); });
  on('config', ({ source, keys }) => {
    if (source === 'load') { renderBanner(); renderChip(); return; }
    // A folder was changed: the server re-checked every folder.
    if (keys?.some((k) => k === 'download_dir' || k === 'transcript_dir')) refreshSettingsMeta().catch(() => {});
  });
  on('job-transition', ({ job, to }) => {
    if (job.from_history || document.hasFocus()) return;
    if (to === 'done') unseenBlur.done++;
    if (to === 'error') unseenBlur.failed++;
  });
  addEventListener('focus', () => { unseenBlur.done = 0; unseenBlur.failed = 0; renderTitle(); });
  renderChrome();

  // 1. First-run setup comes first and opens at once.
  let wizardClosed = null;
  try {
    const setup = await loadSetup();
    if (setup?.needed && wizard?.open) wizardClosed = wizard.open(setup);
  } catch (e) {
    console.error('setup check failed', e);
  }
  // 2. Everything else in parallel; a failure only affects its own section.
  Promise.allSettled([loadHardware(), loadConfig(), loadPacks(), loadModels(), loadCapabilities()]).then((results) => {
    const failed = results.filter((r) => r.status === 'rejected');
    if (failed.length) console.warn('some data failed to load', failed.map((r) => r.reason?.message));
  });
  // 3. Live job updates.
  connect();
  // 4. Focus the link box once the wizard is closed or was never needed.
  if (wizardClosed) {
    const res = await wizardClosed.catch(() => null);
    goTab(res?.tab || active, { scroll: true });
  } else focusTab(active);
  window.__mtStarted = true;
}
