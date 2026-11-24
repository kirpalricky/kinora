// Behavioural test of the Settings DOM code that the pure setup-logic test cannot reach: the Add a device draft, the
// edit error clearing, the theme fallbacks and the QR code disclosure. The named functions are read from static/setup.js and
// run in a vm context with a minimal DOM stub (no browser, no network). Run: node tests/test_setup_dom.js
const fs = require('fs'), vm = require('vm');
const dir = __dirname + '/../kinora/web/static/';
const appSrc = fs.readFileSync(dir + 'app.js', 'utf8'), setupSrc = fs.readFileSync(dir + 'setup.js', 'utf8');
const playerBlock = appSrc.split('/* player-logic:begin */')[1].split('/* player-logic:end */')[0];
let ok = true;
const check = (n, c) => { console.log((c ? 'PASS ' : 'FAIL ') + n); ok = ok && !!c; };

// One top-level function of setup.js, by name (braces matched).
function fnSource(name) {
  const i = setupSrc.indexOf('\nfunction ' + name + '(');
  if (i < 0) throw new Error('no function ' + name + ' in setup.js');
  const start = i + 1;
  let depth = 0;
  for (let k = setupSrc.indexOf('{', start); k < setupSrc.length; k++) {
    if (setupSrc[k] === '{') depth++;
    else if (setupSrc[k] === '}' && --depth === 0) return setupSrc.slice(start, k + 1);
  }
  throw new Error('unbalanced ' + name);
}

// A minimal element: enough for h() (attributes, children, text, listeners) and for the tests to walk the tree.
class Node {}
class El extends Node {
  constructor(tag) { super(); this.tagName = tag; this.attrs = {}; this.children = []; this.className = ''; this.textContent = ''; this.listeners = {}; this.dataset = {}; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  addEventListener(ev, fn) { this.listeners[ev] = fn; }
  append(...kids) { this.children.push(...kids); }
  replaceChildren(...kids) { this.children = kids; }
  focus() { this.focused = true; }
  replaceWith(n) { this.replacedBy = n; }
  // a dialog: showModal opens, close fires onclose (the browser fires it asynchronously; the stub does it at once)
  showModal() { this.open = true; }
  close() { if (!this.open) return; this.open = false; if (this.onclose) this.onclose(); }
  // only the [data-focus="key"] form the page uses
  querySelector(sel) {
    const key = (sel.match(/data-focus="([^"]+)"/) || [])[1];
    for (const c of this.children) {
      if (!(c instanceof El)) continue;
      if (key && c.attrs['data-focus'] === key) return c;
      const hit = c.querySelector(sel); if (hit) return hit;
    }
    return null;
  }
}
const walk = (node, visit) => { if (!(node instanceof El)) return; visit(node); node.children.forEach(c => walk(c, visit)); };
const find = (root, pred) => { const out = []; walk(root, n => { if (pred(n)) out.push(n); }); return out; };

// A fresh page: setup.js functions in one context. withApp: app.js has run (THEMES, theme, applyTheme exist).
function load(names, withApp) {
  const ctx = {
    Node, renders: [], painted: [], reloaded: false,
    document: {createElement: t => new El(t), querySelector: () => null, activeElement: null},
    SU: {sel: 'devices', view: 'settings', screen: 'list', edit: '', opener: '', errs: {}, saved: '', qr: false,
      devForm: {ip: '', name: ''}, devDirty: false, devErr: '', newRow: null, add: null, changed: false, playable: true, prevSel: '', dev: null},
    renderView() { ctx.renders.push(1); },
    narrow: () => false, loadDevices() {}, copyAddress() {}, icon: () => new El('svg'),
    history: {pushState() {}}, localStorage: {setItem() {}},
    location: {reload() { ctx.reloaded = true; }},
    byId: id => (ctx.els[id] = ctx.els[id] || new El(id)), els: {},
  };
  if (withApp) ctx.applyTheme = t => ctx.painted.push(t);
  vm.createContext(ctx);
  if (withApp) vm.runInContext(playerBlock + ';\nlet theme = "system";', ctx);
  vm.runInContext(names.map(fnSource).join('\n'), ctx);
  return ctx;
}
const FUNCS = ['h', 'btn', 'cred', 'errAt', 'addressBox', 'openQr', 'qrOpen', 'closeQrIfStale', 'redrawDevices', 'devicesVisible',
  'typingNow', 'startEdit', 'endEdit', 'openDeviceForm',
  'go', 'selectPane', 'closeSetup', 'setTheme', 'themeGroup', 'themeKeys', 'themeNow', 'themeChoices', 'themeWord'];
const draft = {ip: '192.168.1.5', name: 'half typed'};
const empty = d => d.ip === '' && d.name === '';

// ---- the Add a device draft is dropped when Settings moves on ----
{
  const c = load(FUNCS, true);
  c.SU.edit = 'device-add'; c.SU.devForm = {...draft}; c.endEdit();
  check('draft: Cancel or Escape (endEdit) drops the draft', empty(c.SU.devForm) && c.SU.edit === '');
  c.SU.devForm = {...draft}; c.go('settings');
  check('draft: changing view (go) drops the draft', empty(c.SU.devForm));
  c.SU.devForm = {...draft}; c.selectPane('new');
  check('draft: changing pane (selectPane) drops the draft', empty(c.SU.devForm));
  c.SU.devForm = {...draft}; c.closeSetup();
  check('draft: closing Settings (closeSetup) drops the draft', empty(c.SU.devForm) && c.els.setup.hidden === true);
}

// ---- opening a form clears only its own errors (a Sharing error stays) ----
{
  const c = load(FUNCS, true);
  c.SU.errs = {sharing: 'Could not turn sharing on.', add: 'old add error'};
  c.startEdit('device-add', 'dev-open');
  check('errors: opening Add a device keeps the Sharing error and clears only the add error', c.SU.errs.sharing === 'Could not turn sharing on.' && !('add' in c.SU.errs) && c.SU.edit === 'device-add');
  c.SU.errs = {sharing: 'kept?', name: 'row error'};
  c.startEdit('name:c1', 'rename:c1');
  check('errors: other forms still start with a clean slate', Object.keys(c.SU.errs).length === 0);
}

// ---- the QR code: a QR icon button and Copy in row 1; the full code in the "Open on your phone" dialog ----
{
  const c = load(FUNCS, true);
  const url = 'http://192.168.1.5:80';
  const buttonsIn = n => find(n, x => x.tagName === 'button');
  const tree = c.addressBox({address: url, address_error: null});
  const card = find(tree, n => n.className === 'su-card-s')[0];
  check('qr: one address card, one row (no disclosure, no details, no summary)', !!card && card.children.length === 1
    && find(tree, n => n.tagName === 'details' || n.tagName === 'summary' || n.className === 'su-more-d').length === 0);
  const acts = buttonsIn(card);
  check('qr: row 1 has exactly two actions, the QR icon then Copy', acts.length === 2 && acts[0].attrs['data-focus'] === 'qr-open' && acts[1].attrs['data-focus'] === 'copy-address'
    && acts[1].children.includes('Copy'));
  check('qr: the QR button is icon only, labelled and titled "Show QR code", 36px like Copy (sm class)', acts[0].attrs['aria-label'] === 'Show QR code'
    && acts[0].attrs.title === 'Show QR code' && acts[0].children.length === 1 && acts[0].children[0].tagName === 'svg' && acts[0].className.includes('sm'));
  check('qr: no aria-expanded or aria-controls anywhere on the address row', find(tree, n => n.attrs['aria-expanded'] !== undefined || n.attrs['aria-controls'] !== undefined).length === 0);
  c.addressBox({address: null, address_error: 'Kinora is not listening on your network.'});
  check('qr: no address, no card', find(c.addressBox({address: null, address_error: 'x'}), n => n.className === 'su-card-s').length === 0);

  // opening: a dialog (the one Settings dialog) with the image, the address, the hint and one Close
  const root = new El('section'); root.append(tree); c.els.setup = root;
  c.els.setupmain = new El('main');
  c.openQr(url);
  const dlg = c.els.suconfirm;
  check('qr modal: opens as the Settings dialog with the address as its state', dlg.open === true && dlg.dataset.qr === url);
  const img = find(dlg, n => n.tagName === 'img')[0];
  check('qr modal: the image is the route with the address as cache key, 260px, alt names the address', !!img
    && img.attrs.src === '/api/setup/devices/qr?a=' + encodeURIComponent(url) && img.attrs.alt === 'QR code for ' + url && img.attrs.width === '260');
  check('qr modal: title "Open on your phone" (h2) and the address in mono', find(dlg, n => n.tagName === 'h2' && n.textContent === 'Open on your phone').length === 1
    && find(dlg, n => n.className === 'su-qr-addr su-mono' && n.textContent === url).length === 1);
  check('qr modal: the Wi-Fi line is the muted hint', find(dlg, n => n.className === 'su-hint' && n.textContent === 'Your phone must be on this Wi-Fi. Then allow it under Waiting for approval.').length === 1);
  const closeBtns = find(dlg, n => n.tagName === 'button');
  check('qr modal: exactly one button, a ghost Close, and it takes focus on open', closeBtns.length === 1 && closeBtns[0].children.includes('Close')
    && closeBtns[0].className.includes('ghost') && closeBtns[0].focused === true);
  // closing returns focus to the QR button (the opener by data-focus)
  closeBtns[0].listeners.click();
  check('qr modal: Close ends the dialog and focus returns to the QR button (qr-open)', dlg.open === false && acts[0].focused === true && dlg.dataset.qr === undefined);
  // the image that does not load becomes the hint
  c.openQr(url);
  const img2 = find(c.els.suconfirm, n => n.tagName === 'img')[0];
  img2.listeners.error();
  check('qr modal: when the image does not load, a hint replaces it', !!img2.replacedBy && img2.replacedBy.className === 'su-hint'
    && img2.replacedBy.textContent === 'The QR code did not load. Type the address on your phone instead.');
  c.els.suconfirm.close();

  // the refresh: a stale code closes, a current one stays, and the page is not redrawn under an open code
  c.openQr(url);
  c.SU.dev = {address: 'http://192.168.1.77:80'};
  c.closeQrIfStale();
  check('refresh: the code closes when the address changes', c.els.suconfirm.open === false);
  c.openQr(url);
  c.SU.dev = {address: url};
  c.closeQrIfStale();
  check('refresh: the code stays open when the address is the same', c.els.suconfirm.open === true);
  c.SU.dev = {address: null, sharing: false};
  c.closeQrIfStale();
  check('refresh: the code closes when sharing turns off (no address)', c.els.suconfirm.open === false);
  c.openQr(url);
  c.SU.devDirty = true; c.SU.view = 'settings'; c.SU.sel = 'devices'; c.SU.dev = {address: url};
  const before = c.renders.length;
  c.redrawDevices();
  check('refresh: a changed list does not redraw the page while the code is open (no focus taken)', c.renders.length === before && c.SU.devDirty === true);
  c.els.suconfirm.close();
  c.redrawDevices();
  check('refresh: the page redraws once the code is closed', c.renders.length === before + 1 && c.SU.devDirty === false);
  // the dialog's Escape is native (the setup key handler leaves it alone while suconfirm is open): see the static checks
}

// ---- Theme: the page's own choice is used; with app.js missing, Settings still renders (Auto) and can save ----
{
  const c = load(FUNCS, true);
  c.setTheme('dark');
  check('theme: a choice in Settings is applied through applyTheme and kept in the shared variable', c.painted.join() === 'dark' && vmRead(c, 'theme') === 'dark');
  c.setTheme('light');
  const group = c.themeGroup();
  const chosen = find(group, n => n.attrs['aria-checked'] === 'true');
  check('theme: the radio group marks the choice (Light) and names the three words in order', chosen.length === 1 && chosen[0].textContent === 'Light'
    && find(group, n => n.attrs.role === 'radio').map(n => n.textContent).join(',') === 'Auto,Light,Dark');
  const bare = load(FUNCS, false);
  let threw = false;
  try { bare.setTheme('light'); bare.themeGroup(); } catch (e) { threw = true; }
  check('theme fallback: with app.js not loaded, Settings still renders and saving does not throw', !threw);
  const words = find(load(FUNCS, false).themeGroup(), n => n.attrs.role === 'radio');   // a fresh page: no choice yet
  check('theme fallback: the group offers Auto, Light, Dark and marks Auto', words.map(n => n.textContent).join(',') === 'Auto,Light,Dark' && words[0].attrs['aria-checked'] === 'true');
}

function vmRead(ctx, name) { return vm.runInContext(name, ctx); }

console.log(ok ? 'ALL PASS' : 'SOME FAILED');
process.exit(ok ? 0 : 1);
