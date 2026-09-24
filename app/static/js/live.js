// live.js: the Live tab (L-01..L-09).
//   link card        : stream link, the live check (L-02) and the one action it calls for
//   Recordings card  : every recording of this session as a keyed JobView card (L-03, L-04)
//   Recording options: what to record, quality, save as, limits, waiting (L-07), remembered (L-09)
import {
  el, bdi, setText, get, post, on, registerHook, navigate, CONFIG, bindSetting, num, duration,
  clock, span, shortDate, truncate, hostPath, parseLinks,
} from './core.js';
import {
  icon, spinner, button, pageHead, card, segmented, linkBox, toastError,
} from './ui.js';
import { JobView, allJobs, ACTIVE, FINAL, sortQueue, runAction } from './jobs.js';

const QUALITIES = [['best', 'Best available'], ['2160', '4K'], ['1440', '1440p'], ['1080', '1080p'], ['720', '720p'], ['480', '480p']];
const CONTAINERS = {
  video: [['mp4', 'MP4 (plays everywhere)'], ['mkv', 'MKV'], ['ts', 'TS (safest for very long recordings)']],
  audio: [['m4a', 'M4A (plays everywhere)'], ['ts', 'TS (safest for very long recordings)']],
};
const HINT = 'Stop whenever you like. Everything recorded so far is kept.';
// yt-dlp appends ' YYYY-MM-DD HH:MM' to live titles; the card does not need it (L-02).
const LIVE_STAMP = /\s+\d{4}-\d{2}-\d{2} \d{2}:\d{2}\s*$/;

let input, go, box, previewEl, recCard;
const O = {};
const C = { seq: 0, url: '', key: '', state: 'idle', data: null, error: null, timer: null };
let submitting = false;
let waitAuto = false;          // the wait box was ticked by an offline check, not by the user

const cleanTitle = (t) => { const s = String(t || '').trim(); return s.replace(LIVE_STAMP, '').trim() || s; };
const mode = () => (O.what?.querySelector('input:checked')?.value === 'audio' ? 'audio' : 'video');

/* ================================================================ mount */

export function mount(section) {
  input = el('textarea#lvUrl.linkbox', {
    rows: '1', placeholder: 'Paste a live stream or channel link, e.g. youtube.com/@channel/live or twitch.tv/name',
    'aria-label': 'Stream link', spellcheck: 'false', autocomplete: 'off', 'data-autofocus': '',
  });
  go = button('Record', 'primary', { id: 'lvGo', title: 'Record (Enter)' });
  const hint = el('div.link-hint#lvHint');
  previewEl = el('div#lvPreview.lv-preview', { hidden: true });

  const options = buildOptions();
  box = linkBox(input, go, hint, {
    hintText: HINT,
    label: buttonLabel,
    canSubmit,
    onChange: onLinkChange,
    onSubmit: submit,
  });

  const recList = el('div#lvActive');
  recCard = card({ title: 'Recordings', id: 'lvRecordings', body: [recList] });
  recCard.hidden = true;

  section.append(
    pageHead('Record a live stream', 'Record any live stream from YouTube, Twitch, Kick, TikTok, radio and more. Saved exactly as streamed: no quality loss, hardly any CPU.'),
    card({ id: 'lvLinkCard', body: [el('div.link-row', input, go), hint, previewEl] }),
    recCard,
    options,
  );

  // L-03: waiting, recording and saving jobs, plus the 3 most recent finished ones.
  let seen = null;
  let allowed = new Set();
  const pick = () => {
    const list = allJobs();
    if (list === seen) return allowed;
    seen = list;
    const mine = list.filter((j) => j.kind === 'live' && !j.from_history);
    const done = mine.filter((j) => FINAL.includes(j.status)).sort((a, b) => (b.updated || 0) - (a.updated || 0)).slice(0, 3);
    allowed = new Set([...mine.filter((j) => ACTIVE.includes(j.status)), ...done].map((j) => j.id));
    return allowed;
  };
  new JobView(recList, {
    variant: 'full',
    filter: (j) => j.kind === 'live' && pick().has(j.id),
    sort: sortQueue,
    onUpdate: (items) => { recCard.hidden = !items.length; },
  });

  registerHook('live.setLink', (url, { run = true } = {}) => {
    input.value = url;
    box.refresh();
    input.focus();
    const links = parseLinks(url).links;
    if (run && links.length === 1) { C.key = ''; scheduleCheck(links[0], 0); }
  });
  on('config', () => queueMicrotask(syncMode));
}

export function focus() { input?.focus({ preventScroll: true }); }

/* ================================================================ link box + live check (L-02) */

function current(parsed) {
  return parsed.links.length === 1 && C.url === parsed.links[0] ? C : null;
}
function buttonLabel(parsed) {
  if (submitting) return 'Starting…';
  const c = current(parsed);
  if (c?.state === 'ok') {
    if (c.data.is_live) return 'Record now';
    if (c.data.offline) return O.wait.checked ? 'Wait and record' : 'Record';
  }
  return 'Record';
}
function canSubmit(parsed) {
  if (submitting) return 'Starting…';
  const c = current(parsed);
  if (c?.state === 'ok' && c.data.regular) return 'This is a regular video, not a live stream.';
  return true;
}

function onLinkChange(parsed) {
  // G-05: the tooltip names the action the button will take ('Wait and record (Enter)').
  if (!go.disabled && !go.classList.contains('is-busy')) go.title = `${go.textContent.trim()} (Enter)`;
  if (parsed.links.length !== 1) { resetCheck(false); renderPreview(parsed); return; }
  scheduleCheck(parsed.links[0]);
}
const checkKey = (url) => `${url}|${mode() === 'audio' ? 'audio' : O.quality.value}`;
/** Check the link (debounced). Changing only the quality keeps the old answer on screen until the new one. */
function scheduleCheck(url, delay = 550) {
  const key = checkKey(url);
  if (key === C.key && C.state !== 'idle') return;
  const sameLink = url === C.url && C.state === 'ok';
  clearTimeout(C.timer);
  C.seq++;
  C.key = key;
  C.url = url;
  if (!sameLink) {
    Object.assign(C, { state: 'checking', data: null, error: null });
    restoreWait();
    renderPreview();
  }
  C.timer = setTimeout(() => runCheck(url, key), delay);
}
async function runCheck(url, key) {
  const seq = ++C.seq;
  const q = mode() === 'audio' ? 'best' : O.quality.value;
  const audio = mode() === 'audio';
  try {
    const d = await get(`/api/live-check?url=${encodeURIComponent(url)}&quality=${encodeURIComponent(q)}&audio_only=${audio}`);
    if (seq !== C.seq || key !== C.key) return;
    Object.assign(C, { state: 'ok', data: d, error: null });
    applyWait(d);
  } catch (e) {
    if (seq !== C.seq || key !== C.key) return;
    Object.assign(C, { state: 'error', data: null, error: e });
    restoreWait();
  }
  renderPreview();
  box.refresh();
}
function resetCheck(render = true) {
  clearTimeout(C.timer);
  C.seq++;
  Object.assign(C, { url: '', key: '', state: 'idle', data: null, error: null });
  restoreWait();
  if (render) renderPreview();
}
/** An offline channel or a scheduled stream ticks 'wait for it' for this link only (never saved). */
function applyWait(d) {
  if (d.offline && !O.wait.checked) {
    O.wait.checked = true;
    waitAuto = true;
    syncWait();
  } else if (!d.offline) restoreWait();
}
function restoreWait() {
  if (!waitAuto) return;
  waitAuto = false;
  O.wait.checked = !!CONFIG.lv_wait;
  syncWait();
}

function renderPreview(parsed = box ? box.parse() : parseLinks(input.value)) {
  const show = (...nodes) => { previewEl.replaceChildren(...nodes); previewEl.hidden = false; };
  if (parsed.links.length > 1) {
    show(el('div.lv-line', icon('info'), el('span', `${num(parsed.links.length)} links. Each becomes its own recording.`)));
    return;
  }
  if (!parsed.links.length || C.state === 'idle') { previewEl.hidden = true; previewEl.replaceChildren(); return; }
  if (C.state === 'checking') { show(el('div.skeleton-row', spinner(), 'Checking the stream…')); return; }
  if (C.state === 'error') {
    const e = C.error || {};
    show(el('div.lv-line.is-warn', icon('alert'), el('div.lv-line-text',
      el('div.lv-line-title', e.title || "Couldn't read this link"),
      el('div', e.body || (e.title ? '' : e.text || 'Check that the link opens in your browser, then try again.')))));
    return;
  }
  const d = C.data || {};
  // Without a title (an offline channel), the link itself in --fg-2, like a card before its details arrive.
  const known = cleanTitle(d.title);
  const title = known || hostPath(C.url);
  const who = d.uploader || '';
  let status;
  if (d.is_live) {
    status = el('div.lv-status.is-live', icon('rec-dot'), el('span',
      ['Live now', d.height ? `${d.height}p` : null].filter(Boolean).join(' · '), who ? [' · ', bdi(who)] : null));
  } else if (d.offline) {
    const lines = [who ? bdi(who) : 'This channel', " isn't live right now."];
    const when = scheduled(d.release_timestamp);
    if (when) lines.push(` ${when}`);
    status = el('div.lv-status', icon('clock'), el('span', lines));
  } else if (d.regular) {
    const instead = button('Download it instead', 'secondary', {
      size: 'sm',
      onClick: () => {
        const url = C.url;
        box.clear();
        runAction('download_instead', { url, kind: 'live' });
      },
    });
    status = el('div.lv-status', icon('info'), el('span', 'This is a regular video, not a live stream.'), instead);
  } else {
    status = el('div.lv-status', icon('info'), el('span', 'This link can be recorded.'));
  }
  const chips = [];
  if (d.is_live && d.has_audio && !d.has_video && mode() !== 'audio') chips.push(el('span.chip', 'Sound only'));
  if (d.is_live && d.has_video && !d.has_audio) chips.push(el('span.chip.warn', 'No sound in this stream'));
  const thumb = el('div.thumb', icon('record'));
  if (d.thumbnail && /^https?:\/\//i.test(d.thumbnail)) {
    const img = el('img', { alt: '', loading: 'lazy', decoding: 'async', referrerpolicy: 'no-referrer', src: d.thumbnail });
    img.addEventListener('error', () => img.remove());
    thumb.append(img);
  }
  show(el('div.preview.lv-pv', thumb,
    el('div.preview-body',
      el(known ? 'div.preview-title' : 'div.preview-title.is-url', { dir: 'auto', title }, title),
      status,
      chips.length ? el('div.chips', chips) : null)));
}
/** 'Scheduled to start at 21:30 (in 2 h 10 min).' */
function scheduled(ts) {
  const t = Number(ts);
  if (!t) return '';
  const now = Date.now() / 1000;
  const today = new Date(t * 1000).toDateString() === new Date().toDateString();
  const at = today ? clock(t) : `${shortDate(t)}, ${clock(t)}`;
  return t > now ? `Scheduled to start at ${at} (in ${span(t - now)}).` : `Scheduled to start at ${at}.`;
}

/* ================================================================ record (L-03) */

function liveOptions() {
  const audio = mode() === 'audio';
  const hours = Number(O.hours.value);
  return {
    quality: O.quality.value || 'best',
    audio_only: audio,
    container: O.container.value || (audio ? 'm4a' : 'mp4'),
    max_minutes: O.max.value.trim(),
    split_minutes: O.split.value.trim(),
    wait_for_live: O.wait.checked,
    wait_minutes: Math.round((Number.isFinite(hours) && hours > 0 ? hours : 3) * 60),
  };
}

async function submit(parsed) {
  const c = current(parsed);
  const d = c?.state === 'ok' ? c.data : null;
  const hints = {};
  // An offline channel has no stream title yet: its name keeps the card readable once it ends.
  if (d) hints[c.url] = { title: cleanTitle(d.title) || d.uploader || '', thumbnail: d.thumbnail || '', uploader: d.uploader || '' };
  const options = liveOptions();
  submitting = true;
  box.refresh();
  try {
    const r = await post('/api/jobs', { url: parsed.links.join('\n'), kind: 'live', options, hints });
    const jobs = r.jobs || [];
    submitting = false;
    box.clear();
    resetCheck();
    if (jobs.length > 1) box.showOk(`Started ${num(jobs.length)} recordings. Stop them here or in the Queue.`);
    else if (d && d.offline && options.wait_for_live) {
      box.showOk(['Waiting for ', d.uploader ? bdi(d.uploader) : 'the stream', ' to go live. You can close this tab; keep the app open.']);
    } else {
      const title = (d && cleanTitle(d.title)) || jobs[0]?.title || parsed.links[0];
      box.showOk(['Recording “', bdi(truncate(title === parsed.links[0] ? hostPath(title) : title, 60)), '”. Stop it here or in the Queue.']);
    }
  } catch (e) {
    submitting = false;
    if (e.code === 'bad_link' || e.field || e.status === 400) box.setHint(e.text, 'error');
    else toastError(e);
  } finally {
    submitting = false;
    box.refresh();
  }
}

/* ================================================================ Recording options (L-07, L-09) */

function buildOptions() {
  O.what = segmented({
    name: 'lvWhat', legend: 'What to record', id: 'lvWhat', value: 'video',
    options: [{ value: 'video', label: 'Video and sound' }, { value: 'audio', label: 'Sound only' }],
    onChange: () => syncMode(true),
  });
  O.quality = el('select#lvQuality.select-short', QUALITIES.map(([v, l]) => el('option', { value: v }, l)));
  O.container = el('select#lvContainer.select-short');
  fillContainers('video');
  const num1 = (id) => el(`input#${id}.input-num`, { type: 'number', min: '1', step: 'any', inputmode: 'decimal', placeholder: 'never' });
  O.max = num1('lvMax');
  O.split = num1('lvSplit');
  O.wait = el('input#lvWait', { type: 'checkbox' });
  O.hours = el('input#lvHours.input-tiny', { type: 'number', min: '0.5', max: '168', step: 'any', inputmode: 'decimal', 'aria-label': 'Give up after, in hours' });
  O.qualityField = el('label.field.lv-field', el('span.label', 'Quality'), O.quality);
  O.hoursField = el('label.inline-field.lv-hours', { hidden: true }, el('span.label', 'Give up after'), O.hours, el('span', 'hours'));

  const body = [
    el('div.lv-what', el('span.label', { 'aria-hidden': 'true' }, 'What to record'), O.what),
    el('div.lv-pair', O.qualityField, el('label.field.lv-field', el('span.label', 'Save as'), O.container)),
    el('div.lv-limits',
      el('label.inline-field', el('span.label', 'Stop automatically after'), O.max, el('span', 'minutes')),
      el('label.inline-field', el('span.label', 'Start a new file every'), O.split, el('span', 'minutes'))),
    el('div.lv-wait', el('label.check', O.wait, el('span.check-text', "If it hasn't started yet, wait for it")), O.hoursField),
  ];
  const optCard = card({ title: 'Recording options', id: 'lvOptions', saveState: true, body: [el('div.lv-opts', body)] });

  const minutes = (v) => (v === '' || (Number(v) > 0 && Number(v) <= 44640) ? '' : 'Enter a number of minutes, or leave it empty.');
  bindSetting(O.what, 'lv_audio', { toServer: (v) => v === 'audio', fromServer: (v) => (v ? 'audio' : 'video') });
  bindSetting(O.quality, 'lv_quality');
  bindSetting(O.container, 'lv_container', { fromServer: (v) => containerFor(v, mode()) });
  bindSetting(O.max, 'lv_max', { validate: minutes });
  bindSetting(O.split, 'lv_split', { validate: minutes });
  bindSetting(O.wait, 'lv_wait');
  bindSetting(O.hours, 'lv_wait_hours', {
    toServer: (v) => Number(v),
    validate: (v) => (Number(v) > 0 && Number(v) <= 168 ? '' : 'Enter a number of hours, up to 168.'),
  });

  O.wait.addEventListener('change', () => { waitAuto = false; syncWait(); box?.refresh(); });
  O.quality.addEventListener('change', () => recheck());
  syncMode();
  return optCard;
}

/** The container value that fits the mode: sound only offers M4A or TS, video MP4, MKV or TS. */
function containerFor(v, m) {
  v = String(v || '').toLowerCase();
  if (m === 'audio') return v === 'ts' ? 'ts' : 'm4a';
  return ['mp4', 'mkv', 'ts'].includes(v) ? v : 'mp4';
}
function fillContainers(m) {
  if (O.container._mode === m) return;
  O.container._mode = m;
  O.container.replaceChildren(...CONTAINERS[m].map(([v, l]) => el('option', { value: v }, l)));
}
/** After a mode change (by the user or a settings load): quality only for video, the right containers. */
function syncMode(fromUser = false) {
  const m = mode();
  O.qualityField.hidden = m === 'audio';
  const keep = fromUser ? O.container.value : CONFIG.lv_container;
  fillContainers(m);
  O.container.value = containerFor(fromUser && m === 'video' ? CONFIG.lv_container : keep, m);
  syncWait();
  if (fromUser) recheck();
}
function syncWait() {
  O.hoursField.hidden = !O.wait.checked;
}
/** Quality or sound-only changed: ask again so the preview never promises the wrong height. */
function recheck() {
  const parsed = box?.parse();
  if (parsed && parsed.links.length === 1) scheduleCheck(parsed.links[0], 300);
}
