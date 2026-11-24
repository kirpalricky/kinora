// Behavioural test of the Settings DOM code that the pure setup-logic test cannot reach: the Add a device draft, the
// edit error clearing, the theme fallbacks and the QR code panel. The named functions are read from static/setup.js and
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
    narrow: () => false, loadDevices() {}, copyAddress() {},
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
const FUNCS = ['h', 'btn', 'cred', 'errAt', 'addressBox', 'qrPanel', 'toggleQr', 'startEdit', 'endEdit', 'openDeviceForm',
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

// ---- the QR code panel: toggle label, aria-controls, the image, and the address error hides it ----
{
  const c = load(FUNCS, true);
  const url = 'http://192.168.1.5:80';
  const toggleOf = tree => find(tree, n => n.attrs['data-focus'] === 'qr-toggle')[0];
  c.SU.qr = false;
  let tree = c.addressBox({address: url, address_error: null});
  let t = toggleOf(tree);
  check('qr: collapsed by default, the toggle says Show and names no panel', t.children.includes('Show QR code') && t.attrs['aria-expanded'] === 'false' && t.attrs['aria-controls'] === undefined);
  check('qr: no panel while collapsed', find(tree, n => n.attrs.id === 'su-qr-panel').length === 0);
  c.SU.qr = true;
  tree = c.addressBox({address: url, address_error: null});
  t = toggleOf(tree);
  check('qr: open, the toggle says Hide and aria-controls names the panel', t.children.includes('Hide QR code') && t.attrs['aria-expanded'] === 'true' && t.attrs['aria-controls'] === 'su-qr-panel');
  const panel = find(tree, n => n.attrs.id === 'su-qr-panel')[0];
  const img = panel && find(panel, n => n.tagName === 'img')[0];
  check('qr: the panel holds an img of the route with the address as cache key, and the alt text names the address',
    !!img && img.attrs.src === '/api/setup/devices/qr?a=' + encodeURIComponent(url) && img.attrs.alt === 'QR code for ' + url);
  check('qr: the caption is there', !!panel && find(panel, n => n.textContent === 'Scan with a phone camera on the same Wi-Fi. The phone then shows up under Waiting for approval.').length === 1);
  tree = c.addressBox({address: null, address_error: 'Kinora is not listening on your network.'});
  check('qr: when the address is gone the panel and the toggle are gone and the state is reset', c.SU.qr === false && !toggleOf(tree) && find(tree, n => n.attrs.id === 'su-qr-panel').length === 0);
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
