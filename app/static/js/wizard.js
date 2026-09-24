// wizard.js: first-run setup (W-01, W-02, W-03).
// A native modal <dialog> with two screens. Screen 1 asks one question (where
// files go) and offers the GPU download on NVIDIA machines that need it.
// Screen 2 ends with a real first action. Errors are shown inline, never as a
// toast: toasts sit under the modal dialog (UI-3). There is no way to reopen
// it once setup is complete (W-03); every choice here lives in Settings.
import { el, bdi, on, post, STATE, loadConfig, installPack, openUrl, pickFolder, setFieldError, num } from './core.js';
import { icon, button, linkButton, callout, openDialog, withBusy } from './ui.js';

/** 'C:\Users\sina\Videos\Media Toolkit' -> 'Videos › Media Toolkit' when it lives under home. */
export function friendlyPath(path, home) {
  const p = String(path || '');
  const h = String(home || '').replace(/[\\/]+$/, '');
  if (h && p.length > h.length + 1 && p.slice(0, h.length).toLowerCase() === h.toLowerCase() && /[\\/]/.test(p[h.length])) {
    return p.slice(h.length).split(/[\\/]+/).filter(Boolean).join(' › ');
  }
  return p;
}

/** A short friendly path kept on one line ('Videos › Media Toolkit'); long raw paths may wrap. */
const pathText = (text) => el(`bdi${text.length <= 40 ? '.nowrap' : ''}`, { dir: 'auto' }, text);

/**
 * A folder field that shows the friendly form ('Videos › Media Toolkit') while
 * it is not being edited and the real path while it is. title= holds the path.
 */
function folderField(id, labelText, initial, home) {
  let raw = initial || '';
  const input = el('input', {
    type: 'text', id, dir: 'auto', spellcheck: 'false', autocomplete: 'off', title: raw, value: friendlyPath(raw, home),
  });
  const sync = () => { input.value = friendlyPath(raw, home); input.title = raw; };
  input.addEventListener('focus', () => { input.value = raw; });
  input.addEventListener('blur', () => {
    const v = input.value.trim();
    if (v !== raw) { raw = v; setFieldError(input, ''); }
    sync();
  });
  const change = button('Change…', 'secondary', { size: 'sm' });
  change.addEventListener('click', async () => {
    let picked = '';
    try {
      picked = await withBusy(change, 'Opening…', () => pickFolder(raw || home));
    } catch (e) {
      setFieldError(input, e.text || e.message);
      return;
    }
    if (!picked) return;
    raw = picked;
    setFieldError(input, '');
    sync();
  });
  const row = el('div.wiz-path',
    el('label.label', { for: id }, labelText),
    el('div.field', el('div.path-row', input, change)));
  return {
    row, input,
    /** The path, including what is typed right now. */
    get value() { return (document.activeElement === input ? input.value : raw).trim(); },
    set value(v) { raw = v || ''; sync(); },
  };
}

/**
 * Opens the wizard. The shell calls this at once when GET /api/setup says
 * `needed` (G-09) and switches to the returned tab when it resolves.
 * @returns {Promise<{tab: 'download'|'transcript'}>}
 */
export function open(setup) {
  const s = setup?.suggestions || {};
  const home = s.home || '';
  const recommended = { download_dir: s.download_dir || '', transcript_dir: s.transcript_dir || '' };
  let screen = 1;
  let saved = false;
  let busy = false;
  const offs = [];

  /* ---------------------------------------------------------- screen 1 */
  const pitch = 'Download videos, record live streams, and turn any video into text you can paste into an AI chat.';

  const recInput = el('input', { type: 'radio', name: 'wizLoc', value: 'recommended', checked: true });
  const cusInput = el('input', { type: 'radio', name: 'wizLoc', value: 'custom' });
  // K14: a portable copy keeps its files beside itself, not in Videos and Documents.
  const recTitle = s.portable ? 'With this portable copy (recommended)' : 'Videos and Documents (recommended)';
  const recText = s.portable
    ? ['Downloads go to ', bdi(recommended.download_dir), ', transcripts to ', bdi(recommended.transcript_dir), '. Your files stay with this copy of the app.']
    : ['Downloads go to ', pathText(friendlyPath(recommended.download_dir, home)), ', transcripts to ',
      pathText(friendlyPath(recommended.transcript_dir, home)), '. Easy to find in File Explorer.'];

  const dlField = folderField('wizDl', 'Downloads', recommended.download_dir, home);
  const trField = folderField('wizTr', 'Transcripts', recommended.transcript_dir, home);
  const paths = el('div.wiz-paths', { hidden: true }, dlField.row, trField.row);

  const optRec = el('div.opt-card',
    el('label.opt-main', recInput,
      el('span.opt-body', el('span.opt-title', recTitle), el('span.opt-text', recText))));
  const optCus = el('div.opt-card',
    el('label.opt-main', cusInput,
      el('span.opt-body', el('span.opt-title', 'Somewhere else…'), el('span.opt-text', 'Pick your own folders.'))),
    paths);
  const choices = el('fieldset.wiz-choices',
    el('legend.wiz-q', 'Where should files go?'), optRec, optCus);
  choices.addEventListener('change', () => {
    paths.hidden = !cusInput.checked;
    if (recInput.checked) { setFieldError(dlField.input, ''); setFieldError(trField.input, ''); }
    measure();
  });

  // W-02: only on an NVIDIA machine where transcription can't use the card yet. Unticked.
  const gpuBox = el('input', { type: 'checkbox', id: 'wizGpu' });
  const gpuTitle = el('span.opt-title');
  const gpuText = el('span.opt-text');
  const gpuLicense = el('p.help.wiz-license');
  const gpuCard = el('div.opt-card.wiz-gpu', { hidden: true },
    el('label.opt-main', gpuBox, el('span.opt-body', gpuTitle, gpuText)),
    gpuLicense);

  const privacy = el('p.wiz-privacy', icon('info'), el('span', 'Everything runs on this PC. Nothing is uploaded.'));
  const errBox = el('div.wiz-error', { role: 'alert' });

  const s1 = el('section.wiz-screen', { dataset: { screen: '1' } },
    el('p.wiz-step', { 'aria-hidden': 'true' }, 'Step 1 of 2'),
    el('h2.dlg-title#wizTitle', 'Welcome to Media Toolkit'),
    el('p.wiz-pitch', pitch),
    choices, gpuCard, privacy, errBox);

  /* ---------------------------------------------------------- screen 2 */
  const tile = (id, iconName, title, sub, primary) => el(`button.wiz-tile${primary ? '.is-primary' : ''}`, { type: 'button', id },
    el('span.wiz-tile-icon', icon(iconName, { cls: 'lg' })),
    el('span.wiz-tile-body', el('span.wiz-tile-title', title), el('span.wiz-tile-sub', sub)),
    icon('chevron-right', { cls: 'wiz-tile-go' }));
  const tileTr = tile('wizGoTranscript', 'text', 'Get a transcript', 'Paste a video link and get the text.', true);
  const tileDl = tile('wizGoDownload', 'download', 'Download a video', 'Save a video or its audio to this PC.', false);
  const s2 = el('section.wiz-screen', { dataset: { screen: '2' }, hidden: true, inert: true },
    el('p.wiz-step', { 'aria-hidden': 'true' }, 'Step 2 of 2'),
    el('h2.dlg-title#wizTitle2', "You're ready"),
    el('p.wiz-pitch', 'What would you like to do first?'),
    el('div.wiz-tiles', tileTr, tileDl),
    el('p.help.wiz-note', 'Everything can be changed later in Settings.'));

  /* ---------------------------------------------------------- footer */
  const recBtn = button('Use recommended settings', 'quiet', { id: 'wizRecommended' });
  const backBtn = button('Back', 'quiet', { id: 'wizBack', icon: 'chevron-left' });
  const contBtn = button('Continue', 'primary', { id: 'wizContinue', type: 'submit', title: 'Continue (Enter)', cls: 'primary-min' });
  backBtn.hidden = true;
  const foot = el('div.dlg-foot', recBtn, backBtn, el('div.end', contBtn));

  const live = el('p.sr-only', { 'aria-live': 'polite' });
  const screensEl = el('div.wiz-screens', s1, s2);
  const form = el('form.wiz-form', { novalidate: true, autocomplete: 'off' }, screensEl, foot, live);

  const d = openDialog({
    cls: 'wizard', labelledBy: 'wizTitle', body: form, initialFocus: contBtn,
    // Esc is handled on keydown below; this covers any other close request.
    onCancel: (e) => {
      if (screen === 1 && e.cancelable) { e.preventDefault(); useRecommended(); }
    },
  });
  d.el.id = 'wizard';

  /* ---------------------------------------------------------- behaviour */
  /**
   * The body keeps the height of the plain first screen (recommended folders,
   * nothing ticked, no errors), so the footer does not move between screens.
   * Measured again whenever that plain state changes size (the GPU block
   * arriving); an opened 'Somewhere else…' only grows screen 1 itself.
   */
  let baseline = 0;
  function measure() {
    if (screen !== 1 || s1.hidden || !s2.hidden || !recInput.checked || !gpuLicense.hidden || errBox.childElementCount) return;
    if (s1.querySelector('.field-error')) return;
    const h = Math.ceil(screensEl.getBoundingClientRect().height);
    if (h > baseline) {
      baseline = h;
      screensEl.style.setProperty('--wiz-h', `${baseline}px`);
    }
  }

  function show(n) {
    screen = n;
    for (const [node, i] of [[s1, 1], [s2, 2]]) {
      node.classList.toggle('is-active', i === n);
      node.hidden = i !== n;
      node.inert = i !== n;
    }
    measure();
    recBtn.hidden = n !== 1;
    contBtn.hidden = n !== 1;
    backBtn.hidden = n !== 2;
    d.el.setAttribute('aria-labelledby', n === 1 ? 'wizTitle' : 'wizTitle2');
    live.textContent = n === 1 ? 'Step 1 of 2. Welcome to Media Toolkit' : "Step 2 of 2. You're ready";
    (n === 1 ? contBtn : tileTr).focus();
  }

  function gpuInfo() {
    const hw = STATE.hw;
    return {
      name: (hw ? hw.nvidia_name : s.nvidia_name) || '',
      ready: hw ? !!hw.gpu_ready : !!s.gpu_ready,
      size: hw?.gpu_pack_size_mb || STATE.packs?.gpu_pack_size_mb || s.gpu_pack_size_mb || 0,
    };
  }
  function renderGpu(fromEvent) {
    const g = gpuInfo();
    const eligible = !!g.name && !g.ready;
    if (eligible && gpuCard.hidden && fromEvent) {
      // Hardware answered after the dialog opened: slide in, no reserved space (W-02).
      gpuCard.classList.add('is-entering');
      setTimeout(() => gpuCard.classList.remove('is-entering'), 400);
    }
    gpuCard.hidden = !eligible;
    if (!eligible) gpuBox.checked = false;
    gpuTitle.replaceChildren('Speed up transcription with your ', bdi(g.name));
    gpuText.textContent = `${g.size ? `One-time ${num(g.size)} MB download` : 'One-time download'} that runs in the background. Videos without captions transcribe many times faster.`;
    const lic = STATE.packs?.gpu_pack_license;
    if (lic?.text) {
      gpuLicense.replaceChildren(lic.text, ' ',
        lic.url ? linkButton(lic.name || 'License terms', () => openUrl(lic.url).catch(() => {})) : '');
    }
    // NVIDIA's terms show as soon as the box is ticked, before anything downloads.
    gpuLicense.hidden = !(lic?.text && gpuBox.checked);
    measure();
  }

  function clearErrors() {
    errBox.replaceChildren();
    setFieldError(dlField.input, '');
    setFieldError(trField.input, '');
  }
  function selectCustom() {
    cusInput.checked = true;
    paths.hidden = false;
  }

  /** POST /api/setup; on success go to screen 2. Errors stay inline (W-01). */
  async function save(dirs, { gpu = false, btn, fromRecommended = false } = {}) {
    if (busy) return;
    busy = true;
    clearErrors();
    const body = { download_dir: dirs.download_dir, transcript_dir: dirs.transcript_dir, set_mtime: false };
    if (s.whisper_model) body.whisper_model = s.whisper_model;
    try {
      await withBusy(btn, 'Saving…', () => post('/api/setup', body));
      saved = true;
      // The GPU download runs in the background; the status chip shows its progress.
      if (gpu) installPack('gpu').catch(() => {});
      loadConfig().catch(() => {});
      show(2);
    } catch (e) {
      const field = e.field === 'download_dir' ? dlField : e.field === 'transcript_dir' ? trField : null;
      if (field) {
        if (fromRecommended) { dlField.value = dirs.download_dir; trField.value = dirs.transcript_dir; }
        selectCustom();
        setFieldError(field.input, e.message || e.text);
        field.input.focus();
      } else {
        errBox.replaceChildren(callout('err', { title: "Couldn't save these settings", text: e.text || e.message }));
        (btn || contBtn).focus();
      }
    } finally {
      busy = false;
    }
  }

  function useRecommended() {
    return save(recommended, { gpu: false, btn: recBtn, fromRecommended: true });
  }
  function onContinue() {
    if (screen !== 1) return;
    const gpu = !gpuCard.hidden && gpuBox.checked;
    if (recInput.checked) { save(recommended, { gpu, btn: contBtn, fromRecommended: true }); return; }
    const dirs = { download_dir: dlField.value, transcript_dir: trField.value };
    const missing = !dirs.download_dir ? dlField : !dirs.transcript_dir ? trField : null;
    if (missing) {
      clearErrors();
      setFieldError(missing.input, 'Choose a folder for both downloads and transcripts.');
      missing.input.focus();
      return;
    }
    save(dirs, { gpu, btn: contBtn });
  }

  form.addEventListener('submit', (e) => { e.preventDefault(); onContinue(); });
  recBtn.addEventListener('click', useRecommended);
  backBtn.addEventListener('click', () => show(1));
  // The tiles only act on screen 2, after the folders are saved.
  tileTr.addEventListener('click', () => { if (screen === 2 && saved) d.close({ tab: 'transcript' }); });
  tileDl.addEventListener('click', () => { if (screen === 2 && saved) d.close({ tab: 'download' }); });
  // Esc on screen 1 applies the recommended settings; on screen 2 it closes (W-01).
  // Handled on keydown so it works even before the page has had a click.
  d.el.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape' || e.isComposing) return;
    e.preventDefault();
    e.stopPropagation();
    if (screen === 1) { if (!busy) useRecommended(); } else d.close({ tab: 'download' });
  });

  gpuBox.addEventListener('change', () => renderGpu(false));
  offs.push(on('hardware', () => renderGpu(true)), on('packs', () => renderGpu(false)));
  renderGpu(false);
  show(1);

  return d.closed.then(async (v) => {
    offs.forEach((off) => off());
    if (!saved) {
      // Closed some other way before anything was saved: the recommended
      // settings apply, the same as Esc (never the GPU download).
      try {
        await post('/api/setup', { ...recommended, set_mtime: false, ...(s.whisper_model ? { whisper_model: s.whisper_model } : {}) });
        loadConfig().catch(() => {});
      } catch (_) { /* the wizard opens again next time */ }
    }
    return v || { tab: 'download' };
  });
}
