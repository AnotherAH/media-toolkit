'use strict';

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const api = async (url, opts) => {
  const r = await fetch(url, opts);
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  const type = r.headers.get('content-type') || '';
  return type.includes('json') ? r.json() : r.text();
};
const post = (url, body) => api(url, {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body || {}),
});

let HW = null, CAP = null, JOBS = [], activeTranscript = null, chunks = null, chunkIx = 0;

/* ------------------------------------------------------------------ toast */
let toastTimer;
function toast(msg, bad) {
  const el = $('#toast');
  el.textContent = msg;
  el.classList.toggle('bad', !!bad);
  el.classList.add('on');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('on'), bad ? 7000 : 2600);
}

/* -------------------------------------------------------------------- nav */
$$('nav button').forEach((b) => b.onclick = () => {
  $$('nav button').forEach((x) => x.classList.remove('on'));
  $$('.page').forEach((x) => x.classList.remove('on'));
  b.classList.add('on');
  $('#page-' + b.dataset.tab).classList.add('on');
});
const goTab = (t) => $(`nav button[data-tab="${t}"]`).click();
$('#hwBadge').onclick = () => goTab('set');
$('#siteLink').onclick = (e) => { e.preventDefault(); goTab('set'); $('#siteSearch').focus(); };

/* --------------------------------------------------------------- helpers */
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function bytes(n) {
  if (!n) return '';
  const u = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (n >= 1024 && i < 3) { n /= 1024; i++; }
  return n.toFixed(i ? 1 : 0) + ' ' + u[i];
}
function autoGrow(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 180) + 'px';
}
const num = (id) => { const v = parseFloat($(id).value); return isNaN(v) ? '' : v; };

/* native folder picker — works because the server is this same machine */
async function pickFolder(targetId, btn) {
  if (btn) { btn.disabled = true; btn.textContent = 'Opening…'; }
  try {
    const r = await post('/api/pick-folder', { path: $('#' + targetId).value });
    if (r.path) $('#' + targetId).value = r.path;
  } catch (e) { toast(e.message, true); }
  if (btn) { btn.disabled = false; btn.textContent = 'Browse'; }
}
document.addEventListener('click', (e) => {
  const b = e.target.closest('[data-pick]');
  if (b) { e.preventDefault(); pickFolder(b.dataset.pick, b); }
});

/* ================================================== FIRST-RUN WIZARD ==== */
const WIZ = [
  { title: 'Welcome to Media Toolkit', sub: 'A one-minute setup and you are done.' },
  { title: 'Where should files go?', sub: 'You can change this at any time.' },
  { title: 'Download defaults', sub: 'What you get when you paste a link and press Download.' },
  { title: 'Transcription', sub: 'Used only when a video has no captions of its own.' },
  { title: 'Signing in to sites (optional)', sub: 'Only needed for private or restricted content.' },
  { title: 'All set', sub: '' },
];
let wizStep = 0, wizDefaults = null;

function renderWiz() {
  $('#wizSteps').innerHTML = WIZ.map((_, i) =>
    `<i class="${i < wizStep ? 'done' : i === wizStep ? 'on' : ''}"></i>`).join('');
  $('#wizTitle').textContent = WIZ[wizStep].title;
  $('#wizSub').textContent = WIZ[wizStep].sub;
  $$('.wiz-step').forEach((s) => s.classList.toggle('on', +s.dataset.step === wizStep));
  $('#wizBack').style.visibility = wizStep === 0 ? 'hidden' : '';
  $('#wizSkip').style.display = wizStep >= WIZ.length - 1 ? 'none' : '';
  $('#wizNext').textContent = wizStep === WIZ.length - 1 ? 'Start using it'
    : wizStep === 0 ? 'Get started' : 'Continue';
}

function wizLocation(kind) {
  const s = wizDefaults.suggestions;
  $$('#wizard .opt-card input[name=loc]').forEach((i) => {
    i.checked = i.value === kind;
    i.closest('.opt-card').classList.toggle('on', i.checked);
  });
  if (kind === 'portable') {
    $('#wizDl').value = s.portable_download_dir;
    $('#wizTr').value = s.portable_transcript_dir;
  } else if (kind === 'home') {
    $('#wizDl').value = s.download_dir;
    $('#wizTr').value = s.transcript_dir;
  }
  $('#wizPaths').style.display = kind === 'custom' ? '' : '';
}
$$('#wizard .opt-card input[name=loc]').forEach((i) =>
  i.closest('.opt-card').onclick = () => wizLocation(i.value));

async function openWizard(force) {
  wizDefaults = await api('/api/setup');
  if (!wizDefaults.needed && !force) return false;
  const hw = wizDefaults.hardware;
  $('#wizHw').innerHTML = hw.cuda
    ? `Detected <b>${esc(hw.gpus[0].name)}</b> with ${(hw.vram_mb / 1024).toFixed(1)} GB of video memory. ${esc(hw.note)}`
    : `No NVIDIA GPU found, so Whisper will use your ${hw.cpu_threads} CPU threads. ${esc(hw.note)}`;
  modelOptions($('#wizModel'), wizDefaults.suggestions.whisper_model);
  wizLocation('portable');
  wizStep = 0;
  renderWiz();
  $('#wizard').hidden = false;
  return true;
}

$('#wizBack').onclick = () => { if (wizStep > 0) { wizStep--; renderWiz(); } };
$('#wizNext').onclick = async () => {
  if (wizStep === 1 && (!$('#wizDl').value.trim() || !$('#wizTr').value.trim()))
    return toast('Both folders need a path', true);
  if (wizStep < WIZ.length - 1) { wizStep++; renderWiz(); return; }
  await finishWizard();
};
$('#wizSkip').onclick = () => finishWizard();

async function finishWizard() {
  const patch = {
    download_dir: $('#wizDl').value.trim() || wizDefaults.suggestions.portable_download_dir,
    transcript_dir: $('#wizTr').value.trim() || wizDefaults.suggestions.portable_transcript_dir,
    whisper_model: $('#wizModel').value,
    prefer_native_subs: $('#wizPreferSubs').checked,
    embed_thumbnail: $('#wizThumb').checked,
    embed_metadata: $('#wizThumb').checked,
    sponsorblock: $('#wizSponsor').checked,
    set_mtime: $('#wizMtime').checked,
  };
  try {
    await post('/api/setup', patch);
  } catch (e) { return toast(e.message, true); }
  $('#wizard').hidden = true;
  $('#dlQuality').value = $('#wizQuality').value;
  $('#dlCodec').value = $('#wizCodec').value;
  await loadSettings();
  await loadHardware();
  toast('Ready. Paste a link to begin.');
}

$('#wizDetect').onclick = (e) => detectCookies(e.target, '#wizCookieOut');
$('#wizSignin').onclick = (e) => signinFlow(e.target, '#wizCookieOut');
$('#btnRerunSetup').onclick = () => openWizard(true);

/* ================================================== COOKIE HELPERS ====== */
function cookieRows(r) {
  const rows = r.tested.map((t) => `<div class="ck ${t.ok ? 'ok' : 'no'}">
      <b>${esc(t.label || t.browser)}</b>
      ${t.ok
        ? `<span>${t.count.toLocaleString()} cookies${t.domains.length ? ' · signed in to ' + esc(t.domains.slice(0, 5).join(', ')) : ''}</span>`
        : `<span>${esc(t.error)}</span>`}
    </div>`).join('');
  return rows + `<div class="note" style="margin-top:10px">${esc(r.advice)}</div>`;
}

async function detectCookies(btn, target) {
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = 'Checking…';
  try {
    const r = await post('/api/cookies/detect');
    $(target).innerHTML = cookieRows(r);
    if (r.best) {
      await post('/api/settings', { cookies_browser: r.best, cookies_file: '' });
      $('#setCookies').value = r.best;
      toast('Using cookies from ' + r.best);
    }
  } catch (e) { toast(e.message, true); }
  btn.textContent = old; btn.disabled = false;
}

async function signinFlow(btn, target) {
  try {
    const r = await post('/api/cookies/login', {});
    if (!r.ok) return toast(r.error, true);
    $(target).innerHTML = `<div class="note">${esc(r.message)}</div>`;
    $('#ckHarvest').style.display = '';
    toast(r.browser + ' is opening — sign in there, then press "I have signed in"');
  } catch (e) { toast(e.message, true); }
}

$('#ckDetect').onclick = (e) => detectCookies(e.target, '#ckOut');
$('#ckSignin').onclick = (e) => signinFlow(e.target, '#ckOut');
$('#ckHarvest').onclick = async (e) => {
  e.target.disabled = true;
  try {
    const r = await post('/api/cookies/harvest');
    if (!r.ok) { toast(r.error, true); }
    else {
      $('#ckOut').innerHTML = `<div class="ck ok"><b>Saved</b><span>${r.cookies.toLocaleString()} cookies captured${r.sites?.length ? ' · ' + esc(r.sites.join(', ')) : ''}</span></div>`;
      $('#setCookieFile').value = r.path;
      $('#setCookies').value = '';
      toast('Cookies saved — private content should work now');
    }
  } catch (err) { toast(err.message, true); }
  e.target.disabled = false;
};
$('#ckImport').onclick = async () => {
  const text = $('#ckPaste').value;
  if (!text.trim()) return toast('Paste something first', true);
  try {
    const r = await post('/api/cookies/import', { text });
    $('#setCookieFile').value = r.path;
    $('#ckPaste').value = '';
    toast(`Saved ${r.cookies} cookies${r.note ? ' — ' + r.note : ''}`);
  } catch (e) { toast(e.message, true); }
};
$('#ckClear').onclick = async () => {
  await post('/api/cookies/clear');
  $('#setCookieFile').value = ''; $('#setCookies').value = '';
  $('#ckOut').innerHTML = '';
  toast('Saved cookies removed');
};

/* ----------------------------------------------------------- URL preview */
function previewCard(info) {
  if (info.kind === 'playlist') {
    return `<div class="preview">
      ${info.thumbnail ? `<img src="${esc(info.thumbnail)}" alt="">` : ''}
      <div class="meta"><div class="t">${esc(info.title)}</div>
      <div class="m">${esc(info.uploader)}</div>
      <div class="chips"><span class="chip">Playlist</span><span class="chip">${info.count} items</span></div>
      <div class="m" style="margin-top:8px">Tick <b>Download whole playlist</b> to grab every item.</div>
      </div></div>`;
  }
  const chips = [];
  if (info.heights?.length) chips.push(`<span class="chip">up to ${info.heights[0]}p</span>`);
  if (info.extractor) chips.push(`<span class="chip">${esc(info.extractor)}</span>`);
  if (info.is_live) chips.push('<span class="chip">LIVE</span>');
  if (info.subtitles?.length) chips.push(`<span class="chip good">official captions (${info.subtitles.length})</span>`);
  else if (info.auto_captions?.length) chips.push('<span class="chip good">auto captions — instant transcript</span>');
  else chips.push('<span class="chip">no captions — Whisper will run</span>');
  return `<div class="preview">
    ${info.thumbnail ? `<img src="${esc(info.thumbnail)}" alt="">` : ''}
    <div class="meta"><div class="t">${esc(info.title)}</div>
    <div class="m">${esc(info.uploader)}${info.duration_string ? ' · ' + info.duration_string : ''}${info.upload_date ? ' · ' + info.upload_date : ''}</div>
    <div class="chips">${chips.join('')}</div></div></div>`;
}

function wireProbe(input, target) {
  let timer, last = '';
  input.addEventListener('input', () => {
    autoGrow(input);
    const url = input.value.trim().split('\n')[0];
    clearTimeout(timer);
    if (!/^https?:\/\//i.test(url)) { target.innerHTML = ''; last = ''; return; }
    if (url === last) return;
    timer = setTimeout(async () => {
      last = url;
      target.innerHTML = '<div class="m" style="color:var(--fg-3)">Reading link…</div>';
      try {
        target.innerHTML = previewCard(await api('/api/probe?url=' + encodeURIComponent(url)));
      } catch (e) {
        target.innerHTML = `<div class="note warn">${esc(e.message)}</div>`;
      }
    }, 550);
  });
}
wireProbe($('#dlUrl'), $('#dlPreview'));
wireProbe($('#trUrl'), $('#trPreview'));

/* format table */
$('#btnFormats').onclick = async (e) => {
  const url = $('#dlUrl').value.trim().split('\n')[0];
  if (!url) return toast('Paste a link first', true);
  e.target.disabled = true;
  $('#dlFormats').innerHTML = '<div class="m" style="color:var(--fg-3);margin-top:10px">Listing formats…</div>';
  try {
    const r = await api('/api/formats?url=' + encodeURIComponent(url));
    $('#dlFormats').innerHTML = `<div class="fmt-wrap"><table class="fmt">
      <thead><tr><th>ID</th><th>Ext</th><th>Resolution</th><th>FPS</th><th>Video</th><th>Audio</th><th>Bitrate</th><th>Size</th><th>Note</th></tr></thead>
      <tbody>${r.formats.map((f) => `<tr>
        <td class="id">${esc(f.format_id)}</td><td>${esc(f.ext)}</td><td>${esc(f.resolution)}</td>
        <td>${f.fps || ''}</td><td>${esc(f.vcodec)}</td><td>${esc(f.acodec)}</td>
        <td>${f.tbr ? f.tbr + 'k' : ''}</td><td>${bytes(f.filesize)}</td>
        <td>${esc([f.note, f.dynamic_range].filter(Boolean).join(' '))}</td></tr>`).join('')}</tbody></table></div>`;
  } catch (err) {
    $('#dlFormats').innerHTML = `<div class="note warn">${esc(err.message)}</div>`;
  }
  e.target.disabled = false;
};

/* ------------------------------------------------------------- downloads */
$('#dlMode').onchange = () => {
  const audio = $('#dlMode').value === 'audio';
  $('#wrapQuality').style.display = audio ? 'none' : '';
  $('#wrapContainer').style.display = audio ? 'none' : '';
  $('#wrapCodec').style.display = audio ? '' : 'none';
};

function downloadOptions() {
  return {
    mode: $('#dlMode').value,
    quality: $('#dlQuality').value,
    container: $('#dlContainer').value,
    audio_codec: $('#dlCodec').value,
    embed_thumbnail: $('#dlThumb').checked,
    embed_metadata: $('#dlMeta').checked,
    embed_chapters: $('#dlChapters').checked,
    sponsorblock: $('#dlSponsor').checked,
    subtitles: $('#dlSubs').value,
    subtitle_langs: $('#dlSubLangs').value,
    embed_subs: $('#dlEmbedSubs').checked,
    playlist: $('#dlPlaylist').checked,
    playlist_items: $('#dlItems').value.trim(),
    playlist_order: $('#dlOrder').value,
    max_downloads: num('#dlMax'),
    archive: $('#dlArchive').checked,
    stop_at_known: $('#dlStopKnown').checked,
    concat_playlist: $('#dlConcat').checked,
    split_chapters: $('#dlSplit').checked,
    remove_chapters: $('#dlRmChapters').value.trim(),
    section: $('#dlSection').value.trim(),
    // filters
    min_duration: num('#fMinDur'), max_duration: num('#fMaxDur'),
    min_views: num('#fMinViews'), title_contains: $('#fTitle').value.trim(),
    date_after: $('#fAfter').value.trim(), date_before: $('#fBefore').value.trim(),
    max_filesize: $('#fMaxSize').value.trim(), skip_live: $('#fSkipLive').checked,
    // encoding + extras
    recode_encoder: $('#dlEncoder').value,
    recode_quality: $('#dlRecodeQ').value,
    normalize_audio: $('#dlNormalize').checked,
    convert_thumbnails: $('#dlThumbFmt').value,
    live_from_start: $('#dlLiveStart').checked,
    wait_for_video: $('#dlWait').checked,
    write_info_json: $('#dlInfoJson').checked,
    write_description: $('#dlDesc').checked,
    write_comments: $('#dlComments').checked,
    max_comments: parseInt($('#dlMaxComments').value) || 200,
    write_thumbnail: $('#dlThumbFile').checked,
    write_link: $('#dlLink').checked,
  };
}

$('#dlGo').onclick = async () => {
  const url = $('#dlUrl').value.trim();
  if (!url) return toast('Paste a link first', true);
  $('#dlGo').disabled = true;
  try {
    const r = await post('/api/jobs', { url, kind: 'download', options: downloadOptions() });
    toast(`Queued ${r.jobs.length} download${r.jobs.length > 1 ? 's' : ''}`);
    $('#dlUrl').value = ''; $('#dlPreview').innerHTML = ''; $('#dlFormats').innerHTML = '';
    autoGrow($('#dlUrl'));
    goTab('q');
  } catch (e) { toast(e.message, true); }
  $('#dlGo').disabled = false;
};

/* ------------------------------------------------------------ transcript */
function transcriptOptions() {
  return {
    model: $('#trModel').value,
    language: $('#trLang').value,
    langs: $('#trLangs').value,
    force_whisper: $('#trForce').checked,
    translate: $('#trTranslate').checked,
    vad: $('#trVad').checked,
    keep_audio: $('#trKeepAudio').checked,
    beam: parseInt($('#trBeam').value) || 5,
    hotwords: $('#trHotwords').value.trim(),
    formats: ['txt', 'srt', 'md'],
  };
}

$('#trGo').onclick = async () => {
  const url = $('#trUrl').value.trim();
  if (!url) return toast('Paste a link first', true);
  $('#trGo').disabled = true;
  try {
    await post('/api/jobs', { url, kind: 'transcript', options: transcriptOptions() });
    toast('Working on the transcript…');
    $('#trUrl').value = ''; $('#trPreview').innerHTML = ''; autoGrow($('#trUrl'));
  } catch (e) { toast(e.message, true); }
  $('#trGo').disabled = false;
};

const drop = $('#drop'), fileInput = $('#file');
drop.onclick = () => fileInput.click();
drop.ondragover = (e) => { e.preventDefault(); drop.classList.add('over'); };
drop.ondragleave = () => drop.classList.remove('over');
drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove('over'); if (e.dataTransfer.files[0]) upload(e.dataTransfer.files[0]); };
fileInput.onchange = () => fileInput.files[0] && upload(fileInput.files[0]);

async function upload(file) {
  const fd = new FormData();
  fd.append('file', file);
  fd.append('options', JSON.stringify(transcriptOptions()));
  drop.textContent = `Uploading ${file.name}…`;
  try {
    await api('/api/transcribe-file', { method: 'POST', body: fd });
    toast('Transcribing ' + file.name);
  } catch (e) { toast(e.message, true); }
  drop.textContent = 'or drop a local video / audio file here';
  fileInput.value = '';
}

async function showTranscript(job) {
  activeTranscript = job;
  chunks = null; chunkIx = 0;
  $('#trChunkNav').style.display = 'none';
  $('#trResult').style.display = '';
  $('#trTitle').textContent = job.title;
  const r = job.result, s = r.stats || {}, d = r.detail || {};
  $('#trStats').innerHTML = [
    d.engine ? `<span><b>Source:</b> ${esc(d.engine)}</span>` : '',
    d.device && d.device !== '-' ? `<span><b>Ran on:</b> ${esc(d.device.toUpperCase())} ${esc(d.compute_type || '')}</span>` : '',
    d.realtime_factor ? `<span><b>Speed:</b> ${d.realtime_factor}x realtime</span>` : '',
    d.language ? `<span><b>Language:</b> ${esc(d.language)}</span>` : '',
    `<span><b>${(s.words || 0).toLocaleString()}</b> words</span>`,
    `<span><b>~${(s.est_tokens || 0).toLocaleString()}</b> tokens</span>`,
  ].filter(Boolean).join('');
  $$('#trFormats button').forEach((b) => b.classList.toggle('on', b.dataset.f === 'txt'));
  $('#tOut').value = r.text || '';
  goTab('tr');
  $('#trResult').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

$$('#trFormats button').forEach((b) => b.onclick = async () => {
  if (!activeTranscript) return;
  $$('#trFormats button').forEach((x) => x.classList.remove('on'));
  b.classList.add('on');
  chunks = null; $('#trChunkNav').style.display = 'none';
  try {
    $('#tOut').value = await api(`/api/jobs/${activeTranscript.id}/transcript?format=${b.dataset.f}`);
  } catch (e) { toast(e.message, true); }
});

async function copyText(text, label) {
  try { await navigator.clipboard.writeText(text); toast(label); }
  catch (_) { const t = $('#tOut'); t.select(); document.execCommand('copy'); toast(label); }
}
$('#trCopy').onclick = () => copyText($('#tOut').value, 'Copied');
$('#trCopyAI').onclick = () => {
  if (!activeTranscript) return;
  const m = activeTranscript.result.meta || {};
  const lines = [
    `Transcript of: ${m.title || activeTranscript.title}`,
    m.uploader ? `Channel: ${m.uploader}` : '',
    m.duration_string ? `Duration: ${m.duration_string}` : '',
    m.url ? `Source: ${m.url}` : '',
  ].filter(Boolean);
  copyText(lines.join('\n') + '\n\n---\n\n' + $('#tOut').value,
    'Copied with source details — paste into any chatbot');
};

$('#trChunk').onclick = async () => {
  if (!activeTranscript) return;
  const fmt = $('#trFormats button.on').dataset.f;
  const r = await api(`/api/jobs/${activeTranscript.id}/transcript?format=${fmt}&chunk_size=12000`);
  chunks = r.chunks; chunkIx = 0;
  if (chunks.length < 2) return toast('Short enough to paste in one go');
  $('#trChunkNav').style.display = '';
  renderChunk();
  toast(`Split into ${chunks.length} parts`);
};
function renderChunk() {
  $('#tOut').value = `[Part ${chunkIx + 1} of ${chunks.length}]\n\n` + chunks[chunkIx];
  $('#trChunkLbl').textContent = `${chunkIx + 1} / ${chunks.length}`;
}
$('#trPrev').onclick = () => { if (chunks && chunkIx > 0) { chunkIx--; renderChunk(); } };
$('#trNext').onclick = () => { if (chunks && chunkIx < chunks.length - 1) { chunkIx++; renderChunk(); } };
$('#trSave').onclick = () => reveal(activeTranscript?.result?.output_dir);

/* ============================================================ LIVE ====== */
function liveCard(info) {
  const m = info.meta || {};
  const chips = [];
  chips.push(info.is_live ? '<span class="chip good">LIVE NOW</span>'
    : `<span class="chip">not live${info.live_status ? ' (' + esc(info.live_status) + ')' : ''}</span>`);
  if (info.height) chips.push(`<span class="chip">${info.height}p</span>`);
  chips.push(`<span class="chip${info.has_video ? ' good' : ''}">${info.has_video ? 'video' : 'no video'}</span>`);
  chips.push(`<span class="chip${info.has_audio ? ' good' : ''}">${info.has_audio ? 'audio' : 'no audio'}</span>`);
  if (m.extractor) chips.push(`<span class="chip">${esc(m.extractor)}</span>`);
  return `<div class="preview">
    ${m.thumbnail ? `<img src="${esc(m.thumbnail)}" alt="">` : ''}
    <div class="meta"><div class="t">${esc(info.title)}</div>
    <div class="m">${esc(m.uploader || '')}</div>
    <div class="chips">${chips.join('')}</div></div></div>`;
}

let lvTimer, lvLast = '';
$('#lvUrl').addEventListener('input', () => {
  autoGrow($('#lvUrl'));
  const url = $('#lvUrl').value.trim().split('\n')[0];
  clearTimeout(lvTimer);
  if (!/^https?:\/\//i.test(url)) { $('#lvPreview').innerHTML = ''; lvLast = ''; return; }
  if (url === lvLast) return;
  lvTimer = setTimeout(async () => {
    lvLast = url;
    $('#lvPreview').innerHTML = '<div class="m" style="color:var(--fg-3)">Checking the stream…</div>';
    try {
      $('#lvPreview').innerHTML = liveCard(await api('/api/live-check?url=' + encodeURIComponent(url)));
    } catch (e) { $('#lvPreview').innerHTML = `<div class="note warn">${esc(e.message)}</div>`; }
  }, 650);
});

$('#lvGo').onclick = async () => {
  const url = $('#lvUrl').value.trim();
  if (!url) return toast('Paste a stream link first', true);
  $('#lvGo').disabled = true;
  try {
    await post('/api/jobs', {
      url, kind: 'live',
      options: {
        quality: $('#lvQuality').value,
        container: $('#lvContainer').value,
        max_minutes: num('#lvMax'),
        split_minutes: num('#lvSplit'),
        wait_for_live: $('#lvWait').checked,
        wait_minutes: num('#lvWaitMin') || 180,
        audio_only: $('#lvAudio').checked,
        allow_vod: $('#lvVod').checked,
      },
    });
    toast('Recording started — stop it any time from the Queue');
    $('#lvUrl').value = ''; $('#lvPreview').innerHTML = ''; autoGrow($('#lvUrl'));
    goTab('q');
  } catch (e) { toast(e.message, true); }
  $('#lvGo').disabled = false;
};

/* ------------------------------------------------------------------ jobs */
function jobCard(j) {
  const pct = Math.round((j.progress || 0) * 100);
  const files = (j.files || []).map((f) =>
    `<a class="btn sm ghost" href="/api/file?path=${encodeURIComponent(f.path)}" download>${esc(f.label || '')} ${esc(f.name.slice(-38))}${f.size ? ' · ' + bytes(f.size) : ''}</a>`).join('');
  const busy = j.status === 'running' || j.status === 'queued';
  const rec = j.kind === 'live' && j.status === 'running' && /Recording/.test(j.stage || '');
  const meta = [rec ? null : j.stage, j.speed, j.eta && 'ends in ' + j.eta].filter(Boolean).map((x) => `<span>${esc(x)}</span>`).join('')
    + (rec ? `<span class="rec">${esc(j.stage)}</span>` : '');
  return `<div class="job ${j.status}${j.kind === 'live' ? ' live' : ''}" data-id="${j.id}">
    <div class="head">
      ${j.thumbnail ? `<img class="th" src="${esc(j.thumbnail)}" alt="">` : ''}
      <div style="flex:1;min-width:0">
        <div class="title">${esc(j.title)}</div>
        <div class="stage">${meta}</div>
      </div>
      ${busy ? `<button class="btn sm danger" data-cancel="${j.id}">${j.kind === 'live' ? 'Stop recording' : 'Cancel'}</button>` : ''}
      ${j.kind === 'transcript' && j.status === 'done' ? `<button class="btn sm" data-open="${j.id}">View transcript</button>` : ''}
      ${j.kind === 'download' && j.status === 'done' && j.files?.length ? `<button class="btn sm ghost" data-reveal="${esc(j.files[0].path)}">Show in folder</button>` : ''}
    </div>
    ${busy || j.status === 'done' ? `<div class="bar"><i style="width:${pct}%"></i></div>` : ''}
    ${j.message ? `<div class="err">${esc(j.message)}</div>` : ''}
    ${files ? `<div class="files">${files}</div>` : ''}
  </div>`;
}

function renderJobs() {
  const list = $('#qList');
  if (!JOBS.length) {
    list.innerHTML = `<div class="empty">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
      <div>Nothing queued yet.</div></div>`;
  } else {
    list.innerHTML = JOBS.map(jobCard).join('');
  }
  const active = JOBS.filter((j) => j.status === 'running' || j.status === 'queued').length;
  const badge = $('#qCount');
  badge.style.display = active ? '' : 'none';
  badge.textContent = active;
  const recording = JOBS.filter((j) => j.kind === 'live' && j.status === 'running').length;
  const lb = $('#liveCount');
  if (lb) { lb.style.display = recording ? '' : 'none'; lb.textContent = recording; }
}

$('#qList').onclick = async (e) => {
  const c = e.target.closest('[data-cancel]');
  const o = e.target.closest('[data-open]');
  const r = e.target.closest('[data-reveal]');
  if (c) {
    const job = JOBS.find((x) => x.id === c.dataset.cancel);
    await post(`/api/jobs/${c.dataset.cancel}/cancel`);
    toast(job && job.kind === 'live' ? 'Stopping — finalising the recording…' : 'Cancelled');
  }
  if (o) { const j = JOBS.find((x) => x.id === o.dataset.open); if (j) showTranscript(j); }
  if (r) reveal(r.dataset.reveal);
};

async function reveal(path) {
  if (!path) return;
  try { await post('/api/reveal', { path }); } catch (e) { toast(e.message, true); }
}
$('#qClear').onclick = async () => { await post('/api/jobs/clear'); toast('Cleared'); };
$('#qOpenDl').onclick = () => reveal($('#setDl').value);
$('#qOpenTr').onclick = () => reveal($('#setTr').value);

const announced = new Set();
function onJobs(next) {
  for (const j of next) {
    const was = JOBS.find((x) => x.id === j.id);
    if (j.status === 'done' && was && was.status !== 'done' && !announced.has(j.id)) {
      announced.add(j.id);
      if (j.kind === 'transcript' && j.result) showTranscript(j);
      else toast('Finished: ' + j.title);
    }
    if (j.status === 'error' && (!was || was.status !== 'error')) toast('Failed: ' + j.title, true);
  }
  JOBS = next;
  renderJobs();
}

function connect() {
  const es = new EventSource('/api/events');
  es.onmessage = (e) => onJobs(JSON.parse(e.data).jobs);
  es.onerror = () => { es.close(); setTimeout(connect, 2500); };
}

/* ================================================= WHISPER MODELS ======= */
async function loadModels() {
  let r;
  try { r = await api('/api/models'); } catch (_) { return; }
  const el = $('#modelList');
  if (!r.models.length) {
    el.innerHTML = '<div class="ck"><b>None yet</b><span>The first transcription that needs Whisper will download one.</span></div>';
    return;
  }
  el.innerHTML = r.models.map((m) => `<div class="ck ${m.ok ? 'ok' : 'no'}">
      <b>${esc(m.name)}</b>
      <span>${m.ok ? `${m.size_mb.toLocaleString()} MB${m.aliases.length ? ' · also known as ' + esc(m.aliases.join(', ')) : ''}`
                   : esc(m.problem)}</span>
      <span class="spacer"></span>
      <button class="btn sm ghost" data-repair="${esc(m.name)}">${m.ok ? 'Re-download' : 'Repair'}</button>
    </div>`).join('');
}

$('#modelList').onclick = async (e) => {
  const b = e.target.closest('[data-repair]');
  if (!b) return;
  b.disabled = true;
  try {
    await post('/api/models/repair', { name: b.dataset.repair });
    toast(`${b.dataset.repair} removed — it will download again on next use`);
    await loadModels();
  } catch (err) { toast(err.message, true); }
};

/* =================================================== RUNTIME PACKS ====== */
let packTimer = null;

async function loadPacks() {
  let st;
  try { st = await api('/api/packs'); } catch (_) { return; }
  const card = $('#packCard');
  if (!st.gpu_name && st.ffmpeg_installed) { card.style.display = 'none'; return; }
  card.style.display = '';

  const body = [];
  if (st.gpu_pack_installed) {
    body.push(`<div class="ck ok"><b>Enabled</b><span>${esc(st.gpu_name)} is being used for transcription.</span></div>`);
  } else if (st.gpu_name) {
    body.push(`<div class="ck no"><b>Available</b><span>${esc(st.gpu_name)} detected. A one-time ${st.gpu_pack_size_mb} MB download turns on GPU transcription — until then Whisper runs on the CPU, which is much slower.</span></div>`);
  }
  if (!st.ffmpeg_installed) {
    body.push('<div class="ck no"><b>ffmpeg</b><span>Missing — most downloads will fail without it.</span></div>');
  }
  $('#packBody').innerHTML = body.join('');
  $('#packInstall').style.display = st.gpu_pack_installed ? 'none' : '';
  $('#packRemove').style.display = st.gpu_pack_installed ? '' : 'none';
  $('#packInstall').textContent = st.ffmpeg_installed
    ? `Download GPU acceleration (${st.gpu_pack_size_mb} MB)` : 'Download ffmpeg';

  const pr = st.progress || {};
  const bar = $('#packBar');
  if (pr.busy) {
    bar.style.display = '';
    bar.querySelector('i').style.width = (pr.percent || 0) + '%';
    $('#packBody').innerHTML += `<div class="note" style="margin-top:10px">${esc(pr.message || 'Working…')}</div>`;
    $('#packInstall').disabled = true;
    if (!packTimer) packTimer = setInterval(loadPacks, 1200);
  } else {
    bar.style.display = 'none';
    $('#packInstall').disabled = false;
    if (packTimer) { clearInterval(packTimer); packTimer = null; }
    if (pr.error) $('#packBody').innerHTML += `<div class="note warn" style="margin-top:10px">${esc(pr.error)}</div>`;
  }
}

$('#packInstall').onclick = async () => {
  const st = await api('/api/packs');
  const url = st.ffmpeg_installed ? '/api/packs/gpu' : '/api/packs/ffmpeg';
  $('#packInstall').disabled = true;
  loadPacks();
  try {
    const r = await post(url);
    toast(r.ok ? 'Installed — GPU acceleration is on' : r.error, !r.ok);
  } catch (e) { toast(e.message, true); }
  await loadPacks();
  await loadHardware();
};

$('#packRemove').onclick = async () => {
  await post('/api/packs/gpu/remove');
  await loadPacks();
  toast('GPU libraries removed');
};

/* -------------------------------------------------------------- settings */
function modelOptions(sel, current) {
  sel.innerHTML = HW.models.map((m) =>
    `<option value="${m.id}" ${m.fits ? '' : 'disabled'}>${m.label} — ${m.note}${m.fits ? '' : ' (needs more VRAM)'}</option>`
  ).join('');
  sel.value = current;
  if (!sel.value) sel.value = HW.recommended_model;
}

async function loadHardware() {
  HW = await api('/api/hardware');
  CAP = await api('/api/capabilities');
  const gpu = HW.gpus?.[0];
  $('#hwText').textContent = HW.cuda && gpu ? `${gpu.name} · GPU` : 'CPU mode';
  $('#hwBadge').querySelector('.dot').classList.toggle('cpu', !HW.cuda);

  const rows = [
    ['Status', HW.note],
    ['GPU', gpu ? `${gpu.name} · ${(gpu.vram_mb / 1024).toFixed(1)} GB` : 'none detected'],
    ['CPU threads', HW.cpu_threads],
    ['Recommended model', HW.recommended_model],
    ['Verified backend', Object.entries(HW.cached || {}).map(([m, v]) => `${m}: ${v.device}/${v.compute_type}`).join(', ') || 'not measured yet'],
    ['Hardware video encoders', CAP.hardware?.length ? CAP.hardware.join(', ') : 'none — CPU encoding only'],
    ['Browser impersonation', CAP.impersonate_available ? `${CAP.impersonate.length} targets available` : 'not installed'],
    ['JavaScript runtime', CAP.js_runtime ? `${CAP.js_runtime} — full YouTube support` : 'missing — install Node.js for best YouTube results'],
    ['Browsers found', CAP.browsers?.length ? CAP.browsers.map((b) => b.label).join(', ') : 'none'],
    ['ffmpeg', HW.ffmpeg ? 'bundled' : 'MISSING — re-run setup'],
    ['yt-dlp', HW.yt_dlp],
    ['Python', HW.python],
  ];
  $('#hwInfo').innerHTML = rows.map(([k, v]) => `<div class="hw-line"><span>${esc(k)}</span><span>${esc(v)}</span></div>`).join('');

  // encoders + impersonation dropdowns
  $('#dlEncoder').innerHTML = '<option value="">No — keep the original (fastest)</option>' +
    (CAP.encoders || []).map((e) => `<option value="${e.id}">${esc(e.label)} — ${esc(e.note)}</option>`).join('');
  $('#setImpersonate').innerHTML = '<option value="">Off</option>' +
    (CAP.impersonate || []).map((t) => `<option value="${esc(t)}">${esc(t)}</option>`).join('');
  if (!CAP.aria2c) $('#setDownloader').querySelector('option[value=aria2c]').disabled = true;
}

async function loadSettings() {
  const { config: c } = await api('/api/settings');
  const set = (id, v) => { const el = $(id); if (el) el.value = v ?? ''; };
  const chk = (id, v) => { const el = $(id); if (el) el.checked = !!v; };
  set('#setDl', c.download_dir); set('#setTr', c.transcript_dir);
  set('#setCookies', c.cookies_browser); set('#setCookieFile', c.cookies_file);
  set('#setCookieProfile', c.cookies_profile);
  set('#setDevice', c.whisper_device); set('#setCompute', c.whisper_compute);
  set('#setFrag', c.concurrent_fragments); set('#setRate', c.rate_limit);
  set('#setProxy', c.proxy); set('#setImpersonate', c.impersonate);
  set('#setGeo', c.geo_bypass_country); set('#setSleep', c.sleep_requests);
  set('#setDownloader', c.external_downloader); set('#setTmpl', c.output_template);
  set('#trLangs', c.subtitle_langs);
  chk('#setPreferSubs', c.prefer_native_subs); chk('#setRestrict', c.restrict_filenames);
  chk('#setMtime', c.set_mtime); chk('#setTemp', c.use_temp_dir); chk('#setIpv4', c.force_ipv4);
  chk('#dlSponsor', c.sponsorblock); chk('#dlThumb', c.embed_thumbnail); chk('#dlMeta', c.embed_metadata);
  modelOptions($('#setModel'), c.whisper_model);
  modelOptions($('#trModel'), c.whisper_model);
}

$('#setSave').onclick = async () => {
  await post('/api/settings', {
    download_dir: $('#setDl').value, transcript_dir: $('#setTr').value,
    cookies_browser: $('#setCookies').value, cookies_file: $('#setCookieFile').value,
    cookies_profile: $('#setCookieProfile').value,
    whisper_model: $('#setModel').value, whisper_device: $('#setDevice').value,
    whisper_compute: $('#setCompute').value,
    concurrent_fragments: parseInt($('#setFrag').value) || 8,
    rate_limit: $('#setRate').value, proxy: $('#setProxy').value,
    impersonate: $('#setImpersonate').value,
    geo_bypass_country: $('#setGeo').value,
    sleep_requests: parseFloat($('#setSleep').value) || 0,
    external_downloader: $('#setDownloader').value,
    output_template: $('#setTmpl').value,
    prefer_native_subs: $('#setPreferSubs').checked,
    restrict_filenames: $('#setRestrict').checked,
    set_mtime: $('#setMtime').checked,
    use_temp_dir: $('#setTemp').checked,
    force_ipv4: $('#setIpv4').checked,
    subtitle_langs: $('#trLangs').value,
  });
  await loadHardware();
  toast('Settings saved');
};

$('#btnRedetect').onclick = async () => {
  await post('/api/settings', { whisper_device: $('#setDevice').value });
  await loadHardware();
  toast('Backend will be re-verified on the next transcription');
};

$('#btnUpdate').onclick = async (e) => {
  e.target.disabled = true;
  const old = e.target.textContent;
  e.target.textContent = 'Updating…';
  try {
    const r = await post('/api/update-ytdlp');
    toast(r.ok ? 'yt-dlp updated — restart the app to use it' : 'Update failed', !r.ok);
  } catch (err) { toast(err.message, true); }
  e.target.textContent = old; e.target.disabled = false;
};

let siteTimer;
async function searchSites(q) {
  const r = await api('/api/sites?q=' + encodeURIComponent(q || ''));
  $('#siteCount').textContent = r.total.toLocaleString();
  $('#siteList').innerHTML = r.matches.map((m) => `<div>${esc(m)}</div>`).join('')
    || '<div style="color:var(--fg-3)">No match — try a shorter word.</div>';
}
$('#siteSearch').oninput = (e) => { clearTimeout(siteTimer); siteTimer = setTimeout(() => searchSites(e.target.value), 220); };

/* Liveness: the launcher quits when these stop arriving, which is what makes
   closing the window behave like quitting a desktop app. */
function beat() { fetch('/api/heartbeat', { method: 'POST', keepalive: true }).catch(() => {}); }
beat();
setInterval(beat, 3000);
window.addEventListener('pagehide', () => {
  try { navigator.sendBeacon('/api/goodbye'); } catch (_) {}
});

/* ------------------------------------------------------------------ init */
(async function init() {
  try {
    await loadHardware();
    await loadSettings();
    await searchSites('');
    await loadPacks();
    await loadModels();
    await openWizard(false);
  } catch (e) { toast('Startup problem: ' + e.message, true); }
  renderJobs();
  connect();
  $$('textarea[rows="1"]').forEach(autoGrow);
})();
