/* Sections with in-page tabs instead of nested sidebar entries.

   A tab is either a view of its own (Twitch → Bot is #view=bot) or a tab inside one view
   (Settings → OBS is #view=settings&tab=obs). The sidebar shows only each group's first view;
   while any of the group's tabs is open, that sidebar entry is the current one.

   main.js renders the strip (renderTabs) above the views; a view with inner tabs (Settings)
   reads its active tab with innerTab(). */

export const GROUPS = {
  twitch: [
    { view: 'twitch', label: 'Channel' },
    { view: 'bot', label: 'Bot' },
    { view: 'announce', label: 'Announcements' },
    { view: 'automod', label: 'Moderation' },
  ],
  settings: [
    { view: 'settings', tab: 'general', label: 'General' },
    { view: 'settings', tab: 'obs', label: 'OBS' },
    { view: 'settings', tab: 'brb', label: 'BRB & radio' },
    { view: 'settings', tab: 'home', label: 'Home Assistant' },
    { view: 'settings', tab: 'access', label: 'Access' },
    { view: 'settings', tab: 'backups', label: 'Backups' },
    { view: 'discord', label: 'Discord' },
  ],
};

function params() { return new URLSearchParams(location.hash.slice(1)); }

/** The group a view belongs to ('twitch', 'settings') or null. */
export function groupOf(view) {
  for (const [key, tabs] of Object.entries(GROUPS)) if (tabs.some((t) => t.view === view)) return key;
  return null;
}

/** The sidebar entry (a view name) that stands for this view: the group's first view, else itself. */
export function navView(view) {
  const g = groupOf(view);
  return g ? GROUPS[g][0].view : view;
}

/** For a view with inner tabs: the open tab's id (an unknown or missing ?tab= gives the first one). */
export function innerTab(view) {
  const g = groupOf(view);
  if (!g) return null;
  const inner = GROUPS[g].filter((t) => t.view === view && t.tab);
  if (!inner.length) return null;
  const want = params().get('tab');
  return (inner.find((t) => t.tab === want) || inner[0]).tab;
}

/** The tab entry that is open now for this view (null outside a group). */
export function activeTab(view) {
  const g = groupOf(view);
  if (!g) return null;
  const tab = innerTab(view);
  return GROUPS[g].find((t) => t.view === view && (!t.tab || t.tab === tab)) || null;
}

/** Open a tab: its view, plus ?tab= for an inner tab. Keeps #theme= if present. */
export function openTab(entry) {
  const p = new URLSearchParams({ view: entry.view });
  if (entry.tab) p.set('tab', entry.tab);
  const theme = params().get('theme');
  if (theme) p.set('theme', theme);
  location.hash = p.toString();
}

/** Fill `el` with the tab strip for `view`'s group, or hide it when the view has no group.
    Arrow keys / Home / End move between tabs (WAI-ARIA tabs pattern, manual activation on Enter
    is not needed: moving focus opens the tab, as each tab is cheap to show). */
let currentView = null;
export function renderTabs(el, view) {
  currentView = view;
  const g = groupOf(view);
  el.hidden = !g;
  if (!g) { el.replaceChildren(); el.dataset.group = ''; return; }
  const active = activeTab(view);
  const entries = GROUPS[g];
  const mark = (b, t) => {
    const on = t === active;
    b.setAttribute('aria-selected', String(on));
    b.tabIndex = on ? 0 : -1;
  };
  el.setAttribute('role', 'tablist');
  el.setAttribute('aria-label', g === 'twitch' ? 'Twitch' : 'Settings');
  el.onkeydown = (e) => {
    const btns = [...el.children];
    const i = btns.indexOf(document.activeElement);
    if (i < 0) return;
    const n = btns.length;
    const to = { ArrowRight: (i + 1) % n, ArrowLeft: (i - 1 + n) % n, Home: 0, End: n - 1 }[e.key];
    if (to === undefined) return;
    e.preventDefault();
    btns[to].focus();
    openTab(entries[to]);
  };
  // Same group already drawn: update in place, so keyboard focus stays on the button.
  if (el.dataset.group === g && el.children.length === entries.length) {
    [...el.children].forEach((b, i) => mark(b, entries[i]));
    return;
  }
  const hadFocus = el.contains(document.activeElement);
  const buttons = entries.map((t) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.setAttribute('role', 'tab');
    mark(b, t);
    b.dataset.view = t.view;
    if (t.tab) b.dataset.tab = t.tab;
    b.textContent = t.label;
    b.addEventListener('click', () => { if (t !== activeTab(currentView)) openTab(t); });
    return b;
  });
  el.dataset.group = g;
  el.replaceChildren(...buttons);
  if (hadFocus) buttons.find((b) => b.tabIndex === 0).focus();
}
