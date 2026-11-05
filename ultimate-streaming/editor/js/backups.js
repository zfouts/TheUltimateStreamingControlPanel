/* Backups & history (Settings card), the import wizard and the first-run welcome.
   Every revision of config.yaml can be diffed against the current file and put back;
   a restore or an import always records the current file first, so both can be undone.
   Styles: css/backups.css. */
import { api, on } from './api.js';
import { h, icon, toast, openModal, closeModal, confirmDialog, modalOpen, field, switchInput, asyncButton, empty } from './ui.js';
import { maybeOnboard } from './onboarding.js';

const UNSAVED = 'You have unsaved edits in the Deck editor. They won’t be included, and saving them afterwards will ask you to reload first. Save them now if you want to keep them.';
const WELCOME_KEY = 'deck_welcome_dismissed';
const MAX_DIFF_LINES = 3000;
const PAGE_SIZE = 30;
const MAX_FILE = 1024 * 1024;

const SOURCES = {
  panel: ['Panel save', ''],
  file: ['Edited by hand', ''],
  import: ['Import', 'accent'],
  restore: ['Restore', 'warn'],
  manual: ['Snapshot', 'accent'],
  'before-save': ['Before save', ''],
  start: ['Started', ''],
  legacy: ['Older backup', ''],
};

/* ------------------------------------------------------------------ small helpers */

/** True while the Deck/Settings editor holds edits that aren't saved yet. */
function unsaved() {
  const b = document.getElementById('save');
  return !!(b && !b.disabled);
}

const DAY = 86400000;
function startOfDay(d) { return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime(); }

function dayLabel(d) {
  const diff = Math.round((startOfDay(new Date()) - startOfDay(d)) / DAY);
  if (diff === 0) return 'Today';
  if (diff === 1) return 'Yesterday';
  const opts = { weekday: 'short', month: 'short', day: 'numeric' };
  if (d.getFullYear() !== new Date().getFullYear()) opts.year = 'numeric';
  return d.toLocaleDateString(undefined, opts);
}
function timeLabel(d) { return d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' }); }

/** "today at 3:42 PM", "yesterday at 9:05 AM", "Mon, Oct 27 at 8:00 PM". */
function whenPhrase(ts) {
  if (typeof ts !== 'number') return 'an unknown time';
  const d = new Date(ts * 1000);
  const day = dayLabel(d);
  return `${day === 'Today' || day === 'Yesterday' ? day.toLowerCase() : day} at ${timeLabel(d)}`;
}

function dateFromYmd(s) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s || '');
  return m ? new Date(+m[1], +m[2] - 1, +m[3]) : null;
}

function relTime(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return 'just now';
  if (s < 3600) return plural(Math.floor(s / 60), 'minute') + ' ago';
  if (s < 86400) return plural(Math.floor(s / 3600), 'hour') + ' ago';
  if (s < 86400 * 30) return plural(Math.floor(s / 86400), 'day') + ' ago';
  return 'on ' + new Date(ts * 1000).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

function plural(n, word) { return `${n.toLocaleString()} ${word}${n === 1 ? '' : 's'}`; }

/** /home/me/x → ~/x (the daemon doesn't send the home folder; this covers the usual places). */
function tilde(p) { return String(p || '').replace(/^\/(?:home|Users)\/[^/]+(?=\/|$)|^\/root(?=\/|$)/, '~'); }

/** Split an importer path (dot separated, parts with a dot are "quoted"). */
function splitPath(p) {
  const out = [];
  const re = /"([^"]*)"|([^.]+)/g;
  let m;
  while ((m = re.exec(String(p)))) out.push(m[1] !== undefined ? m[1] : m[2]);
  return out;
}

/** A readable name for an import item: “Page “main” (layout xl)”, “twitch → channel”. */
export function itemName(p) {
  const s = splitPath(p);
  if (s[0] === 'layouts' && s[2] === 'pages' && s.length === 4) return `Page “${s[3]}” (layout ${s[1]})`;
  if (s[0] === 'layouts' && s.length === 2) return `Layout “${s[1]}”`;
  if (s[0] === 'pages' && s.length === 2) return `Page “${s[1]}”`;
  if (s[0] === 'keys' && s.length === 2) return `Saved key “${s[1]}”`;
  return s.join(' → ');
}

/** Restores are labelled "Restored <revision id>" / "Restored daily <date>" by the daemon. */
function friendlyLabel(label) {
  if (typeof label !== 'string') return label;
  let m = /^Restored (?:config-)?(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})\b/.exec(label);
  if (m) {
    const d = new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]);
    return 'Put back the version from ' + whenPhrase(d.getTime() / 1000);
  }
  m = /^Restored daily (\d{4}-\d{2}-\d{2})$/.exec(label);
  if (m) {
    const d = dateFromYmd(m[1]);
    return 'Put back the daily backup of ' + (d ? dayLabel(d).replace(/^(Today|Yesterday)$/, (x) => x.toLowerCase()) : m[1]);
  }
  return label;
}

function skeletonRows(n) {
  return h('div', { class: 'bk-skel', 'aria-busy': 'true', 'aria-label': 'Loading' },
    Array.from({ length: n }, (_, i) => h('div', { class: 'bk-skel-row' },
      h('span', { class: 'skeleton line', style: { width: '64px' } }),
      h('span', { class: 'skeleton line', style: { width: `${48 + ((i * 17) % 40)}%` } }))));
}

function errorBlock(text, retry) {
  return h('div', { class: 'bk-error', role: 'alert' }, icon('warn'),
    h('span', { class: 'grow', text }),
    retry && h('button', { class: 'small', onclick: retry }, icon('refresh'), 'Try again'));
}

/** A success toast with an Undo button (kept up longer so there's time to use it). */
function toastUndo(text, undo) {
  const btn = h('button', { class: 'small', text: 'Undo' });
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    const t = btn.closest('.toast');
    if (t) t.remove();
    try {
      await undo();
      toast('Undone. Your previous config is back.', { ok: true });
    } catch (e) { toast('Couldn’t undo: ' + (e.message || e), { bad: true }); }
    refreshHistory();
  });
  toast(h('div', { class: 'bk-toast' }, h('span', { text }), btn), { ok: true, ms: 10000 });
}

/** Put back the file whose hash was `hash` (the state just before a restore/import).
    Matching by hash is exact even when the "before" copy was deduplicated against an
    older revision; without a hash, fall back to the newest labelled / before-save copy. */
async function restoreHash(hash, label) {
  const { revisions = [] } = await api('/api/backups');
  const same = (r) => hash && r.hash === hash;
  const rev = (label && revisions.find((r) => r.label === label && same(r)))
    || revisions.find(same)
    || (label ? revisions.find((r) => r.label === label) : revisions.find((r) => r.source === 'before-save'));
  if (!rev) throw new Error('no restore point was found');
  await api('/api/backups/restore', { id: rev.id });
}

async function currentHash() {
  try { return (await api('/api/state')).hash || null; } catch (e) { return null; }
}

/* ------------------------------------------------------------------ restore */

/** Confirm, restore a revision ({id}) or a daily bundle ({date}), and offer Undo. */
async function restore({ id, date, when }) {
  const msg = date
    ? `Restore the config from the daily backup of ${when}? Your current config is kept as a revision, so you can undo this.`
    : `Restore the config from ${when}? Your current config is kept as a revision, so you can undo this.`;
  const ok = await confirmDialog('Restore this version?', unsaved() ? `${UNSAVED} ${msg}` : msg, { ok: 'Restore' });
  if (!ok) return;
  const before = await currentHash();
  try {
    const r = await api(date ? '/api/backups/restore-daily' : '/api/backups/restore', date ? { date } : { id });
    if (r.hash) H.curHash = r.hash;
    toastUndo(`Config restored from ${when}.`, () => restoreHash(before, null));
  } catch (e) {
    toast('Couldn’t restore: ' + e.message, { bad: true });
  }
  refreshHistory();
}

/* ------------------------------------------------------------------ diff modal */

function diffLines(text) {
  const lines = text.split('\n');
  if (lines.length && lines[lines.length - 1] === '') lines.pop();
  // drop the ---/+++ file header: the legend says which side is which
  let i = 0;
  while (i < lines.length && i < 2 && /^(---|\+\+\+) /.test(lines[i])) i++;
  return lines.slice(i);
}

function showDiff(rev) {
  const when = whenPhrase(rev.ts);
  const body = h('div', { class: 'bk-diff-modal' });
  const load = async () => {
    body.replaceChildren(h('div', { class: 'bk-diff bk-diff-loading' }, skeletonRows(8)));
    let diff;
    try { ({ diff } = await api('/api/backups/diff?id=' + encodeURIComponent(rev.id))); } catch (e) {
      body.replaceChildren(errorBlock('Couldn’t load the changes: ' + e.message, load), closeRow());
      return;
    }
    const lines = diffLines(diff || '');
    if (!lines.length) {
      body.replaceChildren(empty('No differences', 'This version is the same as your current config.', 'check'), closeRow());
      return;
    }
    const frag = document.createDocumentFragment();
    for (const line of lines.slice(0, MAX_DIFF_LINES)) {
      const c = line[0];
      const cls = line.startsWith('@@') ? 'hunk' : c === '+' ? 'add' : c === '-' ? 'del' : c === '…' ? 'note' : 'ctx';
      const el = document.createElement('div');
      el.className = 'bk-dl ' + cls;
      el.textContent = line || ' ';
      frag.append(el);
    }
    const view = h('div', { class: 'bk-diff scroll', role: 'region', 'aria-label': 'Differences', tabindex: '0' });
    view.append(frag);
    body.replaceChildren(...[
      h('div', { class: 'bk-legend' },
        h('span', { class: 'bk-key del' }, h('span', { class: 'bk-sign', 'aria-hidden': 'true', text: '−' }), `Only in the version from ${when}`),
        h('span', { class: 'bk-key add' }, h('span', { class: 'bk-sign', 'aria-hidden': 'true', text: '+' }), 'Only in your current config')),
      h('p', { class: 'hint', text: 'Restoring brings back the red lines and removes the green ones.' }),
      view,
      lines.length > MAX_DIFF_LINES && h('p', { class: 'note warn', text: `Showing the first ${MAX_DIFF_LINES.toLocaleString()} of ${lines.length.toLocaleString()} lines.` }),
      h('div', { class: 'row end bk-modal-foot' },
        h('button', { class: 'ghost', text: 'Close', onclick: closeModal }),
        h('button', { class: 'primary', onclick: () => restore({ id: rev.id, when }) }, icon('undo'), 'Restore this version')),
    ].filter(Boolean));
  };
  const closeRow = () => h('div', { class: 'row end bk-modal-foot' }, h('button', { class: 'ghost', text: 'Close', onclick: closeModal }));
  openModal(`Changes since ${when}`, body, { wide: true });
  load();
}

/* ------------------------------------------------------------------ snapshot */

function snapshotDialog() {
  const input = h('input', { type: 'text', maxlength: '80', placeholder: 'e.g. Before stream', autocomplete: 'off' });
  const save = asyncButton({ class: 'primary', type: 'button' }, async () => {
    const label = input.value.trim();
    const r = await api('/api/backups/snapshot', label ? { label } : {});
    closeModal();
    if (r.revision) toast(label ? `Snapshot “${label}” saved.` : 'Snapshot saved.', { ok: true });
    else toast('Nothing new to save: the newest revision already matches your config.');
    refreshHistory();
  }, icon('check'), 'Save snapshot');
  openModal('Save a snapshot', h('form', { class: 'stack', onsubmit: (e) => { e.preventDefault(); save.click(); } },
    h('p', { class: 'hint', text: 'Keeps a copy of your config as it is right now. A label makes it easy to find later.' }),
    field('Label (optional)', input, 'Up to 80 characters.'),
    h('div', { class: 'row end bk-modal-foot' },
      h('button', { type: 'button', class: 'ghost', text: 'Cancel', onclick: closeModal }), save)));
}

/* ------------------------------------------------------------------ history card */

const H = {
  card: null, panel: null, tabs: null, tab: 'revisions', showAuto: false, limit: PAGE_SIZE,
  data: null, error: null, curHash: null, seq: 0, stale: true,
};

async function refreshHistory() {
  if (!H.card) return;
  if (!H.card.isConnected) { H.stale = true; return; }
  H.stale = false;
  const my = ++H.seq;
  H.error = null;
  if (!H.data) renderHistory();
  try {
    const [list, hash] = await Promise.all([api('/api/backups'), H.curHash ? H.curHash : currentHash()]);
    if (my !== H.seq) return;
    H.data = list;
    H.curHash = hash;
  } catch (e) {
    if (my !== H.seq) return;
    H.error = e.message;
  }
  renderHistory();
}

function revisionRow(r, isCurrent) {
  const d = typeof r.ts === 'number' ? new Date(r.ts * 1000) : null;
  const when = whenPhrase(r.ts);
  const [name, tone] = SOURCES[r.source] || [r.source || 'Change', ''];
  return h('li', { class: 'bk-row' + (isCurrent ? ' current' : '') },
    h('div', { class: 'bk-time', text: d ? timeLabel(d) : '—' }),
    h('div', { class: 'bk-main' },
      h('div', { class: 'bk-line' },
        h('span', { class: 'badge ' + tone, text: name }),
        isCurrent && h('span', { class: 'badge ok', text: 'Current' }),
        r.label && h('span', { class: 'bk-label', text: friendlyLabel(r.label), title: friendlyLabel(r.label) })),
      r.summary && h('div', { class: 'bk-summary', text: r.summary, title: r.summary })),
    h('div', { class: 'bk-actions' },
      h('button', { class: 'small ghost', 'aria-label': `View changes since ${when}`, onclick: () => showDiff(r) }, icon('eye'), 'View changes'),
      h('button', {
        class: 'small', disabled: isCurrent, title: isCurrent ? 'This is your current config' : null,
        'aria-label': `Restore the version from ${when}`, onclick: () => restore({ id: r.id, when }),
      }, icon('undo'), 'Restore')));
}

function renderRevisions() {
  const all = (H.data && H.data.revisions) || [];
  const autoCount = all.filter((r) => r.source === 'before-save').length;
  const rows = all.filter((r) => H.showAuto || r.source !== 'before-save');
  const toolbar = h('div', { class: 'bk-toolbar' },
    h('span', { class: 'hint', text: rows.length ? plural(rows.length, 'revision') : '' }),
    autoCount > 0 && switchInput(H.showAuto, (v) => { H.showAuto = v; renderHistory(); }, `Show automatic copies (${autoCount})`));
  if (!all.length) return [empty('No revisions yet', 'One is saved every time the config changes.', 'clock')];
  if (!rows.length) return [toolbar, empty('Only automatic copies so far', 'Turn on “Show automatic copies” to see them.', 'clock')];

  const current = rows.find((r) => H.curHash && r.hash === H.curHash);
  const out = [toolbar];
  let day = null, list = null;
  for (const r of rows.slice(0, H.limit)) {
    const label = typeof r.ts === 'number' ? dayLabel(new Date(r.ts * 1000)) : 'Unknown date';
    if (label !== day) {
      day = label;
      list = h('ul', { class: 'bk-list' });
      out.push(h('h3', { class: 'bk-day', text: label }), list);
    }
    list.append(revisionRow(r, r === current));
  }
  if (rows.length > H.limit) {
    const more = Math.min(PAGE_SIZE, rows.length - H.limit);
    out.push(h('div', { class: 'bk-more' }, h('button', { class: 'small ghost', onclick: () => { H.limit += PAGE_SIZE; renderHistory(); } },
      icon('chevronDown'), `Show ${more} older`)));
  }
  return out;
}

function renderDaily() {
  const days = (H.data && H.data.daily) || [];
  if (!days.length) return [empty('No daily backups yet', 'One is made each day the deck runs, and kept for 30 days.', 'clock')];
  return [
    h('p', { class: 'hint bk-toolbar', text: 'A copy of the config (and stream stats) from each day. Restoring puts back the config only.' }),
    h('ul', { class: 'bk-list' }, days.map((d) => {
      const date = dateFromYmd(d.date);
      const when = date ? dayLabel(date) : d.date;
      const files = Array.isArray(d.files) ? d.files : [];
      return h('li', { class: 'bk-row daily' },
        h('div', { class: 'bk-main' },
          h('div', { class: 'bk-line' }, h('span', { class: 'bk-label', text: when }),
            date && when !== d.date && h('span', { class: 'bk-summary', text: d.date })),
          h('div', { class: 'bk-summary', text: files.length ? files.join(', ') : 'config.yaml', title: files.join(', ') })),
        h('div', { class: 'bk-actions' },
          h('button', { class: 'small', 'aria-label': `Restore the config from ${when}`, disabled: files.length > 0 && !files.includes('config.yaml'),
            onclick: () => restore({ date: d.date, when: when === 'Today' || when === 'Yesterday' ? when.toLowerCase() : when }) },
          icon('undo'), 'Restore config')));
    })),
  ];
}

function renderHistory() {
  if (!H.panel) return;
  for (const b of H.tabs.children) {
    const on = b.dataset.tab === H.tab;
    b.setAttribute('aria-selected', String(on));
    b.tabIndex = on ? 0 : -1;
  }
  H.panel.setAttribute('aria-labelledby', 'bk-tab-' + H.tab);
  let kids;
  if (H.error && !H.data) kids = [errorBlock('Couldn’t load the backups: ' + H.error, refreshHistory)];
  else if (!H.data) kids = [skeletonRows(5)];
  else {
    kids = H.tab === 'daily' ? renderDaily() : renderRevisions();
    if (H.error) kids.unshift(errorBlock('Couldn’t refresh: ' + H.error, refreshHistory));
  }
  H.panel.replaceChildren(...kids);
}

/** The Settings card. The same element is returned on every call (Settings re-renders often),
    so the open tab and scroll position survive; it refreshes itself on `config` events. */
export function historyCard() {
  if (!H.card) {
    const tab = (id, text) => h('button', {
      role: 'tab', id: 'bk-tab-' + id, 'aria-controls': 'bk-panel', dataset: { tab: id }, text,
      onclick: () => { H.tab = id; renderHistory(); },
    });
    H.tabs = h('div', { class: 'tabs', role: 'tablist', 'aria-label': 'Backup kind' }, tab('revisions', 'Revisions'), tab('daily', 'Daily'));
    H.tabs.addEventListener('keydown', (e) => {
      if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
      e.preventDefault();
      H.tab = H.tab === 'revisions' ? 'daily' : 'revisions';
      renderHistory();
      document.getElementById('bk-tab-' + H.tab).focus();
    });
    H.panel = h('div', { class: 'bk-panel', id: 'bk-panel', role: 'tabpanel' });
    H.card = h('section', { class: 'card bk-card', id: 'backups-card' },
      h('div', { class: 'card-head' }, h('span', { class: 'card-icon' }, icon('clock')),
        h('div', { class: 'card-titles' }, h('h2', { class: 'card-title', text: 'Backups & history' }),
          h('p', { class: 'card-sub', text: 'Every change to your config is kept. Look at what changed and put any version back.' })),
        h('div', { class: 'bk-head-actions' },
          h('button', { class: 'small', onclick: snapshotDialog }, icon('camera'), 'Snapshot now'),
          h('button', { class: 'small', onclick: () => openImport() }, icon('layers'), 'Import a setup…'))),
      H.tabs, H.panel);
    on('config', (data) => {
      if (data && data.hash) H.curHash = data.hash;
      refreshHistory();
    });
    renderHistory();
  }
  if (H.stale) queueMicrotask(refreshHistory);   // after Settings has put the card in the page
  return H.card;
}

/* ------------------------------------------------------------------ import plan review */

/** How many changes applying `plan` with `choices` makes. */
export function planChangeCount(plan, choices) {
  return ((plan && plan.added) || []).length + Object.values(choices || {}).filter((c) => c === 'incoming').length;
}

/** The "Will be added / Conflicts / Unchanged" part of an import preview. Picking a side writes
    choices[path] and calls onChange(). Shared by the import wizard and the setup assistant. */
export function planReview(p, choices, onChange) {
  const added = p.added || [], conflicts = p.conflicts || [];
  const out = [];
  out.push(h('h3', {}, 'Will be added ', h('span', { class: 'badge accent', text: String(added.length) })));
  out.push(added.length
    ? h('ul', { class: 'bk-plan scroll' }, added.map((a) => h('li', {},
      h('span', { class: 'bk-plan-name', text: itemName(a.path), title: a.path }),
      h('span', { class: 'bk-plan-sum', text: a.summary || '' }))))
    : h('p', { class: 'hint', text: 'Nothing new to add.' }));

  out.push(h('h3', {}, 'Conflicts ', h('span', { class: 'badge ' + (conflicts.length ? 'warn' : ''), text: String(conflicts.length) })));
  if (conflicts.length) {
    out.push(h('p', { class: 'hint bk-gap', text: p.fresh
      ? 'Both configs have these. The imported version is picked because this is a fresh install.'
      : 'Both configs have these, but they differ. Yours is kept unless you pick the imported one.' }));
    out.push(h('ul', { class: 'bk-conflicts' }, conflicts.map((c) => {
      const seg = h('div', { class: 'seg', role: 'group', 'aria-label': `Which version of ${itemName(c.path)}` });
      const opt = (v, t) => h('button', { type: 'button', 'aria-pressed': String(choices[c.path] === v), text: t, onclick: () => {
        choices[c.path] = v;
        for (const b of seg.children) b.setAttribute('aria-pressed', String(b.dataset.v === v));
        if (onChange) onChange();
      }, dataset: { v } });
      seg.append(opt('current', 'Keep mine'), opt('incoming', 'Use imported'));
      const cmp = h('div', { class: 'bk-compare', hidden: true },
        h('figure', {}, h('figcaption', { text: 'Yours' }), h('pre', { class: 'bk-pre scroll', text: c.current })),
        h('figure', {}, h('figcaption', { text: 'Imported' }), h('pre', { class: 'bk-pre scroll', text: c.incoming })));
      const toggle = h('button', { type: 'button', class: 'small ghost bk-toggle', 'aria-expanded': 'false' }, icon('chevronRight'), 'Compare');
      toggle.addEventListener('click', () => {
        cmp.hidden = !cmp.hidden;
        toggle.setAttribute('aria-expanded', String(!cmp.hidden));
      });
      return h('li', {},
        h('div', { class: 'bk-conflict-head' },
          h('span', { class: 'bk-plan-name', text: itemName(c.path), title: c.path }), toggle, seg),
        cmp);
    })));
  } else out.push(h('p', { class: 'hint', text: 'None: nothing you have will be overwritten.' }));

  out.push(h('h3', { text: 'Unchanged' }));
  out.push(h('p', { class: 'hint', text: p.same ? `${plural(p.same, 'item')} already match${p.same === 1 ? 'es' : ''} and stay as they are.` : 'Nothing is the same yet.' }));
  return out;
}

/* ------------------------------------------------------------------ import wizard */

/** What a waiting field does (core/importer.py REVIEW_FIELDS). */
const REVIEW_FIELD = {
  action: 'When pressed', left: 'Turning left', right: 'Turning right', press: 'Pressing the dial',
  state: 'Light up when…', display: 'Live text',
};

/** Settings the import left out (dropped_settings): they pointed at files, servers or commands. */
function droppedNotice(dropped) {
  if (!dropped || !dropped.length) return null;
  return h('div', { class: 'banner warn', role: 'note' },
    h('p', { text: 'These settings point at files or servers and weren’t imported:' }),
    h('ul', {}, dropped.map((d) => h('li', {}, h('code', { text: String(d) })))),
    h('p', { text: 'Set them yourself on this machine.' }));
}

/** One imported action / state / display that is off until enabled; `enable` (async) adds the button. */
function reviewItem(it, enable) {
  const kinds = (it.kinds || []).join(', ') || 'nothing';
  const title = `${it.where}${it.label ? ` (“${it.label}”)` : ''}`;
  return h('li', {},
    h('div', { class: 'review-title' }, h('span', { class: 'review-dot', 'aria-hidden': 'true', text: '!' }),
      h('strong', { text: title })),
    h('div', { class: 'review-label', text: `${REVIEW_FIELD[it.field] || it.field}: ${kinds}` }),
    h('pre', { class: 'review-arg', tabindex: '0', text: String(it.argument) }),
    enable ? asyncButton({ class: 'review-enable' }, enable, icon('check'), 'Enable this') : null);
}

const STEPS = ['Choose a setup', 'Review', 'Done'];

/** The import wizard. openImport({path}) skips straight to reviewing that file. */
export function openImport(opts = {}) {
  const W = {
    step: 1, candidates: null, loadErr: null, chooseErr: '', source: null, plan: null, choices: {},
    hash: null, stale: false, applyErr: '', counts: null, busy: false, dropped: [], pending: [], review: [],
    afterHash: null,
  };
  const stepper = h('ol', { class: 'bk-stepper', 'aria-label': 'Import steps' });
  const panel = h('div', { class: 'bk-wizard-panel' });
  const body = h('div', { class: 'bk-wizard' }, stepper, panel);
  const show = () => openModal('Import a setup', body, { wide: true });

  const render = () => {
    stepper.replaceChildren(...STEPS.map((t, i) => {
      const n = i + 1, last = n === STEPS.length;
      const state = n < W.step || (last && W.step === n) ? 'done' : n === W.step ? 'current' : 'upcoming';
      return h('li', { class: state, 'aria-current': n === W.step ? 'step' : null },
        h('span', { class: 'bk-mark', 'aria-hidden': 'true' }, state === 'done' ? icon('check') : String(n)),
        h('span', { class: 'bk-step-name', text: t }));
    }));
    panel.replaceChildren(...(W.step === 1 ? step1() : W.step === 2 ? step2() : step3()));
  };

  const loadCandidates = async () => {
    W.loadErr = null; W.candidates = null; render();
    try { W.candidates = (await api('/api/import/candidates')).candidates || []; } catch (e) { W.loadErr = e.message; }
    if (W.step === 1) render();
  };

  /** Preview a source; keeps earlier choices for conflicts that are still there. */
  const preview = async (src, keepChoices) => {
    const hash = await currentHash();
    if (hash === null) throw new Error('the current config couldn’t be read; try again');
    const r = await api('/api/import/preview', src.path ? { path: src.path } : { text: src.text });
    const choices = { ...(r.defaults || {}) };
    if (keepChoices) for (const p of Object.keys(choices)) if (W.choices[p]) choices[p] = W.choices[p];
    Object.assign(W, { source: src, plan: r.plan, choices, hash, step: 2, applyErr: '',
      dropped: r.dropped_settings || [], pending: r.review || [] });
  };

  const choose = async (src, btn) => {
    if (W.busy) return;
    W.busy = true; W.chooseErr = '';
    if (btn) btn.classList.add('busy');
    try { await preview(src, false); W.stale = false; } catch (e) { W.chooseErr = e.message; W.step = 1; }
    W.busy = false;
    render();
  };

  /* ---- step 1: choose */
  const step1 = () => {
    const fileInput = h('input', { type: 'file', accept: '.yaml,.yml', class: 'sr-only', tabindex: '-1', 'aria-hidden': 'true' });
    fileInput.addEventListener('change', () => {
      const f = fileInput.files && fileInput.files[0];
      if (!f) return;
      if (f.size > MAX_FILE) { W.chooseErr = 'That file is too big for a deck config (over 1 MB).'; render(); return; }
      const reader = new FileReader();
      reader.onload = () => choose({ text: String(reader.result), name: f.name }, fileBtn);
      reader.onerror = () => { W.chooseErr = 'That file can’t be read.'; render(); };
      reader.readAsText(f);
    });
    const fileBtn = h('button', { onclick: () => fileInput.click() }, icon('plus'), 'Choose a file…');

    let list;
    if (W.loadErr) list = errorBlock('Couldn’t look for setups: ' + W.loadErr, loadCandidates);
    else if (!W.candidates) list = skeletonRows(3);
    else {
      const others = W.candidates.filter((c) => !c.current);
      list = h('ul', { class: 'bk-cands' }, W.candidates.map((c) => {
        const parts = [];
        if (c.layouts) parts.push(plural(c.layouts, 'layout'));
        if (c.pages) parts.push(plural(c.pages, 'page'));
        const secs = (c.sections || []).slice(0, 5).join(', ') + ((c.sections || []).length > 5 ? ', …' : '');
        const meta = [typeof c.mtime === 'number' ? 'Changed ' + relTime(c.mtime) : null, parts.join(', ') || null, secs || null]
          .filter(Boolean).join(' · ');
        const inner = [
          h('span', { class: 'bk-cand-icon' }, icon(c.current ? 'check' : 'layers')),
          h('span', { class: 'bk-cand-text' },
            h('span', { class: 'bk-cand-path', text: tilde(c.path), title: c.path }),
            h('span', { class: 'bk-cand-meta', text: meta })),
        ];
        if (c.current) return h('li', {}, h('div', { class: 'bk-cand is-current', 'aria-disabled': 'true' }, ...inner, h('span', { class: 'badge', text: 'This config' })));
        const b = h('button', { class: 'bk-cand', disabled: W.busy }, ...inner, icon('chevronRight'));
        b.addEventListener('click', () => choose({ path: c.path }, b));
        return h('li', {}, b);
      }));
      if (!others.length) list = h('div', {}, list, h('p', { class: 'hint bk-none', text: 'No other setups were found on this computer. You can choose a file instead.' }));
    }
    return [
      h('p', { class: 'hint', text: 'Bring keys, pages and settings over from another config. You’ll see every change before anything is saved.' }),
      h('h3', { text: 'Found on this computer' }),
      list,
      h('h3', { text: 'Somewhere else' }),
      h('div', { class: 'row' }, fileBtn, fileInput, h('span', { class: 'hint', text: 'A config.yaml file, up to 1 MB.' })),
      h('p', { class: 'err', role: 'alert', text: W.chooseErr }),
    ];
  };

  /* ---- step 2: review */
  const changeCount = () => planChangeCount(W.plan, W.choices);

  const step2 = () => {
    if (!W.plan) return [h('p', { class: 'hint', text: 'Comparing it with your config…' }), skeletonRows(4)];
    const p = W.plan;
    const added = p.added || [], conflicts = p.conflicts || [];
    const n = changeCount();
    const importBtn = h('button', { class: 'primary', disabled: n === 0 || W.busy }, icon('check'),
      n === 0 ? 'Nothing to import' : `Import ${plural(n, 'change')}`);
    importBtn.addEventListener('click', () => apply(importBtn));
    const updateCount = () => {
      const k = changeCount();
      importBtn.disabled = k === 0;
      importBtn.replaceChildren(icon('check'), k === 0 ? 'Nothing to import' : `Import ${plural(k, 'change')}`);
    };

    const srcName = W.source.path ? tilde(W.source.path) : (W.source.name || 'the chosen file');
    const out = [
      h('div', { class: 'bk-source' }, h('span', { class: 'hint', text: 'From' }), h('code', { text: srcName, title: W.source.path || srcName }),
        h('button', { class: 'small ghost', onclick: () => { W.step = 1; render(); } }, 'Choose another')),
    ];
    if (W.stale) out.push(h('div', { class: 'banner warn', role: 'alert' },
      'Your config changed while you were reviewing, so this preview was refreshed. Check it and import again.'));
    if (p.fresh) out.push(h('div', { class: 'banner' }, 'This looks like a fresh install, so your imported setup replaces the example.'));
    const dropped = droppedNotice(W.dropped);
    if (dropped) out.push(dropped);
    if (W.pending.length) {
      out.push(h('div', { class: 'banner warn', role: 'note' },
        h('strong', { text: `${plural(W.pending.length, 'key setting')} will arrive switched off. ` }),
        'They could run commands, press keys or reach other machines, so nothing runs until you enable each one after importing.',
        h('details', { class: 'ob-details' }, h('summary', {}, icon('eye'), 'See exactly what they do'),
          h('ul', { class: 'ob-review' }, W.pending.map((it) => reviewItem(it, null))))));
    }

    out.push(...planReview(p, W.choices, updateCount));

    out.push(h('p', { class: 'err', role: 'alert', text: W.applyErr }));
    out.push(h('div', { class: 'row end bk-modal-foot' },
      h('button', { class: 'ghost', onclick: () => { W.step = 1; render(); } }, 'Back'), importBtn));
    if (!added.length && !conflicts.length) out.splice(out.length - 1, 0, empty('Nothing to import', 'This setup already matches yours.', 'check'));
    return out;
  };

  const apply = async (btn) => {
    if (unsaved()) {
      const ok = await confirmDialog('Import with unsaved edits?', `${UNSAVED} Import anyway?`, { ok: 'Import anyway', danger: true });
      show();
      if (!ok) return;
    }
    W.busy = true; W.applyErr = '';
    btn.classList.add('busy'); btn.disabled = true;
    const src = W.source.path ? { path: W.source.path } : { text: W.source.text };
    try {
      const r = await api('/api/import/apply', { ...src, choices: W.choices, hash: W.hash });
      W.counts = r.counts || {};
      W.before = W.hash;
      W.afterHash = r.hash;
      W.review = r.review || [];
      W.step = 3;
      W.busy = false;
      render();
      refreshHistory();
      const close = panel.querySelector('button.primary');
      if (close) close.focus();
      return;
    } catch (e) {
      W.busy = false;
      if (e.status === 409) {
        try { await preview(W.source, true); W.stale = true; } catch (e2) { W.applyErr = e2.message; }
      } else W.applyErr = 'Couldn’t import: ' + e.message;
    }
    render();
  };

  /** Enable one waiting item: bound to config.yaml as it is now and to exactly what was shown. */
  const enableItem = async (it) => {
    try {
      const r = await api('/api/import/review', { path: it.path, field: it.field, hash: W.afterHash, expect: it.expect, enable: true });
      W.afterHash = r.hash;
      W.review = W.review.filter((x) => x !== it);
      toast('Enabled.', { ok: true });
    } catch (e) {
      toast(e.status === 409
        ? 'Not enabled: config.yaml changed since the import. Check that key in the Deck editor.'
        : 'Not enabled: ' + e.message, { bad: true, ms: 8000 });
    }
    render();
  };

  /* ---- step 3: done */
  const step3 = () => {
    const c = W.counts || {};
    const undo = asyncButton({ class: '' }, async () => {
      await restoreHash(W.before, 'Before import');
      closeModal();
      toast('Import undone. Your previous config is back.', { ok: true });
      refreshHistory();
    }, icon('undo'), 'Undo import');
    return [
      h('div', { class: 'bk-done' },
        h('span', { class: 'bk-done-icon' }, icon('check')),
        h('h3', { class: 'bk-done-title', text: 'Setup imported' }),
        h('p', { class: 'hint', text: 'The deck picks up the new config in a moment.' })),
      h('div', { class: 'bk-counts' },
        h('div', { class: 'stat' }, h('span', { class: 'stat-label', text: 'Added' }), h('span', { class: 'stat-value', text: String(c.added || 0) })),
        h('div', { class: 'stat' }, h('span', { class: 'stat-label', text: 'Replaced' }), h('span', { class: 'stat-value', text: String(c.replaced || 0) })),
        h('div', { class: 'stat' }, h('span', { class: 'stat-label', text: 'Kept yours' }), h('span', { class: 'stat-value', text: String(c.kept || 0) }))),
      h('div', { class: 'banner ok bk-restore-point' }, h('span', {}, icon('check'), 'A restore point was saved first, so you can undo this.')),
      droppedNotice(W.dropped),
      W.review.length ? h('h3', { text: `Waiting for your review (${W.review.length})` }) : null,
      W.review.length ? h('p', { class: 'hint', text: 'These are off. Only enable one if you recognize it and want it to do exactly that.' }) : null,
      W.review.length ? h('ul', { class: 'ob-review' }, W.review.map((it) => reviewItem(it, () => enableItem(it)))) : null,
      h('div', { class: 'row end bk-modal-foot' }, undo, h('button', { class: 'primary', text: 'Close', onclick: closeModal })),
    ];
  };

  show();
  if (opts.path) { W.step = 2; choose({ path: opts.path }); }   // straight to the review
  loadCandidates();   // also needed for Back
}

/* ------------------------------------------------------------------ welcome */

let welcomed = false;

/** First run: the setup assistant when OBS or another deck app is found (onboarding.js),
    else offer to import a config.yaml found on this computer (once per load). */
export async function maybeWelcome() {
  if (welcomed) return;
  welcomed = true;
  const { candidates = [], fresh } = await api('/api/import/candidates');
  if (!fresh) return;
  if (await maybeOnboard()) return;
  try { if (localStorage.getItem(WELCOME_KEY)) return; } catch (e) { /* private mode: ask each load */ }
  const found = candidates.find((c) => !c.current);
  if (!found || modalOpen()) return;
  const parts = [];
  if (found.layouts) parts.push(plural(found.layouts, 'layout'));
  if (found.pages) parts.push(plural(found.pages, 'page'));
  if (typeof found.mtime === 'number') parts.push('changed ' + relTime(found.mtime));
  const notNow = () => {
    try { localStorage.setItem(WELCOME_KEY, '1'); } catch (e) { /* ignore */ }
    closeModal();
  };
  openModal('Welcome', h('div', { class: 'bk-welcome' },
    h('p', { text: 'We found an existing setup on this computer:' }),
    h('div', { class: 'bk-cand is-found' },
      h('span', { class: 'bk-cand-icon' }, icon('layers')),
      h('span', { class: 'bk-cand-text' },
        h('span', { class: 'bk-cand-path', text: tilde(found.path), title: found.path }),
        parts.length > 0 && h('span', { class: 'bk-cand-meta', text: parts.join(' · ') }))),
    h('p', { text: 'Import it? You’ll see exactly what changes before anything is saved.' }),
    h('div', { class: 'row end bk-modal-foot' },
      h('button', { class: 'ghost', text: 'Not now', onclick: notNow }),
      h('button', { class: 'primary', onclick: () => openImport({ path: found.path }) }, 'Review import', icon('arrowRight')))));
  const primary = document.querySelector('#modal-body .bk-welcome button.primary');
  if (primary) primary.focus();
}
