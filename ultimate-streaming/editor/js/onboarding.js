/* The setup assistant for people who already have OBS and/or another Stream Deck app:
   detect → back up → import / adopt → free the deck. Plus Settings' "Existing setup" and
   "External backups" cards. API: core/webapi/setup_api.py (/api/setup/...).

   Everything read from OBS or another app (profile, scene and page names, labels, imported
   actions, warnings) is shown through text nodes only. The browser never sends a path: local
   sources are detect's opaque source_ids, a chosen file goes through /upload. Imported actions
   that could do harm arrive switched off (needs_review) and are enabled one key at a time in the
   Deck editor (app.js); there is no bulk enable here. Styles: css/onboarding.css. */
import { api, apiUpload } from './api.js';
import { h, icon, toast, openModal, closeModal, confirmDialog, modalOpen, asyncButton, empty } from './ui.js';
import { planReview, planChangeCount, itemName } from './backups.js';
import { go } from './main.js';
import { showKey } from './app.js';

const DONE_KEY = 'deck_onboarding_done';
const STATE_DIR = '~/.local/state/ultimate-streaming';
const UPLOAD_MAX = 50 * 1024 * 1024;
const APP_NAMES = { streamcontroller: 'StreamController', streamdeck_ui: 'streamdeck-ui', boatswain: 'Boatswain', elgato: 'Elgato profile', obs: 'OBS Studio' };
const STEPS = ['We found', 'Back up', 'Bring it over', 'Deck access'];
const UNSAVED = 'You have unsaved edits in the Deck editor. Importing writes config.yaml, so saving them afterwards will ask you to reload first.';

const plural = (n, word) => `${Number(n || 0).toLocaleString()} ${word}${n === 1 ? '' : 's'}`;
const appName = (kind) => APP_NAMES[kind] || String(kind);
const unsaved = () => { const b = document.getElementById('save'); return !!(b && !b.disabled); };

function fmtSize(n) {
  n = Number(n) || 0;
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 / 1024).toFixed(n < 10 * 1024 * 1024 ? 1 : 0)} MB`;
}

function when(ts) {
  if (typeof ts !== 'number') return 'an unknown time';
  return new Date(ts * 1000).toLocaleString(undefined, { month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit' });
}

function setDone() { try { localStorage.setItem(DONE_KEY, '1'); } catch (e) { /* private mode */ } }
function isDone() { try { return !!localStorage.getItem(DONE_KEY); } catch (e) { return false; } }

/** Worth offering: an OBS profile other than the default "Untitled", or any deck app / export. */
function worthOffering(scan) {
  const obs = (scan && scan.obs) || {};
  return (obs.profiles || []).some((p) => p.name !== 'Untitled') || ((scan && scan.apps) || []).length > 0;
}

/** First run (called from backups.maybeWelcome on a fresh config): open the assistant when
    something was found. Returns true when it opened. */
export async function maybeOnboard() {
  if (isDone() || modalOpen()) return false;
  let scan;
  try { scan = await api('/api/setup/detect'); } catch (e) { return false; }
  if (!worthOffering(scan) || modalOpen()) return false;
  openOnboarding({ scan, first: true });
  return true;
}

function skeleton(n) {
  return h('div', { class: 'ob-skel', 'aria-busy': 'true', 'aria-label': 'Loading' },
    Array.from({ length: n }, (_, i) => h('span', { class: 'skeleton line', style: { width: `${90 - (i % 3) * 17}%` } })));
}

function errorLine(text, retry) {
  return h('div', { class: 'ob-error', role: 'alert' }, icon('warn'), h('span', { class: 'grow', text }),
    retry && h('button', { class: 'small', onclick: retry }, icon('refresh'), 'Try again'));
}

function okBanner(...kids) {
  return h('div', { class: 'banner ok ob-ok', role: 'status' }, h('div', { class: 'row nowrap ob-banner-row' }, icon('check'), h('div', { class: 'grow' }, ...kids)));
}

function warnBanner(...kids) {
  return h('div', { class: 'banner warn', role: 'status' }, h('div', { class: 'row nowrap ob-banner-row' }, icon('warn'), h('div', { class: 'grow' }, ...kids)));
}

/* ------------------------------------------------------------------ the assistant */

/** openOnboarding({scan?, first?, step?}) */
export function openOnboarding(opts = {}) {
  const W = {
    step: opts.step || 1, first: !!opts.first, scan: opts.scan || null, scanErr: '',
    include: null, backup: null, backupErr: '',
    obs: null, obsErr: '', obsLoading: false, obsMode: 'keep', pickProfile: '', pickCollection: '', adopted: null, adoptErr: '',
    addonScene: {}, addonRes: {},
    imports: {}, open: null, uploadErr: '',
    running: null, runningErr: '', stopped: {},
  };
  const stepper = h('ol', { class: 'bk-stepper ob-stepper', 'aria-label': 'Setup steps' });
  const panel = h('div', { class: 'ob-panel' });
  const body = h('div', { class: 'bk-wizard ob-wizard' }, stepper, panel);
  const show = () => { if (!body.isConnected) openModal('Bring your existing setup', body, { wide: true }); };

  const render = () => {
    stepper.replaceChildren(...STEPS.map((t, i) => {
      const n = i + 1;
      const state = n < W.step ? 'done' : n === W.step ? 'current' : 'upcoming';
      return h('li', { class: state, 'aria-current': n === W.step ? 'step' : null },
        h('span', { class: 'bk-mark', 'aria-hidden': 'true' }, state === 'done' ? icon('check') : String(n)),
        h('span', { class: 'bk-step-name', text: t }));
    }));
    panel.replaceChildren(...[step1, step2, step3, step4][W.step - 1]());
    panel.scrollTop = 0;
  };
  const goStep = (n) => {
    W.step = n;
    if (n === 3 && !W.obs && !W.obsErr && hasObs()) loadObs();
    if (n === 4) loadRunning();
    render();
    const h2 = panel.querySelector('h3, button.primary');
    if (h2 && h2.focus && h2.tagName === 'BUTTON') h2.focus();
  };
  const foot = (...kids) => h('div', { class: 'row end bk-modal-foot ob-foot' }, ...kids.filter(Boolean));
  const finish = () => { setDone(); closeModal(); refreshExternal(); };

  const scanNow = async () => {
    W.scanErr = '';
    W.scan = null;
    render();
    try { W.scan = await api('/api/setup/detect'); } catch (e) { W.scanErr = e.message; }
    W.include = null;
    render();
  };
  const hasObs = () => !!(W.scan && W.scan.obs && ((W.scan.obs.roots || []).length || (W.scan.obs.profiles || []).length));
  const deckApps = () => ((W.scan && W.scan.apps) || []);

  /* ---- step 1: what was found */
  const step1 = () => {
    if (W.scanErr) return [errorLine('Couldn’t look for OBS and deck apps: ' + W.scanErr, scanNow), foot(h('button', { class: 'ghost', text: 'Close', onclick: closeModal }))];
    if (!W.scan) return [h('p', { class: 'hint', text: 'Looking for OBS and other Stream Deck apps…' }), skeleton(5)];
    const obs = W.scan.obs || {};
    const apps = deckApps();
    const out = [h('p', { class: 'ob-lead', text: 'Your current OBS and Stream Deck setup can come along. Nothing changes until you choose to.' })];

    // OBS
    const profiles = obs.profiles || [], collections = obs.collections || [];
    out.push(h('section', { class: 'ob-found', 'aria-labelledby': 'ob-f-obs' },
      h('div', { class: 'ob-found-head' }, h('span', { class: 'ob-found-icon', 'aria-hidden': 'true' }, icon('obs')),
        h('h3', { id: 'ob-f-obs', text: 'OBS Studio' }),
        obs.running ? h('span', { class: 'badge ok', text: 'Running' }) : null),
      profiles.length || collections.length
        ? h('div', { class: 'ob-cols' },
          h('div', {}, h('h4', { class: 'ob-sub', text: plural(profiles.length, 'profile') }),
            h('ul', { class: 'ob-list' }, profiles.map((p) => h('li', {},
              h('div', { class: 'ob-row' }, h('span', { class: 'ob-name', text: String(p.name) }),
                p.name === obs.current_profile ? h('span', { class: 'badge accent', text: 'Current' }) : null,
                p.has_stream_key ? h('span', { class: 'badge ok', text: 'Stream key set ✓', title: 'The key itself is never shown' }) : null),
              h('div', { class: 'ob-meta', text: [p.service, p.encoder].filter(Boolean).map(String).join(' · ') || 'No streaming service set' }))))),
          h('div', {}, h('h4', { class: 'ob-sub', text: plural(collections.length, 'scene collection') }),
            h('ul', { class: 'ob-list' }, collections.map((c) => {
              const scenes = (c.scenes || []).map(String);
              return h('li', {},
                h('div', { class: 'ob-row' }, h('span', { class: 'ob-name', text: String(c.name) }),
                  c.name === obs.current_collection ? h('span', { class: 'badge accent', text: 'Current' }) : null),
                h('div', { class: 'ob-meta', text: `${plural(scenes.length, 'scene')}${scenes.length ? ': ' + scenes.slice(0, 6).join(', ') + (scenes.length > 6 ? ', …' : '') : ''}` }));
            }))))
        : h('p', { class: 'hint', text: 'No OBS settings were found on this computer.' })));

    // deck apps
    out.push(h('section', { class: 'ob-found', 'aria-labelledby': 'ob-f-apps' },
      h('div', { class: 'ob-found-head' }, h('span', { class: 'ob-found-icon', 'aria-hidden': 'true' }, icon('deck')),
        h('h3', { id: 'ob-f-apps', text: 'Stream Deck apps' })),
      apps.length
        ? h('ul', { class: 'ob-list' }, apps.map((a) => h('li', {},
          h('div', { class: 'ob-row' }, h('span', { class: 'ob-name', text: String(a.label || appName(a.kind)) }),
            a.label !== appName(a.kind) ? h('span', { class: 'badge', text: appName(a.kind) }) : null,
            a.running ? h('span', { class: 'badge warn', text: 'Running', title: 'It holds the deck while it runs' }) : null),
          h('div', { class: 'ob-meta', text: a.kind === 'elgato' ? 'A profile file you exported from the Elgato app'
            : [plural(a.decks, 'deck'), plural(a.pages, 'page'), plural(a.keys, 'key')].join(' · ') }),
          (a.warnings || []).filter((w) => w !== 'contents read when you preview').map((w) => h('div', { class: 'ob-warn', text: String(w) })))))
        : h('p', { class: 'hint', text: 'No other Stream Deck app was found. You can still import an Elgato profile file later in this assistant.' })));

    for (const w of [...(W.scan.warnings || []), ...(obs.warnings || [])]) out.push(h('p', { class: 'note warn', text: String(w) }));
    out.push(foot(
      h('button', { class: 'ghost', text: W.first ? 'Not now' : 'Close', onclick: () => { if (W.first) setDone(); closeModal(); } }),
      h('button', { class: 'ghost', onclick: scanNow }, icon('refresh'), 'Scan again'),
      h('button', { class: 'primary', onclick: () => goStep(2) }, 'Next: back up', icon('arrowRight'))));
    return out;
  };

  /* ---- step 2: back up */
  const backupKinds = () => {
    const kinds = [];
    if (hasObs()) kinds.push('obs');
    for (const a of deckApps()) if (a.kind !== 'elgato' && !kinds.includes(a.kind)) kinds.push(a.kind);
    return kinds;
  };
  const step2 = () => {
    const kinds = backupKinds();
    if (!W.include) W.include = new Set(kinds);
    const hasElgato = deckApps().some((a) => a.kind === 'elgato');
    const out = [h('p', { class: 'ob-lead', text: 'Before anything changes, keep a copy of your current settings. It stays private on this computer: OBS backups include your stream key, so they are never shown or uploaded.' })];
    if (!kinds.length) out.push(h('p', { class: 'hint', text: 'Nothing to back up: no OBS settings or deck apps were found.' }));
    else {
      out.push(h('fieldset', { class: 'ob-checks', disabled: !!W.backup },
        h('legend', { class: 'ob-sub', text: 'Back up' }),
        kinds.map((k) => {
          const cb = h('input', { type: 'checkbox', checked: W.include.has(k), onchange: (e) => { if (e.target.checked) W.include.add(k); else W.include.delete(k); render(); } });
          const what = k === 'obs' ? 'Profiles (with the stream key), scene collections and the WebSocket settings' : 'Its pages, keys and images';
          return h('label', { class: 'ob-check' }, cb, h('span', {}, h('span', { class: 'ob-name', text: appName(k) }), h('span', { class: 'ob-meta', text: what })));
        })));
      if (hasElgato) out.push(h('p', { class: 'hint', text: 'Elgato profile files aren’t copied: they are files you downloaded, and they stay where they are.' }));
    }
    if (W.backup) {
      // one archive per OBS settings folder (native, Flatpak); the deck apps are in the first
      const archives = Array.isArray(W.backup.archives) && W.backup.archives.length ? W.backup.archives : [W.backup];
      out.push(okBanner(
        h('strong', { text: archives.length > 1 ? `Backed up in ${archives.length} archives. ` : 'Backed up. ' }), `Saved privately in ${STATE_DIR}.`,
        h('ul', { class: 'ob-archives-made' }, archives.map((b) => h('li', {},
          h('div', { class: 'ob-row' }, h('span', { class: 'ob-name', text: String(b.label || 'Backup') }),
            b.pinned ? h('span', { class: 'badge accent', text: 'Pinned', title: 'Kept until you delete it' }) : null),
          h('div', { class: 'ob-meta', text: [(b.items || []).map((i) => `${String(i.label || appName(i.kind))} (${plural(i.files, 'file')})`).join(', '), fmtSize(b.size)].filter(Boolean).join(' · ') }),
          ...(b.items || []).flatMap((i) => (i.notes || []).map((n) => h('div', { class: 'ob-warn', text: String(n) })))))),
        archives.some((b) => b.pinned) ? h('div', { class: 'ob-meta', text: `${archives.length > 1 ? 'These first backups are' : 'This first backup is'} pinned: kept until you delete ${archives.length > 1 ? 'them' : 'it'}.` }) : null));
    }
    out.push(h('p', { class: 'err', role: 'alert', text: W.backupErr }));
    const backupBtn = asyncButton({ class: 'primary', disabled: !W.include.size }, async () => {
      W.backupErr = '';
      try {
        W.backup = await api('/api/setup/backup', { include: [...W.include] });
        refreshExternal();
      } catch (e) { W.backupErr = 'Not backed up: ' + e.message; }
      render();
    }, icon('lock'), 'Back up');
    out.push(foot(
      h('button', { class: 'ghost', text: 'Back', onclick: () => goStep(1) }),
      !W.backup && kinds.length ? h('button', { class: 'ghost', text: 'Skip', onclick: () => goStep(3) }) : null,
      W.backup || !kinds.length ? h('button', { class: 'primary', onclick: () => goStep(3) }, 'Next', icon('arrowRight')) : backupBtn));
    return out;
  };

  /* ---- step 3: OBS + imports */
  const loadObs = async () => {
    W.obsLoading = true; W.obsErr = '';
    render();
    try { W.obs = await api('/api/setup/obs'); } catch (e) { W.obsErr = e.status === 503 ? 'unreachable' : e.message; }
    W.obsLoading = false;
    if (W.step === 3) render();
  };

  const obsCard = () => {
    const obs = W.scan.obs || {};
    const head = h('div', { class: 'ob-found-head' }, h('span', { class: 'ob-found-icon', 'aria-hidden': 'true' }, icon('obs')), h('h3', { text: 'OBS' }));
    if (W.obsLoading) return h('section', { class: 'ob-found' }, head, skeleton(3));
    const cur = W.obs || { profile: obs.current_profile, collection: obs.current_collection };
    const out = [head];
    if (W.obsErr === 'unreachable') {
      out.push(warnBanner(h('span', { text: 'OBS isn’t running (or its WebSocket server is off; that’s OBS’s built-in remote control, which this panel uses to start your stream and switch scenes), so nothing can be switched or added yet. The deck will use your current profile and scene collection.' }),
        h('div', { class: 'mt-2' }, h('button', { class: 'small', onclick: loadObs }, icon('refresh'), 'Check again'))));
    } else if (W.obsErr) out.push(errorLine('Couldn’t ask OBS: ' + W.obsErr, loadObs));

    const radio = (value, title, sub) => h('label', { class: 'ob-radio' + (W.obsMode === value ? ' on' : '') },
      h('input', { type: 'radio', name: 'ob-obs-mode', value, checked: W.obsMode === value, onchange: () => { W.obsMode = value; W.adoptErr = ''; render(); } }),
      h('span', {}, h('span', { class: 'ob-name', text: title }), h('span', { class: 'ob-meta', text: sub })));
    const curText = cur.profile || cur.collection ? `Profile “${String(cur.profile || '?')}”, scene collection “${String(cur.collection || '?')}”.` : 'Whatever OBS has open.';
    const pickers = [];
    if (W.obsMode === 'other') {
      const sel = (label, list, value, set) => {
        const s = h('select', {}, h('option', { value: '', text: 'Keep the current one' }), list.map((n) => h('option', { value: String(n), text: String(n), selected: value === n })));
        s.addEventListener('change', () => set(s.value));
        const id = 'ob-' + label.replace(/\W+/g, '-').toLowerCase();
        s.id = id;
        return h('div', { class: 'field' }, h('label', { for: id, text: label }), s);
      };
      pickers.push(h('div', { class: 'grid cols-2 fields ob-pickers' },
        sel('Profile', (obs.profiles || []).map((p) => p.name), W.pickProfile, (v) => { W.pickProfile = v; }),
        sel('Scene collection', (obs.collections || []).map((c) => c.name), W.pickCollection, (v) => { W.pickCollection = v; })));
    }
    out.push(h('fieldset', { class: 'ob-radios' }, h('legend', { class: 'ob-sub', text: 'What the deck controls' }),
      radio('keep', 'Keep using my current setup (recommended)', curText),
      radio('other', 'Use another profile or scene collection', 'OBS switches to it. Nothing is created or deleted.')), ...pickers);
    if (W.adopted) {
      out.push(okBanner(h('span', { text: `The deck controls profile “${String(W.adopted.profile)}” and scene collection “${String(W.adopted.collection)}”.` }),
        (W.adopted.switched || []).length ? h('div', { class: 'ob-meta', text: 'OBS switched; a backup of its settings was made first.' }) : null));
    }
    out.push(h('p', { class: 'err', role: 'alert', text: W.adoptErr }));
    out.push(h('div', { class: 'row' }, asyncButton({ class: 'small', disabled: !W.obs }, async () => {
      W.adoptErr = '';
      const body = W.obsMode === 'other' ? Object.fromEntries([['profile', W.pickProfile], ['collection', W.pickCollection]].filter(([, v]) => v)) : {};
      try {
        const r = await api('/api/setup/obs/adopt', body);
        if (r.ok === false) W.adoptErr = String(r.error || 'OBS said no');
        else { W.adopted = r; W.obs = { ...W.obs, profile: r.profile, collection: r.collection }; }
      } catch (e) { W.adoptErr = e.message; }
      render();
    }, icon('check'), W.obsMode === 'keep' ? 'Use my current setup' : 'Switch OBS to these')));

    // add-ons
    if (W.obs && W.obs.addons && Object.keys(W.obs.addons).length) {
      const scenes = (W.obs.scenes || []).map(String);
      out.push(h('h4', { class: 'ob-sub ob-gap', text: 'Add to a scene (optional)' }),
        h('p', { class: 'hint', text: 'Adds one item to a scene you pick. Nothing else in the scene changes.' }),
        h('ul', { class: 'ob-addons' }, Object.entries(W.obs.addons).map(([id, label]) => {
          const sid = 'ob-addon-' + id;
          const s = h('select', { id: sid, 'aria-label': `Scene for ${String(label)}` },
            h('option', { value: '', text: 'Choose a scene…' }), scenes.map((n) => h('option', { value: n, text: n, selected: W.addonScene[id] === n })));
          s.addEventListener('change', () => { W.addonScene[id] = s.value; addBtn.disabled = !s.value; });
          const res = W.addonRes[id];
          const addBtn = asyncButton({ class: 'small', disabled: !W.addonScene[id] }, async () => {
            try {
              const r = await api('/api/setup/obs/addon', { scene: W.addonScene[id], addon: id });
              W.addonRes[id] = r.ok === false ? { err: String(r.error || 'Not added') } : { ok: r.created ? `Added “${String(r.source)}”.` : `“${String(r.source)}” was already there.` };
            } catch (e) { W.addonRes[id] = { err: e.message }; }
            render();
          }, icon('plus'), 'Add');
          return h('li', {}, h('span', { class: 'ob-name', text: String(label) }), h('div', { class: 'row nowrap ob-addon-pick' }, s, addBtn),
            res ? h('div', { class: res.err ? 'err' : 'ob-meta ok', role: 'status', text: res.err || res.ok }) : null);
        })));
    }
    return h('section', { class: 'ob-found' }, ...out);
  };

  /* imports: one open preview at a time */
  const importKey = (a) => a.source_id;
  const preview = async (key, body) => {
    W.open = key;
    W.imports[key] = { state: 'loading', body };
    render();
    try {
      const r = await api('/api/setup/import/preview', body);
      W.imports[key] = { state: 'ready', body, r, choices: { ...(r.defaults || {}) } };
    } catch (e) { W.imports[key] = { state: 'error', body, err: e.message }; }
    render();
  };
  const applyImport = async (key) => {
    const I = W.imports[key];
    if (unsaved()) {
      const ok = await confirmDialog('Import with unsaved edits?', `${UNSAVED} Import anyway?`, { ok: 'Import anyway', danger: true });
      show();
      if (!ok) return;
    }
    try {
      const r = await api('/api/setup/import/apply', { token: I.r.token, choices: I.choices });
      W.imports[key] = { ...I, state: 'done', counts: r.counts || {} };
    } catch (e) {
      if (e.status === 404 || e.status === 409) {
        await preview(key, I.body);
        W.imports[key].note = e.status === 409 ? 'Your config changed in the meantime, so this preview was refreshed. Check it and import again.'
          : 'The preview expired, so it was made again. Check it and import again.';
      } else W.imports[key] = { ...I, err: 'Couldn’t import: ' + e.message };
    }
    render();
  };

  const reviewList = (items, allowJump) => h('ul', { class: 'ob-review' }, items.map((it) => h('li', { class: it.conflict ? 'ob-if' : null },
    it.conflict && !allowJump ? h('div', { class: 'ob-if-note', text: `Only if you take their version of ${itemName(String(it.path || ''))}` }) : null,
    h('div', { class: 'ob-row' },
      h('span', { class: 'ob-review-dot', 'aria-hidden': 'true', text: '!' }),
      h('span', { class: 'ob-name', text: String(it.label || '(no label)') }),
      h('span', { class: 'badge', text: String(it.kind || 'action') }),
      h('span', { class: 'ob-meta', text: `page “${String(it.page)}” · row ${Number(it.row) + 1}, key ${Number(it.col) + 1}` }),
      allowJump ? h('button', { class: 'small ghost ob-jump', onclick: async () => { closeModal(); go('deck'); await showKey(it); } }, 'Show in the Deck editor', icon('arrowRight')) : null),
    h('pre', { class: 'ob-arg', text: String(it.argument ?? '') }))));

  const previewPanel = (key, a) => {
    const I = W.imports[key];
    if (!I || W.open !== key) return null;
    if (I.state === 'loading') return h('div', { class: 'ob-preview' }, h('p', { class: 'hint', text: 'Reading it and comparing it with your config…' }), skeleton(4));
    if (I.state === 'error') return h('div', { class: 'ob-preview' }, errorLine('Couldn’t read it: ' + I.err, () => preview(key, I.body)));
    const r = I.r;
    const out = [];
    // a key from a conflicting plan item only arrives when its "Use imported" was picked
    const all = r.review || [];
    const review = I.state === 'done' ? all.filter((it) => !it.conflict || I.choices[it.path] === 'incoming') : all;
    const skipped = all.length - review.length;
    if (I.note) out.push(h('div', { class: 'banner warn', role: 'alert', text: I.note }));
    if (I.state === 'done') {
      const c = I.counts || {};
      out.push(okBanner(h('strong', { text: 'Imported. ' }), `${plural(c.added || 0, 'item')} added, ${plural(c.replaced || 0, 'item')} replaced, ${plural(c.kept || 0, 'item')} of yours kept. A restore point was saved first (Settings → Backups & history).`));
      if (review.length) {
        out.push(h('h4', { class: 'ob-sub ob-gap', text: `Waiting for your review (${review.length})` }),
          h('p', { class: 'hint', text: 'These keys arrived switched off. Open each one in the Deck editor, check exactly what it runs, and enable it there.' }),
          reviewList(review, true));
      }
      if (skipped) out.push(h('p', { class: 'hint', text: `${plural(skipped, 'other key')} that needed review stayed out, because you kept your version of ${skipped === 1 ? 'its page' : 'their pages'}.` }));
      return h('div', { class: 'ob-preview' }, ...out);
    }
    const info = r.info || {};
    const facts = [info.model ? `Made for ${String(info.model)}` : null,
      Array.isArray(info.grid) && info.grid.length === 2 ? `${Number(info.grid[0])}×${Number(info.grid[1])} keys` : null].filter(Boolean);
    if (facts.length) out.push(h('p', { class: 'ob-meta', text: facts.join(' · ') }));
    const warns = (r.warnings || []).map(String);
    if (warns.length) {
      out.push(h('details', { class: 'ob-details' }, h('summary', {}, icon('warn'), `${plural(warns.length, 'note')} about this import`),
        h('ul', { class: 'ob-warnlist' }, warns.map((w) => h('li', { text: w })))));
    }
    if (review.length) {
      out.push(warnBanner(h('strong', { text: `${plural(review.length, 'key')} will arrive switched off. ` }),
        'They run commands, press keys or call other apps. After importing, you check and enable each one in the Deck editor.'),
      h('details', { class: 'ob-details' }, h('summary', {}, icon('eye'), 'See exactly what they run'), reviewList(review, false)));
    }
    const count = () => planChangeCount(r.plan, I.choices);
    const btn = h('button', { class: 'primary' }, icon('check'), count() ? `Import ${plural(count(), 'change')}` : 'Nothing to import');
    btn.disabled = !count();
    const update = () => { const k = count(); btn.disabled = !k; btn.replaceChildren(icon('check'), k ? `Import ${plural(k, 'change')}` : 'Nothing to import'); };
    btn.addEventListener('click', async () => { btn.classList.add('busy'); btn.disabled = true; await applyImport(key); });
    out.push(h('div', { class: 'ob-plan' }, ...planReview(r.plan, I.choices, update)));
    if (I.err) out.push(h('p', { class: 'err', role: 'alert', text: I.err }));
    out.push(h('div', { class: 'row end' }, h('button', { class: 'ghost', text: 'Cancel', onclick: () => { W.open = null; render(); } }), btn));
    return h('div', { class: 'ob-preview' }, ...out);
  };

  const importRow = (key, title, meta, body, badge) => {
    const I = W.imports[key];
    const done = I && I.state === 'done';
    return h('li', { class: 'ob-import' + (W.open === key ? ' open' : '') },
      h('div', { class: 'ob-row' },
        h('div', { class: 'grow ob-import-text' }, h('span', { class: 'ob-name', text: title }), h('span', { class: 'ob-meta', text: meta })),
        badge, done ? h('span', { class: 'badge ok', text: 'Imported' }) : null,
        W.open === key ? null : h('button', { class: 'small', onclick: () => preview(key, body) }, icon('eye'), done ? 'Preview again' : 'Preview')),
      previewPanel(key));
  };

  const uploadPicker = () => {
    const input = h('input', { type: 'file', accept: '.streamDeckProfile', class: 'sr-only', tabindex: '-1', 'aria-hidden': 'true' });
    const btn = h('button', { class: 'small' }, icon('plus'), 'Choose a .streamDeckProfile file…');
    btn.addEventListener('click', () => input.click());
    input.addEventListener('change', async () => {
      const f = input.files && input.files[0];
      if (!f) return;
      W.uploadErr = '';
      if (f.size > UPLOAD_MAX) { W.uploadErr = 'That file is larger than 50 MB.'; render(); return; }
      btn.classList.add('busy'); btn.disabled = true;
      try {
        const { upload_id: id } = await apiUpload('/api/setup/upload', f);
        W.uploadName = f.name;
        await preview('upload', { kind: 'elgato', upload_id: id });
        return;
      } catch (e) { W.uploadErr = 'Couldn’t read that file: ' + e.message; }
      render();
    });
    return [btn, input];
  };

  const previewing = () => !!(W.open && W.imports[W.open] && W.imports[W.open].state === 'ready');
  const step3 = () => {
    const apps = deckApps();
    const out = [];
    if (hasObs()) out.push(obsCard());
    out.push(h('section', { class: 'ob-found' },
      h('div', { class: 'ob-found-head' }, h('span', { class: 'ob-found-icon', 'aria-hidden': 'true' }, icon('deck')), h('h3', { text: 'Keys and pages' })),
      h('p', { class: 'hint', text: 'Preview shows everything that would be added or replaced before anything is saved.' }),
      apps.length ? h('ul', { class: 'ob-imports' }, apps.map((a) => importRow(importKey(a), String(a.label || appName(a.kind)),
        a.kind === 'elgato' ? 'Elgato profile file' : `${appName(a.kind)} · ${plural(a.pages, 'page')} · ${plural(a.keys, 'key')}`,
        { kind: a.kind, source_id: a.source_id }, a.running ? h('span', { class: 'badge warn', text: 'Running' }) : null))) : null,
      h('div', { class: 'ob-upload' },
        h('h4', { class: 'ob-sub', text: 'Import an Elgato profile (.streamDeckProfile)' }),
        h('p', { class: 'hint', text: 'Exported from the Elgato Stream Deck app (right-click a profile → Export). Up to 50 MB.' }),
        h('div', { class: 'row' }, ...uploadPicker()),
        h('p', { class: 'err', role: 'alert', text: W.uploadErr }),
        W.imports.upload ? h('ul', { class: 'ob-imports' }, importRow('upload', String(W.uploadName || 'Uploaded profile'), 'Elgato profile file', W.imports.upload.body, null)) : null)));
    out.push(foot(h('button', { class: 'ghost', text: 'Back', onclick: () => goStep(2) }),
      h('button', { class: previewing() ? '' : 'primary', onclick: () => goStep(4) }, 'Next', icon('arrowRight'))));
    return out;
  };

  /* ---- step 4: another app holds the deck */
  const loadRunning = async () => {
    W.running = null; W.runningErr = '';
    try { W.running = await api('/api/setup/apps/running'); } catch (e) { W.runningErr = e.message; }
    if (W.step === 4) render();
  };
  const stopApp = async (kind) => {
    const name = appName(kind);
    let ok = await confirmDialog(`Stop ${name}?`, `${name} is closed and won’t start by itself at login any more (its autostart entry is turned off; a copy is kept in your backups). You can start it again yourself any time.`, { ok: `Stop ${name}`, danger: true });
    show();
    if (!ok) return;
    try {
      let r = await api('/api/setup/app/stop', { kind, confirm: true });
      if (r.still_running > 0) {
        ok = await confirmDialog(`${name} didn’t close`, `${name} is still running. Force it to quit? Anything it hasn’t saved is lost.`, { ok: 'Force quit', danger: true });
        show();
        if (ok) {
          const forced = await api('/api/setup/app/stop', { kind, confirm: true, force: true });
          r = { ...forced, autostart_disabled: r.autostart_disabled || forced.autostart_disabled };
        }
      }
      W.stopped[kind] = r;
    } catch (e) { W.stopped[kind] = { err: e.message }; }
    await loadRunning();
  };

  const step4 = () => {
    const out = [];
    if (W.runningErr) out.push(errorLine('Couldn’t check which apps are running: ' + W.runningErr, loadRunning));
    else if (!W.running) out.push(skeleton(2));
    else {
      const running = Object.entries(W.running).filter(([, on]) => on).map(([k]) => k);
      out.push(h('p', { class: 'ob-lead', text: 'A Stream Deck can only listen to one app at a time.' }));
      for (const [kind, r] of Object.entries(W.stopped)) {
        if (r.err) out.push(h('p', { class: 'err', role: 'alert', text: `Couldn’t stop ${appName(kind)}: ${r.err}` }));
        else {
          out.push(okBanner(h('span', { text: r.still_running > 0 ? `${appName(kind)} is still running.` : `${appName(kind)} is stopped.` }),
            h('div', { class: 'ob-meta', text: r.autostart_disabled ? 'It won’t start at login any more.' : 'Its autostart was already off.' })));
        }
      }
      if (!running.length) out.push(okBanner(h('span', { text: 'No other app is using your deck.' })));
      for (const kind of running) {
        out.push(h('section', { class: 'ob-found ob-conflict' },
          h('div', { class: 'ob-found-head' }, h('span', { class: 'ob-found-icon warn', 'aria-hidden': 'true' }, icon('warn')),
            h('h3', { text: `${appName(kind)} has your deck` })),
          h('p', { class: 'hint', text: `While ${appName(kind)} runs, the deck won’t respond to this panel.` }),
          h('div', { class: 'row' }, h('button', { class: 'danger', onclick: () => stopApp(kind) }, icon('stop'), `Stop ${appName(kind)} and turn off its autostart`))));
      }
    }
    out.push(foot(h('button', { class: 'ghost', text: 'Back', onclick: () => goStep(3) }),
      h('button', { class: 'primary', onclick: () => { finish(); toast('All set. Your setup is ready.', { ok: true }); } }, icon('check'), 'Finish')));
    return out;
  };

  show();
  render();
  if (!W.scan) scanNow();
  else if (W.step === 3 && hasObs()) loadObs();
  else if (W.step === 4) loadRunning();
}

/* ------------------------------------------------------------------ Settings: Existing setup */

export function existingSetupCard() {
  return h('section', { class: 'card ob-setup-card' },
    h('div', { class: 'card-head' }, h('span', { class: 'card-icon' }, icon('layers')),
      h('div', { class: 'card-titles' }, h('h2', { class: 'card-title', text: 'Existing setup' }),
        h('p', { class: 'card-sub', text: 'Bring over OBS and another Stream Deck app (StreamController, streamdeck-ui, Boatswain, Elgato profiles).' }))),
    h('button', { class: 'small', onclick: () => openOnboarding() }, icon('arrowRight'), 'Open the setup assistant'));
}

/* ------------------------------------------------------------------ Settings: External backups */

const X = { card: null, list: null, data: null, interrupted: null, err: '', running: {}, obsRunning: false, seq: 0, at: 0 };

async function refreshExternal() {
  if (!X.card) return;
  X.at = Date.now();
  const my = ++X.seq;
  X.err = '';
  try {
    const [b, run, scan] = await Promise.all([api('/api/setup/backups'), api('/api/setup/apps/running').catch(() => ({})),
      api('/api/setup/detect').catch(() => null)]);
    if (my !== X.seq) return;
    X.data = b.backups || [];
    X.interrupted = isInterrupted(b.interrupted_restore) ? b.interrupted_restore : null;   // absent on older daemons
    X.running = run || {};
    X.obsRunning = !!(scan && scan.obs && scan.obs.running);
  } catch (e) {
    if (my !== X.seq) return;
    X.err = e.message;
  }
  renderExternal();
}

function isRunning(kind) { return kind === 'obs' ? X.obsRunning : !!X.running[kind]; }

function isInterrupted(v) {
  return !!v && typeof v === 'object' && typeof v.before === 'string' && v.before !== '' && v.target !== undefined;
}

/** A restore that was cut off (crash, power loss) can leave a half-written folder: put the
    "before" backup back, which is the copy made just before that restore began. */
async function restoreInterrupted(ir) {
  const before = (X.data || []).find((a) => a.id === ir.before);
  const items = before ? (before.items || []).filter((it) => it.label !== 'autostart files') : [];
  if (!items.length) { toast('The backup made before that restore is gone, so it can’t be put back from here.', { bad: true, ms: 8000 }); return; }
  const ok = await confirmDialog('Put the earlier settings back?',
    `The folder ${String(ir.target)} is replaced with the copy from ${when(before.ts)}, which was saved just before the interrupted restore. Its current contents are backed up first.`,
    { ok: 'Restore', danger: true });
  if (!ok) return;
  try {
    for (const it of items) await api('/api/setup/restore', { id: before.id, item: it.kind, confirm: true });
    toast('Put back. The settings from before the interrupted restore are in place.', { ok: true, ms: 8000 });
  } catch (e) {
    toast(e.status === 409 ? `Not restored: ${e.message}` : `Couldn’t restore: ${e.message}`, { bad: true, ms: 8000 });
  }
  refreshExternal();
}

function interruptedCard(ir) {
  return h('div', { class: 'banner warn ob-interrupted', role: 'alert' },
    h('div', { class: 'row nowrap' }, icon('warn'),
      h('div', { class: 'grow' }, h('strong', { text: 'A restore was interrupted' }),
        h('p', { class: 'hint', text: `A restore of ${String(ir.target)} was interrupted on ${when(ir.started)}. Restore the “before” backup to put it back.` }))),
    h('div', { class: 'row' }, h('button', { class: 'small', onclick: () => restoreInterrupted(ir) }, icon('undo'), 'Restore the “before” backup')));
}

async function restoreItem(a, item) {
  const name = String(item.label || appName(item.kind));
  const ok = await confirmDialog(`Restore ${name}?`,
    `${name}’s current settings are backed up first, then replaced with the copy from ${when(a.ts)}. Files that aren’t in the backup are left alone.`,
    { ok: 'Restore', danger: true });
  if (!ok) return;
  try {
    const req = { id: a.id, item: item.kind, confirm: true };
    let r;
    try {
      r = await api('/api/setup/restore', req);
    } catch (e) {
      const mm = e.status === 409 ? e.mismatch : null;
      if (!mm) throw e;
      const again = await confirmDialog('Restore into a different folder?',
        `This backup came from ${String(mm.archive_root)} but ${name} is now using ${String(mm.target_root)}. Restore it into ${String(mm.target_root)} anyway?`,
        { ok: 'Restore anyway', danger: true });
      if (!again) return;
      r = await api('/api/setup/restore', { ...req, allow_other_target: true });
    }
    const notes = (r.notes || []).map(String);
    toast(h('div', {}, h('span', { text: `${name} restored (${plural(r.restored || 0, 'file')}). The settings it replaced were backed up first.` }),
      notes.length ? h('ul', {}, notes.slice(0, 6).map((n) => h('li', { text: n }))) : null), { ok: true, ms: 8000 });
  } catch (e) {
    toast(e.status === 409 ? `Not restored: ${e.message}` : `Couldn’t restore: ${e.message}`, { bad: true, ms: 8000 });
  }
  refreshExternal();
}

async function pin(a, pinned) {
  try { await api('/api/setup/backups/pin', { id: a.id, pinned }); } catch (e) { toast(e.message, { bad: true }); }
  refreshExternal();
}

async function del(a) {
  const ok = await confirmDialog('Delete this backup?', `The copy from ${when(a.ts)} is deleted for good.`, { ok: 'Delete backup', danger: true });
  if (!ok) return;
  try { await api('/api/setup/backups/delete', { id: a.id }); toast('Backup deleted', { ok: true }); } catch (e) { toast(e.message, { bad: true }); }
  refreshExternal();
}

function archiveRow(a) {
  const items = a.items || [];
  return h('li', { class: 'ob-arch' },
    h('div', { class: 'ob-arch-main' },
      h('div', { class: 'ob-row' },
        h('span', { class: 'ob-name', text: String(a.label || 'Backup') }),
        a.pinned ? h('span', { class: 'badge accent', text: 'Pinned', title: 'Kept until you delete it' }) : null,
        a.auto ? h('span', { class: 'badge', text: 'Automatic' }) : null),
      h('div', { class: 'ob-meta', text: `${when(a.ts)} · ${fmtSize(a.size)}` }),
      h('ul', { class: 'ob-arch-items' }, items.map((it) => {
        const name = String(it.label || appName(it.kind));
        const busy = isRunning(it.kind);
        const autostart = it.label === 'autostart files';
        const why = autostart ? 'Autostart files are kept for reference and aren’t restored from here.'
          : busy ? `Close ${appName(it.kind)} first: it rewrites its settings when it quits.` : '';
        return h('li', {},
          h('span', { class: 'ob-arch-item' }, h('span', { text: name }), h('span', { class: 'ob-meta', text: plural(it.files, 'file') })),
          h('button', { class: 'small', disabled: autostart || busy, title: why || `Put back ${name} from this backup`, 'aria-label': `Restore ${name} from ${when(a.ts)}`, onclick: () => restoreItem(a, it) }, icon('undo'), 'Restore'),
          why && !autostart ? h('span', { class: 'note warn ob-why', text: why }) : null);
      }))),
    h('div', { class: 'ob-arch-tools' },
      h('button', { class: 'small ghost', onclick: () => pin(a, !a.pinned) }, icon(a.pinned ? 'x' : 'lock'), a.pinned ? 'Unpin' : 'Pin'),
      h('button', { class: 'small ghost', disabled: a.pinned, title: a.pinned ? 'Unpin it first' : 'Delete', 'aria-label': `Delete the backup from ${when(a.ts)}`, onclick: () => del(a) }, icon('trash'))));
}

function renderExternal() {
  if (!X.list) return;
  const warn = X.interrupted ? interruptedCard(X.interrupted) : null;
  if (X.err && !X.data) { X.list.replaceChildren(errorLine('Couldn’t load the external backups: ' + X.err, refreshExternal)); return; }
  if (!X.data) { X.list.replaceChildren(skeleton(3)); return; }
  if (!X.data.length) {
    X.list.replaceChildren(...[warn, empty('No external backups yet', 'The setup assistant makes one before it changes anything.', 'lock')].filter(Boolean));
    return;
  }
  X.list.replaceChildren(...[warn, h('ul', { class: 'ob-archs' }, X.data.map(archiveRow))].filter(Boolean));
}

/** Settings card; one element reused across Settings re-renders (like historyCard). */
export function externalBackupsCard() {
  if (!X.card) {
    X.list = h('div', { class: 'ob-arch-list' });
    X.card = h('section', { class: 'card ob-ext-card' },
      h('div', { class: 'card-head' }, h('span', { class: 'card-icon' }, icon('lock')),
        h('div', { class: 'card-titles' }, h('h2', { class: 'card-title', text: 'External backups' }),
          h('p', { class: 'card-sub', text: `Private copies of OBS and other deck apps’ settings, kept in ${STATE_DIR}. They can include your stream key, so they are never shown or downloaded here.` })),
        h('div', { class: 'bk-head-actions' },
          h('button', { class: 'small ghost', title: 'Check again', onclick: refreshExternal }, icon('refresh')),
          h('button', { class: 'small', onclick: () => openOnboarding({ step: 2 }) }, icon('lock'), 'Back up now'))),
      X.list);
    renderExternal();
  }
  if (Date.now() - X.at > 15000) queueMicrotask(refreshExternal);   // Settings re-renders often
  return X.card;
}
