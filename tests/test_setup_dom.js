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
  constructor(tag) { super(); this.tagName = tag; this.attrs = {}; this.children = []; this.className = ''; this.textContent = ''; this.listeners = {}; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  addEventListener(ev, fn) { this.listeners[ev] = fn; }
  append(...kids) { this.children.push(...kids); }
  focus() { this.focused = true; }
  replaceWith(n) { this.replacedBy = n; }
}
const walk = (node, visit) => { if (!(node instanceof El)) return; visit(node); node.children.forEach(c => walk(c, visit)); };
const find = (root, pred) => { const out = []; walk(root, n => { if (pred(n)) out.push(n); }); return out; };

// A fresh page: setup.js functions in one context. withApp: app.js has run (THEMES, theme, applyTheme exist).
function load(names, withApp) {
  const ctx = {
    Node, renders: [], painted: [], reloaded: false,
    document: {createElement: t => new El(t), querySelector: () => null},
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
const FUNCS = ['h', 'btn', 'cred', 'errAt', 'addressBox', 'qrDisclosure', 'startEdit', 'endEdit', 'openDeviceForm',
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

// ---- the QR code: one card, Copy the only action in row 1, a native disclosure that follows SU.qr ----
{
  const c = load(FUNCS, true);
  const url = 'http://192.168.1.5:80';
  const dets = tree => find(tree, n => n.tagName === 'details');
  const buttonsIn = n => find(n, x => x.tagName === 'button');
  c.SU.qr = false;
  let tree = c.addressBox({address: url, address_error: null});
  const card = find(tree, n => n.className === 'su-card-s')[0];
  check('qr: one address card holds the address row and the disclosure', !!card && card.children.length === 2);
  const row1 = card.children[0];
  check('qr: row 1 has Copy as its only button (data-focus copy-address)', buttonsIn(row1).length === 1 && buttonsIn(row1)[0].attrs['data-focus'] === 'copy-address' && buttonsIn(row1)[0].children.includes('Copy'));
  let det = dets(tree)[0];
  check('qr: collapsed by default when SU.qr is false (no open attribute)', !!det && det.attrs.open === undefined && det.className === 'su-more-d');
  check('qr: the summary names the disclosure and carries the qr-toggle focus key', find(det, n => n.tagName === 'summary' && n.attrs['data-focus'] === 'qr-toggle').length === 1
    && find(det, n => n.tagName === 'span' && n.textContent === 'QR code for a phone').length === 1);
  check('qr: no aria-expanded or aria-controls anywhere (the native disclosure carries its own state)', find(tree, n => n.attrs['aria-expanded'] !== undefined || n.attrs['aria-controls'] !== undefined).length === 0);
  const img = find(det, n => n.tagName === 'img')[0];
  check('qr: the image is the route with the address as cache key, 160px, lazy, alt names the address', !!img
    && img.attrs.src === '/api/setup/devices/qr?a=' + encodeURIComponent(url) && img.attrs.alt === 'QR code for ' + url
    && img.attrs.width === '160' && img.attrs.height === '160' && img.attrs.loading === 'lazy');
  check('qr: the body says what to do and the Wi-Fi rule, in the agreed classes', find(det, n => n.className === 'su-qr-do' && n.textContent === "Point your phone's camera at this code.").length === 1
    && find(det, n => n.className === 'su-hint' && n.textContent === 'Your phone must be on this Wi-Fi. Then allow it under Waiting for approval.').length === 1);
  const body = find(det, n => n.className === 'su-more-body su-qr')[0];
  check('qr: nothing in the body is a tab stop (the summary is the only one)', !!body && find(body, n => ['button', 'a', 'input', 'select', 'textarea'].includes(n.tagName) || n.attrs.tabindex !== undefined).length === 0);
  // SU.qr follows the native toggle (the browser fires toggle after open changes; the stub sets open and fires it)
  det.open = true; det.listeners.toggle();
  check('qr: opening the disclosure sets SU.qr (no re-render needed)', c.SU.qr === true);
  det.open = false; det.listeners.toggle();
  check('qr: closing it clears SU.qr', c.SU.qr === false);
  c.SU.qr = true;
  tree = c.addressBox({address: url, address_error: null});
  det = dets(tree)[0];
  check('qr: a redraw with SU.qr true renders the disclosure open', det.attrs.open !== undefined);
  c.SU.qr = false;
  tree = c.addressBox({address: url, address_error: null});
  check('qr: a redraw with SU.qr false renders it closed', dets(tree)[0].attrs.open === undefined);
  const img2 = find(dets(tree)[0], n => n.tagName === 'img')[0];
  img2.listeners.error();
  check('qr: when the image does not load, a hint replaces it (no broken picture)', !!img2.replacedBy && img2.replacedBy.className === 'su-hint'
    && img2.replacedBy.textContent === 'The QR code did not load. Type the address on your phone instead.');
  tree = c.addressBox({address: null, address_error: 'Kinora is not listening on your network.'});
  check('qr: no address, no card, no disclosure, and the state is reset', c.SU.qr === false && find(tree, n => n.className === 'su-card-s').length === 0
    && dets(tree).length === 0 && find(tree, n => n.className === 'su-foot-note' && n.textContent === 'Kinora is not listening on your network.').length === 1);
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
