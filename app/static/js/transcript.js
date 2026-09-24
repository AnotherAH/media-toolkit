// transcript.js: the Transcript tab (T-01..T-13).
//   link card  : link box, file picker, preview line (T-08), Options drawer (T-02, T-03)
//   #trWork    : upload progress (T-07, T-13), the tracked job with its step list (T-01),
//                the reader (T-04, T-06, T-11, T-12), or Recent transcripts (T-10)
// It also registers the app-wide drop hook and the transcript.* hooks the shell and
// the job actions call.
import {
  el, $, bdi, setText, get, post, api, upload, on, registerHook, navigate, currentTab,
  CONFIG, isConfigLoaded, saveSettings, bindSetting, STATE, num, bytes, bytesOf, duration, eta, plural,
  truncate, siteName, isYouTube, languageName, nativeLanguageName, relTime, copyText, revealPath,
  openUrl, installPack, scrollToEl, notify, parseLinks,
} from './core.js';
import {
  icon, spinner, button, linkButton, iconButton, callout, disclosure, pageHead, card, flash,
  withBusy, announce, toast, toastError, openDialog, confirmDialog, menu, segmented, tabSwitch, linkBox,
} from './ui.js';
import {
  JobView, getJob, allJobs, isActive, ACTIVE, statusOf, renderCard, patchCard, patchSteps,
  transcriptFile, wordCount, displayTitle, unseenTranscripts, clearUnseenTranscripts,
} from './jobs.js';

/* ================================================================ constants */

// T-02: the languages offered under 'The video is in…'.
const LANGS = ['en', 'fa', 'ar', 'es', 'fr', 'de', 'tr', 'ru', 'pt', 'it', 'hi', 'ur', 'ja', 'ko', 'zh',
  'id', 'nl', 'pl', 'uk', 'vi', 'he'];
const NATIVE_FALLBACK = {
  en: 'English', fa: 'فارسی', ar: 'العربية', es: 'Español', fr: 'Français', de: 'Deutsch', tr: 'Türkçe',
  ru: 'Русский', pt: 'Português', it: 'Italiano', hi: 'हिन्दी', ur: 'اردو', ja: '日本語', ko: '한국어',
  zh: '中文', id: 'Bahasa Indonesia', nl: 'Nederlands', pl: 'Polski', uk: 'Українська', vi: 'Tiếng Việt', he: 'עברית',
};
const OLD_CODES = { iw: 'he', in: 'id', ji: 'yi', jw: 'jv', mo: 'ro' };
const MODEL_NAMES = {
  'large-v3-turbo': 'Large v3 Turbo', 'large-v3': 'Large v3', 'distil-large-v3': 'Distil Large v3',
  medium: 'Medium', small: 'Small', base: 'Base', tiny: 'Tiny',
};
const SAVE_FORMATS = [
  { fmt: 'txt', label: 'Text (.txt)' },
  { fmt: 'md', label: 'Notes with timestamps (.md)' },
  { fmt: 'srt', label: 'Subtitles (.srt)' },
  { fmt: 'vtt', label: 'Web subtitles (.vtt)' },
  { fmt: 'json', label: 'Data (.json)' },
];
const LONG_AT = 12000;       // T-06: longer than this gets the warning and parts (by characters, FIN-7)
const PART_SIZE = 11500;     // room for the preamble, header and end marker inside one message
const BLOCK_SECONDS = 30;    // T-04: timestamp blocks
const STAGE_WAITING = 'Waiting for another transcription to finish';
const TEXT = {
  hint: 'Enter to start · Shift+Enter for another line',
  empty: 'Your transcript appears here. Paste a link above, or drop a file anywhere in this window.',
  playlist: 'This is a playlist. Transcripts work one video at a time: open a video from it and paste that link.',
  live: 'This is a live stream. Transcripts need a finished video.',
  upcoming: "This video hasn't started yet. Transcripts need a finished video.",
  soon: 'Ready in about a second.',
  onPC: 'The speech will be transcribed on this PC.',
};

/* ================================================================ state */

let input, go, box, hintEl, fileInput, previewEl, optionsEl, workEl, uploadEl, trackEl, moreEl, readerEl, recentEl;
let trackView = null;
const O = {};                 // option controls
const UD = {};                // upload card parts
const RD = {};                // reader parts
const S = { tracked: null, submitted: null, submitting: false, more: 0, pendingOpen: null, lastStatus: '' };
const P = { seq: 0, url: '', state: 'idle', data: null, error: null, timer: null };
const U = { active: false, queue: [], index: 0, total: 0, name: '', frac: null, ctrl: null };
let R = { open: false, key: '', seq: 0, view: 'text', part: 0, parts: null, editing: false, dirty: false, cache: {} };
let lastView = 'text';
const recent = { rows: null, seq: 0, timer: null };

/* ================================================================ small helpers */

/** 'pt-BR' -> 'pt', 'iw' -> 'he' (the backend's captions.primary). */
function primary(code) {
  const head = String(code || '').trim().toLowerCase().replace('_', '-').split('-')[0];
  return OLD_CODES[head] || head;
}
const langName = (code) => (code ? languageName(primary(code)) : '');

/** T-02 option label: native name, then the English name in brackets. */
function langLabel(code) {
  let nat = code === 'id' ? NATIVE_FALLBACK.id : nativeLanguageName(code);
  if (!nat || nat === code) nat = NATIVE_FALLBACK[code] || code;
  const chars = Array.from(nat);
  nat = chars[0].toLocaleUpperCase(code) + chars.slice(1).join('');
  const en = languageName(code);
  return en && en !== nat && code !== 'id' ? `${nat} (${en})` : nat;
}

/** Nodes joined with ' · ' (strings stay text; nodes as given). */
function joined(parts) {
  const out = [];
  for (const p of parts.filter((x) => x !== null && x !== undefined && x !== '')) {
    if (out.length) out.push(' · ');
    out.push(p);
  }
  return out;
}
function thumbFor(url, fallbackIcon, cls = 'div.thumb') {
  const t = el(cls, icon(fallbackIcon));
  if (url && /^https?:\/\//i.test(url)) {
    const img = el('img', { alt: '', loading: 'lazy', decoding: 'async', referrerpolicy: 'no-referrer', src: url });
    img.addEventListener('error', () => img.remove());
    t.append(img);
  }
  return t;
}

/* ---- text shaping (mirrors app/subs.py so copies match the saved files) ---- */

function isCJK(ch) {
  const o = ch.codePointAt(0);
  return (o >= 0x3000 && o <= 0x30FF) || (o >= 0x3400 && o <= 0x4DBF) || (o >= 0x4E00 && o <= 0x9FFF)
    || (o >= 0xF900 && o <= 0xFAFF) || (o >= 0xFF00 && o <= 0xFF60) || (o >= 0x31F0 && o <= 0x31FF);
}
function joinPair(a, b) {
  if (!a) return b;
  if (!b) return a;
  if (/(\.\.\.|…)$/.test(a) && /^(\.\.\.|…)/.test(b)) {
    a = a.replace(/[.…]+$/, '').trimEnd();
    b = b.replace(/^[.…]+/, '').trimStart();
    if (!a || !b) return a || b;
  }
  if (isCJK(a.slice(-1)) && isCJK(b[0])) return a + b;
  return `${a} ${b}`;
}
const joinTexts = (parts) => parts.reduce((acc, p) => joinPair(acc, String(p || '').trim()), '');

/** Paragraphs of a .txt render: split on blank lines, line breaks inside one become spaces (FIN-18). */
function paragraphs(txt) {
  return String(txt || '').split(/\n[ \t]*\n\s*/).map((p) => p.replace(/\s*\n\s*/g, ' ').trim()).filter(Boolean);
}
/** Segments grouped into about-30-second blocks (T-04). */
function blocksOf(segs) {
  const out = [];
  let cur = null;
  for (const s of segs || []) {
    if (!cur || (s.start - cur.start >= BLOCK_SECONDS && cur.parts.length)) {
      cur = { start: Number(s.start) || 0, parts: [] };
      out.push(cur);
    }
    cur.parts.push(s.text);
  }
  return out.map((b) => ({ start: b.start, text: joinTexts(b.parts) })).filter((b) => b.text);
}
const stamp = (sec) => duration(Math.floor(Number(sec) || 0));

// Split levels, coarsest first (subs.chunk): paragraphs, lines, sentences (incl. CJK,
// Arabic, Urdu and Devanagari full stops), words; then a hard cut (FIN-7, UI-15).
const LEVELS = [/(\n[ \t]*\n\s*)/, /(\n)/, /((?<=[.!?…])\s+|(?<=[。！？؟۔।])\s*)/, /(\s+)/];
function splitKeep(text, re) {
  const parts = text.split(re);
  const out = [];
  for (let i = 0; i < parts.length; i += 2) {
    const piece = (parts[i] ?? '') + (parts[i + 1] ?? '');
    if (piece) out.push(piece);
  }
  return out;
}
function hardSplit(text, size) {
  const out = [];
  while (text.length > size) {
    let cut = size;
    while (cut > 1 && (/\p{M}/u.test(text[cut]) || /[\uDC00-\uDFFF]/.test(text[cut]))) cut--;
    out.push(text.slice(0, cut));
    text = text.slice(cut);
  }
  if (text) out.push(text);
  return out;
}
function units(text, size, level = 0) {
  if (text.length <= size) return [text];
  if (level >= LEVELS.length) return hardSplit(text, size);
  const parts = splitKeep(text, LEVELS[level]);
  if (parts.length <= 1) return units(text, size, level + 1);
  return parts.flatMap((p) => (p.length > size ? units(p, size, level + 1) : [p]));
}
function chunk(text, size) {
  if (text.length <= size) return [text];
  const out = [];
  let buf = '';
  for (const u of units(text, size)) {
    if (buf && buf.length + u.length > size) {
      if (buf.trim()) out.push(buf.trim());
      buf = '';
    }
    buf += u;
  }
  if (buf.trim()) out.push(buf.trim());
  return out;
}
/**
 * 'rtl' when the first letter of the text belongs to a right-to-left script.
 * A timestamp row needs this explicitly: its text sits in children that carry
 * their own dir, which dir=auto on the row itself skips.
 */
const RTL_LETTER = /[\p{Script=Arabic}\p{Script=Hebrew}\p{Script=Syriac}\p{Script=Thaana}\p{Script=Nko}\p{Script=Adlam}]/u;
function textDir(text) {
  const first = /\p{L}/u.exec(String(text || ''));
  return first && RTL_LETTER.test(first[0]) ? 'rtl' : 'ltr';
}
/** Paragraph text with URLs isolated, so Latin links keep their shape inside RTL prose (T-04). */
function linkified(text) {
  const out = [];
  for (const [i, part] of String(text).split(/(https?:\/\/\S+)/).entries()) {
    if (!part) continue;
    out.push(i % 2 ? el('bdi', { dir: 'ltr' }, part) : part);
  }
  return out;
}

/* ================================================================ mount */

export function mount(section) {
  input = el('textarea#trUrl.linkbox', {
    rows: '1', placeholder: 'Paste a video link, or drop a file anywhere in this window',
    'aria-label': 'Link to transcribe', spellcheck: 'false', autocomplete: 'off', 'data-autofocus': '',
  });
  go = button('Transcribe', 'primary', { id: 'trGo', title: 'Transcribe (Enter)' });
  fileInput = el('input#trFile', { type: 'file', multiple: true, hidden: true, tabindex: '-1', 'aria-hidden': 'true' });
  fileInput.addEventListener('change', () => {
    const files = Array.from(fileInput.files || []);
    fileInput.value = '';
    if (files.length) uploadFiles(files);
  });
  const choose = button('Choose a file…', 'quiet', { id: 'trChoose', size: 'sm', onClick: () => fileInput.click() });
  hintEl = el('div.link-hint#trHint', el('span.hint-text'), choose);
  previewEl = el('div#trPreview.tr-preview', { hidden: true });
  optionsEl = buildOptions();

  box = linkBox(input, go, hintEl, {
    hintText: TEXT.hint,
    label: (p) => (S.submitting || trackedBusy() ? 'Transcribing…' : p.links.length > 1 ? `Transcribe ${num(p.links.length)}` : 'Transcribe'),
    canSubmit: canSubmit,
    onChange: onLinkChange,
    onSubmit: submit,
  });

  buildUpload();
  trackEl = el('div#trJob.tr-job-host');
  moreEl = el('p.tr-more', { hidden: true }, linkButton('', () => navigate('queue')));
  buildReader();
  recentEl = el('div.tr-recent', { hidden: true });
  workEl = el('div#trWork.tr-work', uploadEl, trackEl, moreEl, readerEl, recentEl);

  section.append(
    pageHead('Get a transcript', "Uses the video's own captions when it has them. Otherwise the speech is transcribed on this PC. Nothing is uploaded."),
    card({ id: 'trLinkCard', cls: 'tr-link-card', body: [el('div.link-row', input, go), hintEl, fileInput, previewEl, el('hr.divider'), optionsEl] }),
    workEl,
  );

  trackView = new JobView(trackEl, {
    custom: true,
    filter: (j) => j.id === S.tracked && j.status !== 'done',
    render: (job) => renderTracked(job),
    patch: (node, job, _v, now) => patchTracked(node, job, now),
    onUpdate: () => renderWork(),
  });

  wireEvents();
  registerHooks();
  renderRecent();
  loadRecent();
  renderWork();
}

export function focus() { input?.focus({ preventScroll: true }); }

export function onShow() {
  if (S.pendingOpen) return;
  // A switch made by a drop or an error action (choose a file, smaller model, edit link)
  // is followed at once by its hook, which cancels this; a plain tab click is not (G-03).
  S.quietShow = false;
  queueMicrotask(() => {
    if (S.quietShow) { S.quietShow = false; return; }
    const unseen = unseenTranscripts();
    if (unseen.length && !R.dirty) {
      const job = getJob(unseen[unseen.length - 1]);
      clearUnseenTranscripts();
      if (job && job.status === 'done') { openReader(sourceFromJob(job)); return; }
    } else if (!unseen.length) clearUnseenTranscripts();
    if (!R.open) loadRecent();
  });
}
/** Called by hooks that switch here for their own purpose: no automatic reader. */
const quiet = () => { S.quietShow = true; };

/* ================================================================ events and hooks */

function wireEvents() {
  on('jobs', ({ seed }) => {
    // After a reload, keep showing the transcript that is still running (T-01).
    if (seed && !S.tracked) {
      const running = allJobs().filter((j) => j.kind === 'transcript' && isActive(j) && !j.from_history)
        .sort((a, b) => b.created - a.created)[0];
      if (running) { S.tracked = running.id; trackView.update(allJobs()); }
    }
    const t = S.tracked ? getJob(S.tracked) : null;
    const sig = t ? `${t.id}:${t.status}` : '';
    if (sig !== S.lastStatus) { S.lastStatus = sig; box.refresh(); }
  });
  on('job-transition', ({ job, to }) => {
    if (job.kind === 'transcript' && (to === 'done' || to === 'skipped')) refreshRecentSoon();
  });
  const refreshOptions = () => { fillModels(); updateOptionsSummary(); if (P.state === 'ok') renderPreview(); };
  on('hardware', () => { O.hwNote.hidden = true; refreshOptions(); });
  on('models', refreshOptions);
  on('packs', () => { if (P.state === 'ok' && previewEl.querySelector('.tr-gpu')) renderPreview(); });
  on('load-error', ({ what }) => {
    if (what !== 'hardware') return;
    O.hwNote.textContent = "Couldn't read this PC's hardware, so the speech model list may be incomplete.";
    O.hwNote.hidden = false;
  });
}

function registerHooks() {
  // T-07: a file dropped anywhere in the window (the shell already stopped the navigation).
  registerHook('drop', (files) => {
    navigate('transcript');
    quiet();
    uploadFiles(files);
  });
  // G-03: a finished transcript opens by itself only when this tab is on screen and it is ours.
  registerHook('transcript.ready', (job) => {
    refreshRecentSoon();
    if (job.id !== S.tracked) return false;
    if (S.submitted !== null && input.value === S.submitted) box.clear();
    S.submitted = null;
    if (currentTab() !== 'transcript' || R.dirty) return false;
    openReader(sourceFromJob(job));
    const title = displayTitle(job);
    announce(`Transcript ready: ${title}`);
    if (document.hidden || !document.hasFocus()) notify.send('Transcript ready', title, () => {});
    return true;
  });
  registerHook('transcript.open', (job) => {
    S.pendingOpen = job.id;
    navigate('transcript');
    S.pendingOpen = null;
    clearUnseenTranscripts();
    openReader(sourceFromJob(getJob(job.id) || job), { scroll: true });
  });
  registerHook('transcript.chooseFile', () => { quiet(); fileInput.click(); });
  registerHook('transcript.smallerModel', () => {
    quiet();
    optionsEl.open = true;
    scrollToEl(O.model, 'center');
    O.model.focus({ preventScroll: true });
    flash(O.model.closest('.tr-row') || O.model);
  });
  registerHook('transcript.setLink', (url, { run = true } = {}) => {
    quiet();
    input.value = url;
    box.refresh();
    input.focus();
    if (run && parseLinks(url).links.length === 1) runProbe(parseLinks(url).links[0]);
  });
}

/* ================================================================ link box + preview (T-08) */

function trackedBusy() {
  const t = S.tracked ? getJob(S.tracked) : null;
  return !!t && isActive(t) && S.submitted !== null && input.value === S.submitted;
}
function canSubmit(parsed) {
  if (S.submitting) return 'Starting…';
  if (trackedBusy()) return 'This link is being transcribed';
  if (parsed.links.length === 1 && P.url === parsed.links[0] && P.state === 'ok') {
    const o = outcome(P.data);
    if (o.block) return o.block;
  }
  return true;
}

function onLinkChange(parsed) {
  if (parsed.links.length !== 1) {
    if (P.state !== 'idle' || P.url) { clearTimeout(P.timer); P.seq++; Object.assign(P, { url: '', state: 'idle', data: null, error: null }); }
    renderPreview(parsed);
    return;
  }
  const url = parsed.links[0];
  if (url === P.url && P.state !== 'idle') return;
  clearTimeout(P.timer);
  P.seq++;
  Object.assign(P, { url, state: 'reading', data: null, error: null });
  renderPreview(parsed);
  P.timer = setTimeout(() => runProbe(url), 550);
}
async function runProbe(url) {
  clearTimeout(P.timer);
  const seq = ++P.seq;
  Object.assign(P, { url, state: 'reading', data: null, error: null });
  renderPreview();
  try {
    const d = await get(`/api/probe?url=${encodeURIComponent(url)}`);
    if (seq !== P.seq) return;
    Object.assign(P, { state: 'ok', data: d });
  } catch (e) {
    if (seq !== P.seq) return;
    Object.assign(P, { state: 'error', error: e });
  }
  renderPreview();
  box.refresh();
}
function resetPreview() {
  clearTimeout(P.timer);
  P.seq++;
  Object.assign(P, { url: '', state: 'idle', data: null, error: null });
  renderPreview();
}

/**
 * The outcome line: what will happen with this link and the current Options,
 * using the backend's caption ranking (app/captions.py candidates()).
 * Returns {tone, icon, text, block (button title when Transcribe is disabled), whisper}.
 */
function outcome(d) {
  if (!d) return { tone: 'info', icon: 'info', text: '' };
  if (d.kind === 'playlist') return { tone: 'warn', icon: 'alert', text: TEXT.playlist, block: 'Transcripts work one video at a time' };
  if (d.is_live || d.live_status === 'is_live') return { tone: 'warn', icon: 'alert', text: TEXT.live, block: 'Transcripts need a finished video' };
  if (d.live_status === 'is_upcoming') return { tone: 'warn', icon: 'alert', text: TEXT.upcoming, block: 'Transcripts need a finished video' };
  const choice = O.lang.value;
  const translate = choice === 'translate';
  const manual = d.subtitles || [];
  const auto = d.auto_captions || [];
  const hasOrig = auto.some((k) => k.endsWith('-orig'));
  const match = (pool, lang) => pool.filter((k) => primary(k) === lang);
  const ok = (text) => ({ tone: 'ok', icon: 'check', text });
  const pc = (text) => ({ tone: 'info', icon: 'info', text, whisper: true });
  if (!O.captions.checked) {
    return pc(translate ? 'Will transcribe and translate to English on this PC.' : `Captions are turned off in Options. ${TEXT.onPC}`);
  }
  const site = d.site || siteName(d.url) || 'the site';
  if (translate) {
    if (match(manual, 'en').length) return ok(`Has English captions from the uploader. ${TEXT.soon}`);
    if (auto.includes('en-orig') || (!hasOrig && match(auto, 'en').length && primary(d.language) === 'en')) {
      return ok(`Has automatic English captions. Ready in about a second; may contain mistakes.`);
    }
    if (match(auto, 'en').length) return ok(`Will translate to English using ${site}'s automatic translation.`);
    return pc('Will transcribe and translate to English on this PC.');
  }
  const want = primary(choice) || primary(d.language);
  if (want) {
    const name = langName(want);
    if (match(manual, want).length) return ok(`Has ${name} captions from the uploader. ${TEXT.soon}`);
    const orig = auto.some((k) => k.endsWith('-orig') && primary(k) === want);
    // Without any '-orig' track, the site lists only its own automatic captions (no translations).
    if (orig || (!hasOrig && match(auto, want).length)) return ok(`Has automatic ${name} captions. Ready in about a second; may contain mistakes.`);
    return pc(manual.length || auto.length ? `No captions in ${name}. ${TEXT.onPC}` : `No captions. ${TEXT.onPC}`);
  }
  if (manual.length === 1) {
    const name = langName(manual[0]);
    return ok(name ? `Has ${name} captions from the uploader. ${TEXT.soon}` : `Has captions from the uploader. ${TEXT.soon}`);
  }
  const orig = auto.find((k) => k.endsWith('-orig'));
  if (orig) return ok(`Has automatic ${langName(orig)} captions. Ready in about a second; may contain mistakes.`);
  return pc(`No captions. ${TEXT.onPC}`);
}

function renderPreview(parsed = box ? box.parse() : parseLinks(input.value)) {
  const show = (...nodes) => { previewEl.replaceChildren(...nodes); previewEl.hidden = false; };
  if (parsed.links.length > 1) {
    show(outcomeLine({ tone: 'info', icon: 'info', text: `${num(parsed.links.length)} links. Each becomes its own transcript.` }));
    return;
  }
  if (!parsed.links.length || P.state === 'idle') { previewEl.hidden = true; previewEl.replaceChildren(); return; }
  if (P.state === 'reading') { show(el('div.skeleton-row', spinner(), 'Reading link…')); return; }
  if (P.state === 'error') {
    show(el('div.tr-outcome', { dataset: { tone: 'warn' } }, icon('alert'),
      el('div.tr-outcome-text', el('div.tr-outcome-title', P.error?.title || "Couldn't read this link"),
        el('div', 'You can still try transcribing it.'))));
    return;
  }
  const d = P.data || {};
  const oc = outcome(d);
  const meta = d.kind === 'playlist'
    ? joined([d.uploader ? bdi(d.uploader) : '', d.is_channel ? 'Channel' : 'Playlist', d.count ? `${plural(d.count, 'video')}${d.count_more ? '+' : ''}` : ''])
    : joined([d.uploader ? bdi(d.uploader) : '', d.duration ? duration(d.duration) : '']);
  // yt-dlp appends ' YYYY-MM-DD HH:MM' to a live stream's title; the Live tab drops it too.
  const title = String(d.title || '').replace(d.is_live ? /\s+\d{4}-\d{2}-\d{2} \d{2}:\d{2}\s*$/ : /$^/, '');
  show(el('div.preview.tr-pv',
    thumbFor(d.thumbnail, d.kind === 'playlist' ? 'queue' : 'video'),
    el('div.preview-body',
      el('div.preview-title', { dir: 'auto', title }, title),
      meta.length ? el('div.preview-meta', meta) : null,
      outcomeLine(oc),
      oc.whisper ? gpuOffer() : null)));
}
function outcomeLine(oc) {
  return el('div.tr-outcome', { dataset: { tone: oc.tone } }, icon(oc.icon), el('span.tr-outcome-text', oc.text));
}

/** G-12: the one-time GPU download, offered where the speed matters (NVIDIA present, not ready). */
function gpuOffer() {
  const hw = STATE.hw;
  if (!hw || !hw.nvidia_name || hw.gpu_ready) return null;
  const pr = STATE.packs?.progress || {};
  if (pr.busy && pr.task !== 'ffmpeg') {
    return el('div.tr-gpu.help', spinner(), `Installing GPU support · ${Math.round(pr.percent || 0)}%`);
  }
  const size = hw.gpu_pack_size_mb || STATE.packs?.gpu_pack_size_mb || 528;
  return el('div.tr-gpu.help',
    el('span', `Transcription runs on your processor. Your ${hw.nvidia_name} can do it many times faster after a one-time ${num(size)} MB download. `),
    linkButton('Download GPU support', openGpuDialog));
}
function openGpuDialog() {
  const hw = STATE.hw || {};
  const lic = STATE.packs?.gpu_pack_license || {};
  const size = hw.gpu_pack_size_mb || STATE.packs?.gpu_pack_size_mb || 528;
  const cancel = button('Cancel', 'secondary');
  const ok = button('Download GPU support', 'primary');
  const d = openDialog({
    title: 'Download GPU support',
    cls: 'confirm',
    body: [
      el('p', `A one-time ${num(size)} MB download that runs in the background. Videos without captions then transcribe many times faster on your ${hw.nvidia_name || 'graphics card'}.`),
      lic.text ? el('p.help.mt-3', lic.text) : null,
      lic.url ? el('p.mt-2', linkButton(lic.name || "NVIDIA's license terms", () => openUrl(lic.url).catch(toastError))) : null,
    ],
    footer: el('div.end', cancel, ok),
    initialFocus: cancel,
  });
  cancel.addEventListener('click', () => d.close(false));
  ok.addEventListener('click', () => d.close(true));
  d.closed.then(async (yes) => {
    if (!yes) return;
    const job = installPack('gpu');
    renderPreview();
    const r = await job;
    if (r && r.ok !== false) toast('GPU support is installed. Transcription now runs on your graphics card.', { tone: 'ok' });
    else toast(`Couldn't install GPU support: ${r?.error || r?.message || 'the download failed'}`, { tone: 'err' });
    renderPreview();
  });
}

/* ================================================================ Options (T-02, T-03) */

function buildOptions() {
  O.lang = el('select#trLang.select-md',
    el('option', { value: '' }, 'Same as the video (recommended)'),
    O.langGroup = el('optgroup', { label: 'The video is in…' }, LANGS.map((c) => el('option', { value: c }, langLabel(c)))),
    el('optgroup', { label: 'Translate' }, el('option', { value: 'translate' }, 'Translate to English')));
  O.captions = el('input#trCaptions', { type: 'checkbox', checked: true });
  O.model = el('select#trModel.tr-model');
  O.modelNote = el('p.help', { hidden: true });
  O.hwNote = el('p.help', { hidden: true });
  O.beam = segmented({
    name: 'trBeam', legend: 'Accuracy', id: 'trBeam', value: '5',
    options: [{ value: '5', label: 'Standard' }, { value: '10', label: 'Thorough, slower' }],
  });
  O.vad = el('input#trVad', { type: 'checkbox', checked: true });
  O.hot = el('input#trHotwords', { type: 'text', placeholder: 'e.g. names, brands or technical terms, separated by commas', maxlength: '500', spellcheck: 'false', autocomplete: 'off' });
  O.keep = el('input#trKeepAudio', { type: 'checkbox' });

  const row = (label, control, ...extra) => el('div.tr-row', label, el('div.tr-ctl', control, ...extra));
  const body = [
    row(el('label.label', { for: 'trLang' }, 'Language'), O.lang),
    row(el('span.tr-gap'), el('label.check', O.captions,
      el('span.check-text', "Use the video's own captions when available (fastest)",
        el('span.help', 'Turn this off if the captions are poor. The speech is then transcribed on this PC.')))),
    el('div.sub-heading', 'SPEECH RECOGNITION · used when there are no captions'),
    row(el('label.label', { for: 'trModel' }, 'Speech model'), O.model, O.modelNote, O.hwNote),
    row(el('span.label', { 'aria-hidden': 'true' }, 'Accuracy'),
      el('div.row.gap-3.tr-acc', O.beam, el('label.check', O.vad, el('span.check-text', 'Skip long silences (faster)')))),
    row(el('label.label', { for: 'trHotwords' }, 'Names and terms to listen for'), O.hot),
    row(el('span.tr-gap'), el('label.check', O.keep,
      el('span.check-text', 'Also save the audio file', el('span.help', 'Only applies to links without captions.')))),
  ];
  const d = disclosure('Options', { id: 'trOptions', body });
  optionsEl = d;
  d.setAttribute('data-save-scope', '');
  d.querySelector('summary').append(el('span.save-state', { 'aria-live': 'polite' }));

  // The language list must hold whatever the settings name before bindSetting applies it.
  on('config', () => { ensureLangOption(CONFIG.transcript_language); queueMicrotask(afterConfig); });
  bindSetting(O.lang, 'transcript_language');
  bindSetting(O.captions, 'prefer_native_subs');
  bindSetting(O.model, 'whisper_model');
  bindSetting(O.beam, 'whisper_beam', { toServer: (v) => Number(v), fromServer: (v) => (Number(v) >= 8 ? '10' : '5') });
  bindSetting(O.vad, 'whisper_vad');
  for (const c of [O.lang, O.captions, O.model]) {
    c.addEventListener('change', () => { updateOptionsSummary(); if (P.state === 'ok') { renderPreview(); box.refresh(); } });
  }
  fillModels();
  updateOptionsSummary();
  return d;
}
function afterConfig() {
  fillModels();
  updateOptionsSummary();
  if (P.state === 'ok') { renderPreview(); box.refresh(); }
}
function ensureLangOption(code) {
  if (!code || code === 'translate' || [...O.lang.options].some((o) => o.value === code)) return;
  O.langGroup.append(el('option', { value: code }, langLabel(primary(code)) || code));
}

const models = () => STATE.hw?.models || [];
const modelEntry = (id) => models().find((m) => m.id === id);
const modelName = (id) => modelEntry(id)?.label || MODEL_NAMES[id] || id;
function modelLabel(m) {
  const tags = [];
  if (m.recommended) tags.push('recommended');
  if (m.installed) tags.push('downloaded');
  if (!m.fits) tags.push('needs more graphics memory');
  const base = m.option_label || [m.label, m.note, m.size_label].filter(Boolean).join(' · ');
  return tags.length ? `${base} (${tags.join(', ')})` : base;
}
/** The model list from /api/hardware; a model that no longer fits gives way to the recommended one (FIN-15). */
function fillModels() {
  const list = models();
  const saved = CONFIG.whisper_model || '';
  if (!list.length) {
    if (!O.model.options.length) {
      const id = saved || 'large-v3-turbo';
      O.model.append(el('option', { value: id }, MODEL_NAMES[id] || id));
      O.model.value = id;
    }
    return;
  }
  const focused = document.activeElement === O.model;
  const current = focused ? O.model.value : (saved || O.model.value);
  const sig = list.map((m) => `${m.id}:${modelLabel(m)}:${m.fits}`).join('|');
  if (O.model._sig !== sig) {
    O.model._sig = sig;
    O.model.replaceChildren(...list.map((m) => el('option', { value: m.id, disabled: !m.fits }, modelLabel(m))));
  }
  let want = current;
  const entry = modelEntry(want);
  if (!entry || !entry.fits) want = STATE.hw.recommended_model || list.find((m) => m.fits)?.id || list[0].id;
  if (!focused) O.model.value = want;
  if (isConfigLoaded() && saved && want !== saved && (!entry || !entry.fits)) {
    saveSettings({ whisper_model: want }).catch(() => {});
  }
}
/** The model that will really run (app/transcribe.py pick_model). */
function effectiveModel() {
  const id = O.model.value || CONFIG.whisper_model || 'large-v3-turbo';
  const choice = O.lang.value;
  const m = modelEntry(id);
  const installed = new Set(models().filter((x) => x.installed).map((x) => x.id));
  if (choice === 'translate' && m && !m.can_translate) {
    return { id: installed.has('large-v3') ? 'large-v3' : 'medium', why: 'translate' };
  }
  if (choice && choice !== 'translate' && primary(choice) !== 'en' && /^distil/.test(id)) {
    const pick = ['large-v3-turbo', 'large-v3', 'medium', 'small'].find((x) => installed.has(x)) || STATE.hw?.recommended_model || 'small';
    return { id: pick, why: 'english' };
  }
  return { id, why: '' };
}
function updateOptionsSummary() {
  const choice = O.lang.value;
  const langText = choice === '' ? 'Same as the video' : choice === 'translate' ? 'Translate to English' : (langName(choice) || choice);
  const eff = effectiveModel();
  const model = modelName(eff.id);
  optionsEl.setSummary(O.captions.checked
    ? `Language: ${langText} · Captions first, then ${model} on this PC`
    : `Language: ${langText} · Always transcribed on this PC with ${model}`);
  const chosen = modelName(O.model.value || eff.id);
  if (eff.why === 'translate') O.modelNote.textContent = `${chosen} can't translate, so ${model} is used when translating.`;
  else if (eff.why === 'english') O.modelNote.textContent = `${chosen} only understands English, so ${model} is used for ${langText}.`;
  O.modelNote.hidden = !eff.why;
}

/** The options a transcript job is sent with (T-02). */
function jobOptions() {
  const choice = O.lang.value;
  const out = {
    language: choice === 'translate' ? '' : choice,
    translate: choice === 'translate',
    prefer_captions: O.captions.checked,
    beam: Number(O.beam.querySelector('input:checked')?.value || 5),
    vad: O.vad.checked,
    hotwords: O.hot.value.trim(),
    keep_audio: O.keep.checked,
  };
  if (O.model.value && modelEntry(O.model.value)) out.model = O.model.value;
  return out;
}

/* ================================================================ submit (T-01) */

async function submit(parsed) {
  if (R.dirty && !(await confirmDiscard())) return;
  S.submitting = true;
  box.refresh();
  const hints = {};
  if (P.state === 'ok' && P.data?.kind === 'video' && parsed.links.includes(P.url)) {
    hints[P.url] = { title: P.data.title || '', thumbnail: P.data.thumbnail || '', uploader: P.data.uploader || '' };
  }
  try {
    const r = await post('/api/jobs', { url: parsed.links.join('\n'), kind: 'transcript', options: jobOptions(), hints });
    const jobs = r.jobs || [];
    if (jobs.length) {
      S.submitted = input.value;
      track(jobs[0], jobs.length - 1);
    }
  } catch (e) {
    if (e.code === 'bad_link' || e.field || e.status === 400) box.setHint(e.text, 'error');
    else toastError(e);
  } finally {
    S.submitting = false;
    box.refresh();
  }
}

/** Show this job in the work area (T-01). more = how many others went to the Queue. */
function track(job, more = 0) {
  S.tracked = job.id;
  S.more = more;
  S.lastStatus = `${job.id}:${job.status}`;
  const list = allJobs();
  trackView.update(list.some((j) => j.id === job.id) ? list : [job, ...list]);
  renderWork();
}

/* ================================================================ tracked job card (T-01) */

function renderTracked(job) {
  const card = renderCard(job, 'full');
  card.classList.add('tr-job');
  const p = card._p;
  p.steps = el('ol.steps');
  p.status.after(p.steps);
  p.pct = el('span.tr-pct', { 'aria-hidden': 'true' });
  const wrap = el('div.tr-progress');
  p.bar.replaceWith(wrap);
  wrap.append(p.bar, p.pct);
  p.progress = wrap;
  patchTracked(card, job);
  return card;
}
function patchTracked(card, job, now) {
  patchCard(card, job, now);
  const p = card._p;
  // A transcript skipped as a live stream is not 'Done' (shared statusOf reads any
  // reason without the 'Skipped:' prefix as done).
  if (job.status === 'skipped' && !/^Nothing new/i.test(job.stage || '')) setText(p.pillWord, 'Skipped');
  const active = ACTIVE.includes(job.status);
  const steps = (job.steps || []).filter((s) => s.state !== 'skipped');
  const showSteps = active && steps.length > 0;
  card.classList.toggle('show-steps', showSteps);
  if (showSteps) {
    // A model already on disk is only loaded: say so before the step starts (the backend relabels it then).
    const haveModel = !!modelEntry(job.options?.model || CONFIG.whisper_model)?.installed;
    patchSteps(p.steps, {
      steps: steps.map((s) => {
        if (s.state === 'active') return { ...s, note: liveNote(job, s) };
        if (s.key === 'model' && s.state === 'pending' && haveModel) return { ...s, label: 'Loading the speech model' };
        return s;
      }),
    });
  }
  p.progress.hidden = p.bar.hidden;
  const st = statusOf(job, now);
  setText(p.pct, st.bar && !st.bar.indeterminate && !p.bar.hidden ? `${p.bar.getAttribute('aria-valuenow') || st.bar.value}%` : '');
}
/** The active step's live numbers: model MB, audio bytes, transcription % and time left. */
function liveNote(job, s) {
  if (job.stage === STAGE_WAITING) return 'waiting for another transcription to finish';
  const pct = Math.round((Number(job.progress) || 0) * 100);
  const parts = [];
  if (s.key === 'transcribing') {
    if (!job.indeterminate && pct > 0) parts.push(`${pct}%`);
    if (job.eta_s) parts.push(eta(job.eta_s));
  } else if (s.key === 'audio') {
    if (job.bytes_total) parts.push(bytesOf(job.bytes_done, job.bytes_total));
    else if (job.bytes_done) parts.push(bytes(job.bytes_done));
    if (job.eta_s && job.bytes_total) parts.push(eta(job.eta_s));
  } else if (s.note) parts.push(s.note);
  return parts.join(' · ');
}

/* ================================================================ work area */

function renderWork() {
  if (!workEl) return;
  const trackedShown = !!trackView && trackView.items.length > 0;
  uploadEl.hidden = !U.active;
  readerEl.hidden = !R.open;
  recentEl.hidden = trackedShown || R.open || U.active;
  const showMore = S.more > 0 && trackedShown;
  moreEl.hidden = !showMore;
  if (showMore) setText(moreEl.firstChild, `+${num(S.more)} more in the Queue`);
}

/* ---- Recent transcripts (T-10) ---- */

function refreshRecentSoon() {
  clearTimeout(recent.timer);
  recent.timer = setTimeout(loadRecent, 400);
}
async function loadRecent() {
  const seq = ++recent.seq;
  try {
    const r = await get('/api/transcripts?limit=8');
    if (seq !== recent.seq) return;
    recent.rows = r.transcripts || [];
  } catch (_) {
    if (seq !== recent.seq) return;
    recent.rows = recent.rows || [];
  }
  renderRecent();
}
function renderRecent() {
  const rows = recent.rows || [];
  if (!rows.length) {
    recentEl.replaceChildren(el('div.empty.dashed.tr-empty', el('p.empty-text', TEXT.empty)));
    return;
  }
  const list = el('ul.tr-recent-list', rows.map((row) => el('li', recentRow(row))));
  const all = button('Show all in folder', 'quiet', {
    size: 'sm', icon: 'folder',
    onClick: () => revealPath(CONFIG.transcript_dir || '').catch(toastError),
  });
  recentEl.replaceChildren(card({ title: 'Recent transcripts', id: 'trRecent', actions: [all], body: [list] }));
}
function recentRow(row) {
  const meta = [row.site || (row.url ? siteName(row.url) : ''), relTime(row.date), row.stats ? wordCount(row.stats) : (row.words ? plural(row.words, 'word') : '')]
    .filter(Boolean).join(' · ');
  const b = el('button.tr-recent-row', { type: 'button', title: row.title },
    icon('text'), el('span.tr-recent-title', { dir: 'auto' }, row.title || row.stem), el('span.tr-recent-meta', meta));
  b.addEventListener('click', () => openReader(sourceFromRow(row), { scroll: true }));
  return b;
}

/* ---- uploads (T-07, T-13) ---- */

function buildUpload() {
  UD.name = el('bdi', { dir: 'auto' });
  UD.pctText = el('span.tabular');
  UD.title = el('div.j-title', 'Copying ', UD.name, UD.pctText);
  UD.detail = el('span.j-detail');
  UD.status = el('div.j-status', UD.detail);
  UD.fill = el('i');
  UD.bar = el('div.bar', { role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100' }, UD.fill);
  UD.cancel = button('Cancel', 'secondary', { size: 'sm', onClick: () => U.ctrl?.abort() });
  // The percentage is in the title line ('Copying {name}… 38%'), so the bar carries no label of its own.
  uploadEl = el('article.job.tr-job.tr-upload', { hidden: true, dataset: { tone: 'run', kind: 'transcript' }, 'aria-label': 'Copying files' },
    el('div.thumb', icon('upload')),
    el('div.j-body', UD.title, UD.status, el('div.tr-progress', UD.bar)),
    el('div.j-actions', UD.cancel));
}
function patchUpload() {
  setText(UD.name, U.name);
  const known = typeof U.frac === 'number';
  const pct = known ? Math.min(100, Math.round(U.frac * 100)) : 0;
  // 'Copying {name} to Media Toolkit…' until the size is known, then 'Copying {name}… 38%'.
  setText(UD.pctText, known ? `… ${pct}%` : ' to Media Toolkit…');
  UD.status.hidden = U.total < 2;
  setText(UD.detail, U.total > 1 ? `File ${num(U.index)} of ${num(U.total)}` : '');
  UD.bar.classList.toggle('indeterminate', !known);
  UD.bar.setAttribute('aria-label', `Copying ${U.name}`);
  if (known) {
    UD.fill.style.width = `${pct}%`;
    UD.bar.setAttribute('aria-valuenow', String(pct));
  } else UD.bar.removeAttribute('aria-valuenow');
}
/** Upload every file in turn; the first is tracked, the rest go to the Queue. */
async function uploadFiles(fileList) {
  const files = Array.from(fileList || []).filter(Boolean);
  if (!files.length) return;
  if (U.active) { U.queue.push(...files); U.total += files.length; patchUpload(); return; }
  if (R.dirty && !(await confirmDiscard())) return;
  Object.assign(U, { active: true, queue: files.slice(), index: 0, total: files.length, name: '', frac: null, ctrl: new AbortController() });
  renderWork();
  const options = jobOptions();
  let first = true, added = 0;
  const failed = [];
  while (U.queue.length) {
    const f = U.queue.shift();
    U.index++;
    U.name = f.name;
    U.frac = null;
    patchUpload();
    const fd = new FormData();
    fd.append('file', f, f.name);
    fd.append('options', JSON.stringify(options));
    try {
      const r = await upload('/api/transcribe-file', fd, {
        signal: U.ctrl.signal,
        onProgress: (x) => { U.frac = x; patchUpload(); },
      });
      const job = (r?.jobs || [])[0];
      if (!job) continue;
      if (first) { first = false; S.submitted = null; track(job, 0); } else { added++; S.more = added; renderWork(); }
    } catch (e) {
      if (e.code === 'aborted') break;
      failed.push({ name: f.name, error: e });
    }
  }
  U.active = false;
  U.queue = [];
  renderWork();
  box.refresh();
  if (added) toast(`${plural(added, 'more file', 'more files')} added to the Queue`, { tone: 'info', actions: [{ label: 'View', onClick: () => navigate('queue') }] });
  for (const x of failed.slice(0, 2)) {
    toast(["Couldn't copy “", bdi(truncate(x.name, 60)), `”: ${x.error.title || x.error.text || 'the upload failed'}`], { tone: 'err' });
  }
}

/* ================================================================ the reader (T-04, T-06, T-11, T-12) */

function buildReader() {
  RD.title = el('h2#trReaderTitle.tr-title', { dir: 'auto' });
  RD.prov = el('p.tr-prov');
  RD.stats = el('p.tr-stats');
  RD.longText = el('span');
  RD.long = el('p.tr-long', { hidden: true }, icon('alert'), RD.longText);
  RD.close = iconButton('x', 'Close transcript', () => closeReader());
  RD.primary = button('Copy for AI chat', 'primary', { cls: 'primary-min', onClick: onPrimary });
  RD.prev = iconButton('chevron-left', 'Previous part', () => movePart(-1));
  RD.next = iconButton('chevron-right', 'Next part', () => movePart(1));
  RD.partNav = el('span.tr-partnav', { hidden: true }, RD.prev, RD.next);
  RD.copy = button('Copy text', 'secondary', { onClick: onCopyText });
  RD.saveAs = button('Save as', 'secondary', { iconEnd: 'chevron-down' });
  menu(RD.saveAs, SAVE_FORMATS.map((f) => ({ label: f.label, onSelect: () => saveAs(f.fmt) })), { label: 'Save as' });
  RD.show = button('Show file', 'quiet', { onClick: showFile });
  RD.edit = button('Edit', 'quiet', { onClick: toggleEdit, cls: 'tr-edit' });
  RD.edit.setAttribute('aria-pressed', 'false');
  RD.saveEdits = button('Save changes', 'secondary', { onClick: saveEdits });
  RD.saveEdits.hidden = true;
  RD.view = tabSwitch({
    label: 'Transcript view', value: 'text', onChange: onViewChange,
    options: [{ value: 'text', label: 'Text' }, { value: 'ts', label: 'With timestamps' }],
  });
  for (const b of RD.view.querySelectorAll('[role=tab]')) {
    b.id = `trView-${b.dataset.value}`;
    b.setAttribute('aria-controls', 'trReaderBody');
  }
  RD.body = el('div#trReaderBody.tr-body', { role: 'tabpanel', 'aria-labelledby': 'trView-text' });
  RD.body.addEventListener('input', onEditInput);
  readerEl = el('article#trReader.card.tr-reader', { hidden: true, 'aria-label': 'Transcript text' },
    el('div.tr-head', el('div.tr-titles', RD.title, RD.prov, RD.stats, RD.long), RD.close),
    el('div.tr-bar',
      el('div.tr-actions', RD.primary, RD.partNav, RD.copy, RD.saveAs, RD.show, RD.edit, RD.saveEdits),
      RD.view),
    el('hr.divider'),
    RD.body);
}

function sourceFromJob(job) {
  const r = job.result || {};
  const m = r.meta || {};
  return {
    key: `job:${job.id}`, jobId: job.id, stem: r.stem || '',
    title: m.title || displayTitle(job), uploader: m.uploader || job.uploader || '',
    duration: m.duration || r.stats?.duration || 0, url: m.url || job.url || '',
    stats: r.stats || {}, detail: r.detail || {}, file: transcriptFile(job)?.path || '', dir: r.output_dir || '',
  };
}
function sourceFromRow(row) {
  const job = allJobs().find((j) => j.kind === 'transcript' && j.status === 'done' && j.result?.stem === row.stem);
  return {
    key: job ? `job:${job.id}` : `stem:${row.stem}`, jobId: job?.id || null, stem: row.stem,
    title: row.title || row.stem, uploader: row.uploader || '', duration: row.duration || row.stats?.duration || 0,
    url: row.url || '', stats: row.stats || {}, detail: row.detail || {}, file: row.path || '', dir: CONFIG.transcript_dir || '',
  };
}

async function openReader(src, { scroll = false } = {}) {
  if (R.open && R.key === src.key) {
    renderWork();
    if (scroll) revealReader();
    return true;
  }
  if (R.dirty && !(await confirmDiscard())) return false;
  stopEditing();
  R = { ...src, open: true, seq: R.seq + 1, view: lastView, part: 0, parts: null, editing: false, dirty: false, cache: {} };
  RD.saveEdits.hidden = true;
  setText(RD.title, R.title);
  RD.title.title = R.title;
  setText(RD.prov, provenanceLine(R.detail, R.url));
  RD.prov.title = engineLine(R.detail);
  setText(RD.stats, [R.duration ? duration(R.duration) : '', wordCount(R.stats),
    R.stats.tokens ? `about ${num(R.stats.tokens)} tokens` : ''].filter(Boolean).join(' · '));
  RD.long.hidden = true;
  RD.view.setValue(R.view);
  renderWork();
  if (scroll) revealReader();
  await renderView();
  return true;
}
async function closeReader() {
  if (R.dirty && !(await confirmDiscard())) return;
  stopEditing();
  R = { ...R, open: false, key: '', dirty: false, cache: {} };
  RD.body.replaceChildren();
  renderWork();
  loadRecent();
  input.focus({ preventScroll: true });
}
/** Bring the reader's title to 16 px under the top bar when it is not comfortably in view (T-03). */
function revealReader() {
  requestAnimationFrame(() => {
    const top = readerEl.getBoundingClientRect().top;
    if (top < 60 || top > innerHeight * 0.55) scrollToEl(readerEl, 'start');
  });
}

/** Where the text came from, in words (copy › Transcript › Provenance). */
function provenanceLine(d = {}, url = '') {
  const site = d.site || siteName(url) || 'the site';
  switch (d.source) {
    case 'official': {
      const L = langName(d.caption_lang || d.language);
      return L ? `From the uploader's ${L} captions` : "From the uploader's captions";
    }
    case 'auto': {
      const L = langName(d.caption_lang || d.language);
      return L ? `From ${site}'s automatic ${L} captions · may contain mistakes` : `From ${site}'s automatic captions · may contain mistakes`;
    }
    case 'auto_translated': {
      const L = langName(d.source_lang);
      return L ? `${site}'s machine translation from ${L} to English · may contain mistakes` : `${site}'s machine translation to English · may contain mistakes`;
    }
    case 'whisper': {
      if (d.translated || d.task === 'translate') return 'Transcribed and translated to English on this PC';
      const L = langName(d.language || d.source_lang);
      return L ? `Transcribed on this PC · ${L} (${d.language_chosen ? 'as chosen' : 'detected'})` : 'Transcribed on this PC';
    }
    default: return '';
  }
}
/** Tooltip for a speech-recognition transcript: 'Whisper large-v3-turbo · CUDA float16 · 12× realtime'. */
function engineLine(d = {}) {
  if (d.source !== 'whisper') return '';
  const parts = [`Whisper ${d.model || String(d.engine || '').replace(/^Whisper\s*/, '')}`.trim()];
  const dev = [String(d.device || '').toUpperCase(), d.compute_type].filter(Boolean).join(' ');
  if (dev) parts.push(dev);
  if (d.realtime_factor) parts.push(`${Math.round(d.realtime_factor)}× realtime`);
  return parts.join(' · ');
}

/* ---- fetching and rendering a view (UI-14: only the latest request may paint) ---- */

async function readerText(fmt) {
  const tryStem = () => api(`/api/transcripts/${encodeURIComponent(R.stem)}?format=${fmt}`);
  if (R.jobId) {
    try { return await api(`/api/jobs/${encodeURIComponent(R.jobId)}/transcript?format=${fmt}`); } catch (e) {
      if (e.status === 404 && R.stem) { R.jobId = null; return tryStem(); }
      throw e;
    }
  }
  return tryStem();
}
async function readerPost(action, body) {
  const viaStem = () => post(`/api/transcripts/${encodeURIComponent(R.stem)}/${action}`, body);
  if (R.jobId) {
    try { return await post(`/api/jobs/${encodeURIComponent(R.jobId)}/${action}`, body); } catch (e) {
      if (e.status === 404 && R.stem) return viaStem();
      throw e;
    }
  }
  return viaStem();
}
async function loadView(view) {
  if (view === 'text') {
    if (R.cache.txt === undefined) R.cache.txt = String(await readerText('txt') ?? '');
    return R.cache.txt;
  }
  if (!R.cache.blocks) {
    const raw = await readerText('json');
    const data = typeof raw === 'string' ? JSON.parse(raw) : raw;
    R.cache.blocks = blocksOf(data?.segments || []);
  }
  return R.cache.blocks;
}
async function renderView() {
  const seq = ++R.seq;
  const { view, key } = R;
  RD.body.setAttribute('aria-labelledby', `trView-${view}`);
  RD.edit.hidden = view !== 'text';
  const slow = setTimeout(() => {
    if (seq === R.seq) RD.body.replaceChildren(el('div.skeleton-row', spinner(), 'Opening the transcript…'));
  }, 150);
  try {
    const data = await loadView(view);
    if (seq !== R.seq || key !== R.key) return;
    clearTimeout(slow);
    RD.body.classList.toggle('tr-text', view === 'text');
    RD.body.classList.toggle('tr-ts', view === 'ts');
    RD.body.replaceChildren(...(view === 'text' ? textNodes(data) : tsNodes(data)));
    updateParts();
  } catch (e) {
    if (seq !== R.seq) return;
    clearTimeout(slow);
    RD.body.replaceChildren(callout('err', { title: "Couldn't open this transcript", text: e.text || e.message }));
  }
}
function textNodes(txt) {
  const paras = paragraphs(txt);
  if (!paras.length) return [el('p.help', 'This transcript is empty.')];
  return paras.map((p) => el('p', { dir: 'auto' }, linkified(p)));
}
function tsNodes(blocks) {
  if (!blocks.length) return [el('p.help', 'This transcript is empty.')];
  const deep = R.url && isYouTube(R.url);
  return blocks.map((b) => {
    const t = stamp(b.start);
    const chip = deep
      ? el('button.tr-chip', { type: 'button', dir: 'ltr', title: `Open the video at ${t}`, 'aria-label': `Open the video at ${t}` }, t)
      : el('span.tr-chip', { dir: 'ltr' }, t);
    if (deep) chip.addEventListener('click', () => openAt(b.start));
    return el('div.tr-ts-row', { dir: textDir(b.text) }, chip, el('p', { dir: 'auto' }, linkified(b.text)));
  });
}
/** T-12: open the source at that moment in the normal browser. */
async function openAt(sec) {
  try {
    const u = new URL(R.url);
    u.searchParams.set('t', `${Math.floor(sec)}s`);
    await openUrl(u.toString(), R.stem || '');
  } catch (e) { toastError(e); }
}

/* ---- copying (T-06, T-11) ---- */

function currentText() {
  if (R.view === 'text') return R.dirty ? editedText() : paragraphs(R.cache.txt).join('\n\n');
  return (R.cache.blocks || []).map((b) => `[${stamp(b.start)}] ${b.text}`).join('\n\n');
}
function aiHeader() {
  const lines = [`Transcript of “${R.title}”`];
  if (R.uploader) lines.push(`Channel: ${R.uploader}`);
  if (R.duration) lines.push(`Length: ${duration(R.duration)}`);
  if (R.url) lines.push(`Link: ${R.url}`);
  return lines.join('\n');
}
/** Long transcripts: the warning, and the primary becomes 'Copy part 1 of 3' (by length, FIN-7). */
function updateParts() {
  const text = currentText();
  const long = text.length > LONG_AT;
  RD.long.hidden = !long;
  if (long) {
    const tokens = R.stats.tokens || Math.ceil(text.length / 4);
    setText(RD.longText, `Long transcript (about ${num(tokens)} tokens). Some AI chats can't take it in one message.`);
    R.parts = chunk(text, PART_SIZE);
    R.part = Math.max(0, Math.min(R.part, R.parts.length - 1));
  } else R.parts = null;
  paintParts();
}
function paintParts() {
  const lab = RD.primary.querySelector('.btn-label');
  const n = R.parts?.length || 0;
  if (n > 1) {
    setText(lab, `Copy part ${R.part + 1} of ${n}`);
    RD.partNav.hidden = false;
    RD.prev.disabled = R.part === 0;
    RD.next.disabled = R.part >= n - 1;
    RD.primary.title = 'Copies one part at a time, with instructions for the AI chat';
  } else {
    setText(lab, 'Copy for AI chat');
    RD.partNav.hidden = true;
    RD.primary.title = '';
  }
}
function movePart(d) {
  if (!R.parts) return;
  R.part = Math.max(0, Math.min(R.parts.length - 1, R.part + d));
  paintParts();
}
async function onPrimary() {
  if (R.cache[R.view === 'text' ? 'txt' : 'blocks'] === undefined) return;
  if (R.dirty) updateParts();
  const text = currentText();
  if (!R.parts || R.parts.length < 2) {
    const ok = await copyText(`${aiHeader()}\n\n${text}\n`);
    if (!ok) { toast("Couldn't copy. Try again.", { tone: 'err' }); return; }
    const words = wordCount(R.stats);
    toast(R.url ? `Copied ${words} with the title and link. Paste it into any AI chat.` : `Copied ${words} with the title. Paste it into any AI chat.`, { tone: 'ok', key: 'tr-copy' });
    return;
  }
  const n = R.parts.length;
  const i = R.part;
  let out = R.parts[i];
  if (i === 0) out = `I will send a transcript in ${n} parts. Reply only “OK” until you have all ${n}, then wait for my question.\n\n${aiHeader()}\n\n${out}`;
  out += `\n\n[End of part ${i + 1} of ${n}]\n`;
  if (!(await copyText(out))) { toast("Couldn't copy. Try again.", { tone: 'err' }); return; }
  toast(i < n - 1 ? `Copied part ${i + 1}. Next: part ${i + 2} of ${n}` : `Copied part ${n} of ${n}.`, { tone: 'ok', key: 'tr-part' });
  if (i < n - 1) { R.part = i + 1; paintParts(); }
}
async function onCopyText() {
  const text = currentText();
  if (!text) return;
  if (!(await copyText(text))) { toast("Couldn't copy. Try again.", { tone: 'err' }); return; }
  toast(`Copied ${wordCount(R.stats)}.`, { tone: 'ok', key: 'tr-copy' });
}
async function saveAs(fmt) {
  try {
    const r = await readerPost('export', { format: fmt });
    if (r?.path) await revealPath(r.path);
  } catch (e) { toastError(e); }
}
async function showFile() {
  const job = R.jobId ? getJob(R.jobId) : null;
  const path = (job && transcriptFile(job)?.path) || R.file || R.dir;
  if (!path) return;
  try { await revealPath(path); } catch (e) { toastError(e); }
}

/* ---- editing (T-12) ---- */

function toggleEdit() {
  if (R.editing) stopEditing();
  else startEditing();
}
function startEditing() {
  if (R.view !== 'text' || R.cache.txt === undefined) return;
  R.editing = true;
  RD.body.setAttribute('contenteditable', 'plaintext-only');
  RD.body.setAttribute('role', 'textbox');
  RD.body.setAttribute('aria-multiline', 'true');
  RD.body.setAttribute('aria-label', 'Transcript text');
  RD.body.classList.add('is-editing');
  RD.edit.setAttribute('aria-pressed', 'true');
  RD.body.focus();
}
function stopEditing() {
  R.editing = false;
  if (!RD.body) return;
  RD.body.removeAttribute('contenteditable');
  RD.body.setAttribute('role', 'tabpanel');
  RD.body.removeAttribute('aria-multiline');
  RD.body.removeAttribute('aria-label');
  RD.body.classList.remove('is-editing');
  RD.edit.setAttribute('aria-pressed', 'false');
}
let editTimer = null;
function onEditInput() {
  if (!R.editing) return;
  R.dirty = true;
  RD.saveEdits.hidden = false;
  clearTimeout(editTimer);
  editTimer = setTimeout(updateParts, 300);
}
function editedText() {
  return String(RD.body.innerText || '').replace(/\r/g, '').replace(/\n{3,}/g, '\n\n').trim();
}
async function saveEdits() {
  const text = editedText();
  try {
    await withBusy(RD.saveEdits, 'Saving…', () => readerPost('save-text', { text }));
  } catch (e) { toastError(e); return; }
  R.cache.txt = text;
  R.dirty = false;
  RD.saveEdits.hidden = true;
  toast('Saved your changes to the text file.', { tone: 'ok' });
}
function confirmDiscard() {
  return confirmDialog({
    title: 'Discard your edits?', body: "Your changes to the text haven't been saved.",
    confirm: 'Discard', cancel: 'Keep editing',
  }).then((yes) => {
    if (yes) {
      R.dirty = false;
      RD.saveEdits.hidden = true;
      stopEditing();
    }
    return yes;
  });
}
async function onViewChange(v) {
  if (R.dirty) {
    const yes = await confirmDiscard();
    if (!yes) { RD.view.setValue('text'); return; }
  }
  stopEditing();
  R.view = v;
  lastView = v;
  renderView();
}
