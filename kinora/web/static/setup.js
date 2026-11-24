// First-run wizard and Settings (see docs/codemap/frontend.md). Classic script, no build step, strict CSP.
// Pure logic sits between the setup-logic markers and touches no DOM: tests/test_setup_logic.js reads it.
// Camera contact from this file: POST /api/setup/verify (one press = at most one gated login, one camera at a
// time) and POST /api/setup/reach (TCP connect only). Every other request is a save or a removal the user pressed.
// Passwords go from an input into one JSON request body; the input is cleared at once. Nothing secret is stored,
// logged or put in a URL.
// app.js shares this page's global scope: do not redeclare its top-level names ($, v, cam, W, ...).

/* setup-logic:begin */
// The sign-in is automatic (the app's own login with the TP-Link account); the explicit methods of older cameras are kept.
const SU_LOGIN = {auto: 'automatic', admin_cloud: 'Tapo account', cam_cloud: 'Camera account'};
const SU_PORTS = [[443, 'Camera API (443)'], [554, 'Video stream (554)'], [8800, 'Direct link (8800)']];
const SU_EMAIL = /^[^@\s]{1,64}@[^@\s]{1,253}\.[^@\s]{2,}$/;
const SU_NO_ANSWER = /not reachable|not answering|Max retries|Connection refused|timed out|Timeout|No route|NewConnectionError/;

function loginLabel(auth) { return SU_LOGIN[auth] || 'automatic'; }
function signInText(auth) { return 'Sign-in: ' + loginLabel(auth); }
function signInValue(auth) { return !auth || auth === 'auto' ? 'Automatic, with your TP-Link account' : 'Manual'; }
function isPlayable(cam) { return !!(cam && cam.enabled && cam.verified); }
function formatCountdown(sec) {
  const s = Math.max(0, Math.ceil(Number(sec) || 0)), m = Math.floor(s / 60), r = s % 60;
  return m ? `${m}:${String(r).padStart(2, '0')}` : `${r} s`;
}
function validateIp(s) {
  const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(String(s || '').trim());
  if (!m) return 'Enter an IPv4 address such as 192.168.1.50.';
  if (m.slice(1).some(o => +o > 255 || (o.length > 1 && o[0] === '0'))) return 'Each part of the address must be a number from 0 to 255.';
  return '';
}
function isPrivateIp(s) {
  const p = String(s || '').trim().split('.').map(Number);
  return p[0] === 10 || (p[0] === 172 && p[1] >= 16 && p[1] <= 31) || (p[0] === 192 && p[1] === 168);
}
function validateAddress(s) {
  const e = validateIp(s);
  if (e) return e;
  return isPrivateIp(s) ? '' : 'The address must be on your home network (10.x.x.x, 172.16-31.x.x or 192.168.x.x).';
}
function validateName(s) {
  const t = String(s || '').trim();
  if (!t) return 'Enter a name for the camera.';
  if (t.length > 60) return 'The name can be at most 60 characters.';
  return '';
}
function validateEmail(s) {
  const t = String(s || '').trim();
  return t.length <= 254 && SU_EMAIL.test(t) ? '' : 'Enter the email address of your TP-Link account.';
}
function validateSecret(s, label) {
  if (!s) return `Enter the ${label}.`;
  if (s.length > 256) return `The ${label} is too long (at most 256 characters).`;
  return '';
}
function validateAddForm(f) {
  const e = validateAddRow(f);
  return e.name || e.ip || e.user || e.password || '';
}
// Clip piece length and pan/tilt step: a number in range ('' = fine). whole: a whole number only.
function numberError(label, min, max, whole, raw) {
  const t = String(raw === undefined || raw === null ? '' : raw).trim(), v = Number(t);
  if (t === '' || !Number.isFinite(v)) return `${label} must be a number from ${min} to ${max}.`;
  if (v < min || v > max) return `${label} must be from ${min} to ${max}.`;
  if (whole && !Number.isInteger(v)) return `${label} must be a whole number.`;
  return '';
}
// Why a camera's Verify button must wait ('' = it may be pressed). One camera at a time; a refused login stops the run.
function verifyHold(cam, busyId, authStopId) {
  if (busyId) return busyId === cam.id ? 'Verifying…' : 'Another camera is being verified. Wait for it to finish.';
  if (authStopId && authStopId !== cam.id) return 'Stopped after a refused sign-in: change the TP-Link password first.';
  return '';
}
// Same rule as the server (setup.py _model_mismatch): a device that does not name the chosen model is flagged.
function modelMismatch(chosen, seen) {
  if (!chosen || chosen === 'other' || !seen) return false;
  return !String(seen).toUpperCase().includes(String(chosen).toUpperCase());
}
// The preset models a device can name. "other" has no preset.
const SU_MODELS = [['C100', 'C100'], ['C200', 'C200'], ['C210', 'C210'], ['other', 'Other (experimental)']];
// The preset model named in a model_seen string (for example "C200 1.0"), or '' when the device names none of them.
function seenModel(seen) {
  const s = String(seen || '').toUpperCase();
  const m = SU_MODELS.find(([v]) => v !== 'other' && s.includes(v));
  return m ? m[0] : '';
}
function modelPatchBody(model, apply) { return {model, apply_preset: !!apply}; }
// Body for applying the model the device reports (silently, after a Verify that found another model). null if none.
function modelFixBody(seen) { const code = seenModel(seen); return code ? modelPatchBody(code, true) : null; }
function addButtonLabel(n) { return n === 1 ? 'Add camera' : `Add ${n} cameras`; }
// TP-Link account (one account for every camera): email and password, both required. Returns {body, errors};
// body is null while any error is set.
function accountPlan(f) {
  const email = String(f.email || '').trim(), password = f.password || '';
  const errors = {email: validateEmail(email), password: validateSecret(password, 'TP-Link password')};
  return {body: Object.values(errors).some(Boolean) ? null : {email, password}, errors};
}
// The camera account (for Live view only) is optional. It counts as given when its password is typed; then the user
// name (default rtspadmin) must be filled too. Both are sent together or not at all.
function cameraAccountGiven(r) { return !!r.password; }
// One row of the add form (plain values; password included). Returns field -> message, {} when ready.
// Keys: name, ip, user, password.
function validateAddRow(r) {
  const e = {};
  const set = (k, m) => { if (m) e[k] = m; };
  set('name', validateName(r.name));
  set('ip', validateAddress(r.ip));
  if (cameraAccountGiven(r)) {
    const u = String(r.user || '').trim();
    set('user', !u ? 'Enter the camera account user name, or leave the camera account empty.'
      : (u.length > 64 ? 'The user name is too long (at most 64 characters).' : ''));
  }
  return e;
}
// Indexes of the rows still to add, in order. A row already added is never sent again.
function addBatchPlan(rows) { return rows.map((r, i) => i).filter(i => !rows[i].added); }
// Body of one POST /api/setup/cameras: name and address, and the camera account only when one is given.
function addBatchBody(r) {
  const body = {name: String(r.name || '').trim(), ip: String(r.ip || '').trim()};
  if (cameraAccountGiven(r)) { body.camera_user = String(r.user || '').trim(); body.camera_password = r.password; }
  return body;
}
// Sends the pending rows one by one. send(i, body) -> {ok, id} | {ok: false, text}; the first failure stops the batch.
// onAdded(i, id) runs after each success. Returns {stopped: index or -1, text}.
async function runAddBatch(rows, send, onAdded) {
  for (const i of addBatchPlan(rows)) {
    const res = await send(i, addBatchBody(rows[i]));
    if (!res || !res.ok) return {stopped: i, text: plainError(res && res.text)};
    onAdded(i, res.id);
  }
  return {stopped: -1, text: ''};
}
// A refusal of either password stops the run: the TP-Link one (shared) and the camera account one (cam_cloud).
function isAuthFailure(text) { return /refused the TP-Link password|refused the camera account password|Invalid authentication/.test(String(text || '')); }
// Confirmation copy for the dialogs: kind is remove, address or auto.
function dialogCopy(kind, name) {
  if (kind === 'remove') return {title: `Remove ${name}?`,
    body: 'Its saved passwords are deleted from this computer. Recordings on the SD card are not touched. You can add it again later.',
    ok: 'Remove camera', bad: true};
  if (kind === 'address') return {title: `Change ${name}'s address?`,
    body: `${name} stays off until you verify it at the new address.`, ok: 'Change address', bad: false};
  return {title: `Use automatic sign-in for ${name}?`,
    body: `${name} stays off until you verify it again. Nothing signs in now.`, ok: 'Use automatic sign-in', bad: false};
}
// Sentences the server writes for the user. When one is in the text, it is shown from there on.
const SU_PLAIN = ['The camera refused the TP-Link password', 'The camera refused the camera account password',
  'The camera changed how it signs in', 'Save your TP-Link account first'];
// After the TP-Link password is saved: earlier failed results are stale and the countdowns come from the server state.
function afterTplinkSaved() {
  for (const id of Object.keys(SU.vs)) if (SU.vs[id].kind === 'failed') delete SU.vs[id];
  SU.deadline = {}; SU.span = {};
  SU.authStop = '';
}
function plainSentence(t) {
  for (const s of SU_PLAIN) { const i = t.indexOf(s); if (i >= 0) return t.slice(i); }
  return '';
}
// The gate and session texts are for logs. The page shows a plain explanation and never the raw text.
function friendlyVerifyReason(text) {
  const t = String(text || '');
  let m;
  if (plainSentence(t)) return plainSentence(t);
  if (/no camera account password stored/.test(t))
    return 'This camera has no camera account, which only Live view needs. Add it in Settings to use Live view.';
  if ((m = /about (\d+) min left/.exec(t)))
    return `The camera paused sign-ins for about ${m[1]} minutes. It unlocks by itself; just wait.`;
  if (/locked itself|too many login/.test(t))
    return 'The camera paused sign-ins for about 30 minutes. It unlocks by itself; just wait.';
  if (/session slots|-71102|-71101|USER_ID_EMPLOYED|USER_ID_FULL/.test(t))
    return 'The camera has no free session. Close the Tapo app and other viewers, wait a few minutes, then verify again.';
  if (/budget/.test(t)) return 'The sign-in allowance for this camera is used up for this hour. Try again later.';
  if (SU_NO_ANSWER.test(t))
    return 'No answer from the camera. Check the address and that it\'s powered on and on this network.';
  if (/waiting \d+ s|last login was/.test(t))
    return 'Sign-ins are spaced out to protect your camera. You can verify again when the countdown ends.';
  if (/disabled/.test(t))
    return 'The app switched this camera off after a failed sign-in. Fix the password, then verify again after the wait.';
  return 'The sign-in did not succeed. Check the address and the sign-in details, then try again.';
}
// Errors from the other endpoints: plain text from the server. Anything that looks like a file or CLI hint is replaced.
function plainError(text) {
  const t = String(text || '').trim();
  if (!t) return 'Something went wrong. Try again.';
  if (/cameras\.json|python|--reset-auth|credentials-changed|\[gate\]|\.json/.test(t))
    return 'The app refused this change. The Settings page shows the current state.';
  return t;
}
// The state a camera card shows (wizard-ui-v2 section B table): one chip, at most one alert, one action.
// cam: a camera of /api/setup/state. v: this page's last Verify result for it ({kind: 'ok'|'failed'|'note', reason|text})
// or null. busy: its Verify is running. waiting: a sign-in wait is running. dot: sidebar colour. nav: its sidebar line
// ('' = none, the camera is Ready). action: {key, label, off} or null; off means the button stays disabled.
function camState(cam, v, busy, waiting) {
  const vf = v && v.kind === 'failed' ? String(v.reason || '') : '';
  const out = (key, chip, alert, action, nav) => ({key, chip, alert, action, nav,
    dot: chip.tone === 'ok' ? 'ok' : chip.tone === 'wait' ? 'wait' : chip.tone === 'bad' ? 'bad' : 'mut'});
  const tplink = {key: 'tplink', label: 'Change TP-Link password'};
  const again = {key: 'verifyagain', label: 'Verify again'};
  if (busy || cam.status === 'verifying')
    return out('verifying', {label: 'Verifying…', tone: 'wait'}, null, {key: 'verify', label: 'Verifying…', off: true}, 'Verifying…');
  if (waiting || cam.status === 'waiting')
    return out('waiting', {label: 'Wait', tone: 'wait', count: true}, null, {key: 'verify', label: 'Verify', off: true}, 'Waiting');
  // This page's own result comes first: right after a refusal the server already reports the gate pause, and the
  // card should still say what happened. After a reload the gate pause is the only state left.
  if (vf && isAuthFailure(vf))
    return out('refused', {label: 'Sign-in refused', tone: 'bad'},
      {tone: 'bad', text: "TP-Link refused the password. Other verifications are paused so your cameras don't lock."}, tplink, 'Sign-in refused');
  if (vf && /Save your TP-Link account first/.test(vf))
    return out('noaccount', {label: 'Not verified', tone: 'mut'}, {tone: 'warn', text: 'Save your TP-Link account first.'},
      {key: 'account', label: 'Add TP-Link account'}, 'Not verified');
  if (v && v.kind === 'note')
    return out('note', {label: 'Not verified', tone: 'mut'}, {tone: 'warn', text: v.text}, {key: 'verify', label: 'Verify'}, 'Not verified');
  if (vf && SU_NO_ANSWER.test(vf))
    return out('noanswer', {label: 'No answer', tone: 'bad'}, {tone: 'bad', text: "Check it's powered on and on this Wi-Fi."}, again, 'No answer');
  if (vf) return out('failed', {label: 'Verify failed', tone: 'bad'}, {tone: 'bad', text: friendlyVerifyReason(vf)}, again, 'Verify failed');
  if (cam.status === 'disabled_by_gate')
    return out('paused', {label: 'Paused', tone: 'bad'},
      {tone: 'bad', text: 'Paused after a refused sign-in, to avoid a 30-minute lockout. Change the password, then verify again.'}, tplink, 'Paused');
  if (cam.verified && cam.mac_mismatch)
    return out('mac', {label: 'Different device', tone: 'bad'}, {tone: 'bad', text: 'Another device answers at this address.'},
      {key: 'address', label: 'Edit address'}, 'Different device');
  if (cam.verified && !cam.enabled) return out('off', {label: 'Off', tone: 'mut'}, null, {key: 'turnon', label: 'Turn on'}, 'Off');
  if (cam.verified) return out('ok', {label: 'Ready', tone: 'ok'}, null, null, '');
  if (cam.status === 'failed')
    return out('lastfail', {label: 'Last sign-in failed', tone: 'bad'},
      {tone: 'bad', text: 'The last sign-in did not succeed. Check the details, then verify again.'}, again, 'Not verified');
  return out('new', {label: 'Not verified', tone: 'mut'}, null, {key: 'verify', label: 'Verify'}, 'Not verified');
}
// The Live view row (wizard-ui-v2 section G): none (not added), saved (user shown, password masked), refused by the
// camera for Live view, or the developer setting. Never a value.
function liveRow(cam) {
  const src = cam.secret_sources || {};
  if (src.camera === 'dev_override') return {mode: 'dev', user: '', refused: false};
  if (cam.has_camera_account) return {mode: 'saved', user: cam.camera_user || 'rtspadmin', refused: !!cam.live_refused};
  return {mode: 'none', user: '', refused: false};
}
// The camera Settings opens on: the first camera that needs attention, else the first camera; '' with no cameras.
function pickSelection(cams) {
  if (!cams.length) return '';
  const need = cams.find(c => camState(c, null, false, false).dot !== 'ok');
  return (need || cams[0]).id;
}
// The camera ids with `id` moved one place up (dir -1) or down (+1); the same list when it is already at that end.
function movedOrder(ids, id, dir) {
  const i = ids.indexOf(id), j = i + dir;
  if (i < 0 || j < 0 || j >= ids.length) return ids.slice();
  const out = ids.slice();
  out[i] = ids[j]; out[j] = ids[i];
  return out;
}
// Verify page title: "Verify your cameras" first; the counts once cameras are ready; "No camera is ready yet" after a try.
function verifyTitle(cams, anyTried) {
  const n = cams.filter(isPlayable).length;
  if (!cams.length) return 'Add a camera first';
  if (n === cams.length) return "You're all set";
  if (n) return `${n} of ${cams.length} cameras ready`;
  return anyTried ? 'No camera is ready yet' : 'Verify your cameras';
}
function verifyLead(cams) {
  const n = cams.filter(isPlayable).length, pend = cams.filter(c => !isPlayable(c));
  if (!cams.length) return 'Add a camera before you verify.';
  if (!pend.length) return 'Your cameras are ready. Open the timeline to browse the recordings.';
  if (n) return pend.length === 1
    ? `You can open the timeline now. ${pend[0].name} isn't verified yet: it stays off until you verify it.`
    : `You can open the timeline now. ${pend.length} cameras aren't verified yet: they stay off until you verify them.`;
  return 'Each verify signs in once. If a sign-in is refused, the app pauses so your cameras don\'t lock for 30 minutes.';
}
// Why "Open timeline" is off ('' when a camera is playable): the reason shown beside it.
function openReason(cams, busyName) {
  if (cams.some(isPlayable)) return '';
  return busyName ? `Verifying ${busyName}…` : 'Verify at least one camera to continue.';
}
// Local prerequisites from /api/setup/state. Each row: label, ok, the plain reason when not ok, and a link (text, href)
// that leads to the help. The page shows no file names, settings keys or install commands.
const SU_FFMPEG_URL = 'https://ffmpeg.org/download.html';
const SU_HELP_URL = '';   // the "How to fix" page for saved passwords (the owner sets it; no link is shown while empty)
function prereqRows(p) {
  const st = p.credential_store || {};
  return [
    {label: 'Video tools', ok: !!p.ffmpeg && !!p.ffprobe,
      fix: 'This computer is missing FFmpeg, a free video tool the app needs.', link: {text: 'How to install', href: SU_FFMPEG_URL}},
    {label: 'Saved passwords', ok: st.name === 'file' ? true : !!st.ok,
      fix: "Saved passwords can't be read on this computer.", link: {text: 'How to fix', href: SU_HELP_URL}},
  ];
}
// Other devices (Settings > Other devices). state: the body of GET /api/setup/devices.
const DEV_IP_MSG = "Enter the device's IP address, like 192.168.1.20";
function validateDeviceIp(s) { return validateIp(s) ? DEV_IP_MSG : ''; }
function validateDeviceName(s) { return String(s || '').trim().length > 60 ? 'The name can be at most 60 characters.' : ''; }
// "3 min ago" for a time in seconds before now.
function agoText(sec) {
  const s = Math.max(0, Math.floor(Number(sec) || 0));
  if (s < 60) return 'a moment ago';
  if (s < 3600) return Math.floor(s / 60) + ' min ago';
  if (s < 86400) { const hr = Math.floor(s / 3600); return hr + (hr === 1 ? ' hour ago' : ' hours ago'); }
  const d = Math.floor(s / 86400); return d + (d === 1 ? ' day ago' : ' days ago');
}
// What is on screen: a redraw is needed only when this changes (a waiting device's last-seen time is painted apart).
function devicesKey(state) {
  if (!state) return '';
  return JSON.stringify([state.sharing, state.listening, state.address, state.address_error, state.from_command_line,
    state.devices, (state.pending || []).map(p => p.ip), state.command_line]);
}
function deviceLabel(d) { return d.name || d.ip; }
/* setup-logic:end */

// ---------- DOM: wizard and Settings. Text goes in with textContent only; no innerHTML with data. ----------
// SU.view: a wizard step (prepare, account, add, verify) or settings. SU.sel: the Settings pane: a camera id, 'new',
// 'account', 'computer', 'about' or ''. SU.edit: the one row in edit mode ('name:id', 'addr:id', 'live:id', 'acct').
// SU.dev: the last GET /api/setup/devices answer (Other devices); SU.devForm: its Add a device draft, kept across redraws.
// SU.opener: the data-focus key of the control that opened the edit. SU.errs: messages by key. SU.saved: the key
// that shows "Saved" once. SU.vs: this page's last Verify result per camera. SU.deadline and SU.span: sign-in waits.
const SU = {state: null, playable: false, view: 'prepare', changed: false, busy: '', vs: {}, deadline: {}, span: {},
  authStop: '', add: null, uid: 0, sel: '', prevSel: '', newRow: null, edit: '', opener: '', errs: {}, saved: '',
  screen: 'list', loadErr: '', dev: null, devForm: {ip: '', name: ''}, devDirty: false, devErr: '', qr: false};
const byId = id => document.getElementById(id);
const SVG_NS = 'http://www.w3.org/2000/svg';
// Icons: 20px on a 24 grid, stroke currentColor, no fill. Built with createElementNS (h() cannot make SVG).
const SU_ICONS = {
  check: [['path', {d: 'M5 12.5l4.5 4.5L19 7.5'}]],
  cross: [['path', {d: 'M6.5 6.5l11 11M17.5 6.5l-11 11'}]],
  lock: [['rect', {x: 5, y: 10.5, width: 14, height: 10, rx: 2.5}], ['path', {d: 'M8 10.5V8a4 4 0 0 1 8 0v2.5'}], ['path', {d: 'M12 14.5h.01'}]],
  alert: [['path', {d: 'M21 12a9 9 0 1 1-18 0a9 9 0 1 1 18 0z'}], ['path', {d: 'M12 8v5'}], ['path', {d: 'M12 16.5h.01'}]],
  chevron: [['path', {d: 'M9 6l6 6-6 6'}]],
  back: [['path', {d: 'M15 6l-6 6 6 6'}]],
  plus: [['path', {d: 'M12 5v14M5 12h14'}]],
  up: [['path', {d: 'M6 14l6-6 6 6'}]],
  down: [['path', {d: 'M6 10l6 6 6-6'}]],
  pencil: [['path', {d: 'M4 20h4L19 9l-4-4L4 16v4z'}]],
  mark: [['rect', {class: 'su-mk-b', x: 3, y: 3, width: 14, height: 14, rx: 4}], ['rect', {class: 'su-mk-f', x: 7, y: 7, width: 14, height: 14, rx: 4}]],
};
const SU_CHECKLIST = [
  ['Turn on Third-Party Compatibility', 'Me → Third-Party Services.'],
  ['Know your TP-Link email and password', 'The ones you use in the Tapo app.'],
  ['Optional: a camera account for Live view', 'Made per camera in the Tapo app. You can add it later.'],
];
const SU_WIZARD = [['prepare', 'Prepare'], ['account', 'Accounts'], ['add', 'Add camera'], ['verify', 'Verify']];
const SU_LEGAL = 'Unofficial software. Not made by, affiliated with or endorsed by TP-Link or Tapo, and not supported by TP-Link. Use at your own risk.';

function h(tag, props, ...kids) {
  const e = document.createElement(tag);
  for (const k in (props || {})) {
    const v = props[k];
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') e.className = v;
    else if (k === 'text') e.textContent = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? '' : String(v));
  }
  for (const c of kids.flat(Infinity)) if (c !== undefined && c !== null && c !== false) e.append(c instanceof Node ? c : String(c));
  return e;
}
function icon(name, cls) {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('class', 'su-ico' + (cls ? ' ' + cls : ''));
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  for (const [tag, attrs] of SU_ICONS[name]) {
    const el = document.createElementNS(SVG_NS, tag);
    for (const k in attrs) el.setAttribute(k, String(attrs[k]));
    svg.append(el);
  }
  return svg;
}
// btn(label, onclick, cls, props): cls is '' (outlined), 'go' (the one primary), 'ghost', 'bad', 'bad-o', 'sm'.
function btn(label, onclick, cls, props) {
  return h('button', {type: 'button', class: 'su-btn' + (cls ? ' ' + cls : ''), onclick, ...(props || {})}, label);
}
// Field: label above, input, optional hint, and an error line (empty until setErr puts a message in it).
function labelled(text, input, hint) {
  const id = 'su-f' + (++SU.uid);
  input.id = id;
  if (hint) input.setAttribute('aria-describedby', id + '-h');
  return h('div', {class: 'su-field'}, h('label', {for: id, text}), input,
    hint ? h('small', {class: 'su-hint', id: id + '-h', text: hint}) : null,
    h('p', {class: 'su-err', id: id + '-e'}));
}
// Marks one field as wrong (aria-invalid, error text linked with aria-describedby) or clears it.
function setErr(input, msg) {
  const box = input.closest('.su-field'), err = box && box.querySelector('.su-err');
  if (!err) return;
  const hint = box.querySelector('.su-hint');
  err.textContent = msg || '';
  box.classList.toggle('su-invalid', !!msg);
  if (msg) input.setAttribute('aria-invalid', 'true'); else input.removeAttribute('aria-invalid');
  const ids = [hint && hint.id, msg && err.id].filter(Boolean);
  if (ids.length) input.setAttribute('aria-describedby', ids.join(' ')); else input.removeAttribute('aria-describedby');
}
function watch(input) { input.addEventListener('input', () => setErr(input, '')); }
// Footer of a wizard step: Back (ghost) on the left and the actions on the right. renderView moves it into #setupfoot.
function navRow(left, ...right) {
  return h('div', {class: 'su-nav'}, left ? h('div', {class: 'su-nav-l'}, left) : null, h('div', {class: 'su-nav-r'}, ...right));
}
// Status chip. until (ms) adds a countdown that tickCountdowns updates once a second.
function chipEl(chip, until) {
  return h('span', {class: 'su-chip ' + chip.tone},
    chip.label, until ? [' ', h('span', {'data-until': String(until), 'aria-hidden': 'true', text: formatCountdown((until - Date.now()) / 1000)})] : null);
}
// Inline alert: tone is ok | warn | bad. Icon, optional bold title, body nodes.
function alertBox(tone, iconName, title, body, props) {
  const p = props || {};
  return h('div', {...p, class: ['su-alert', tone, p.class].filter(Boolean).join(' ')},
    icon(iconName),
    h('div', {class: 'su-alert-body'}, title ? h('p', {class: 'su-alert-t', text: title}) : null, body));
}
// The message kept under a field or an object, by key (SU.errs). Read by the editor that owns the key.
function errAt(key) { return SU.errs[key] ? h('p', {class: 'su-err', text: SU.errs[key]}) : null; }
// "Saved" shown once next to the row that was saved; it clears itself after 3 s.
function savedTag(key) {
  if (SU.saved !== key) return null;
  const tag = h('span', {class: 'su-saved', role: 'status', text: 'Saved'});
  setTimeout(() => { if (SU.saved === key) SU.saved = ''; tag.remove(); }, 3000);
  return tag;
}
// One row of a saved-credential box: label, value (text or node), and the action on the right.
function cred(label, value, action, extra) {
  return h('div', {class: 'su-cred' + (extra ? ' ' + extra : '')}, h('span', {class: 'su-cred-l', text: label}),
    h('div', {class: 'su-cred-v'}, value), action ? h('div', {class: 'su-cred-a'}, action) : null);
}
function narrow() { return typeof matchMedia === 'function' && matchMedia('(max-width: 719px)').matches; }

// fetch helper: JSON in, JSON out; error bodies are plain text and are returned as text.
async function api(method, url, body) {
  const opt = {method, headers: {}};
  if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
  let r;
  try { r = await fetch(url, opt); } catch (e) { return {ok: false, text: 'Lost contact with the local server.'}; }
  if (!r.ok) {
    const t = (await r.text().catch(() => '')).trim();
    return {ok: false, status: r.status, text: t || `The server answered with HTTP ${r.status}.`};
  }
  return {ok: true, data: await r.json().catch(() => null)};
}
// Re-reads /api/setup/state after each action (never on a timer: the server shells out to `security` on macOS).
// The countdown deadline only ever moves later here: a deadline from a verify answer is not wiped by a refresh.
function absorb(data) {
  SU.state = data;
  data.cameras.forEach(c => {
    if (c.wait_s > 0) {
      if (!(SU.deadline[c.id] > Date.now())) SU.span[c.id] = c.wait_s * 1000;   // length of the wait, for the draining bar
      SU.deadline[c.id] = Math.max(SU.deadline[c.id] || 0, Date.now() + c.wait_s * 1000);
    }
  });
  SU.playable = data.cameras.some(isPlayable);
}
async function refresh() {
  const r = await api('GET', '/api/setup/state');
  const good = r.ok && r.data && Array.isArray(r.data.cameras);
  if (good) absorb(r.data);
  return good;
}
// The one toast of the page: success only (Saved is inline). Undo keeps it longer. Hover pauses the timer.
let suToastT = 0;
function toast(text, undo) {
  const box = byId('sutoast');
  clearTimeout(suToastT);
  box.replaceChildren(h('span', {text}), ...(undo ? [btn('Undo', () => { box.replaceChildren(); clearTimeout(suToastT); undo(); }, 'ghost sm')] : []));
  const hide = () => { box.replaceChildren(); };
  suToastT = setTimeout(hide, undo ? 6000 : 4000);
  box.onmouseenter = () => clearTimeout(suToastT);
  box.onmouseleave = () => { suToastT = setTimeout(hide, 2000); };
  box.onfocusin = () => clearTimeout(suToastT);   // focus inside the toast (Undo) keeps it until the user acts
}

// ---------- navigation ----------
function go(view) {
  if (view === 'done') view = 'verify';   // the last step is Verify (one screen for the result)
  SU.view = view;
  if (view !== 'add') SU.add = null;
  if (view !== 'settings') SU.newRow = null;
  SU.edit = ''; SU.opener = ''; SU.errs = {}; SU.saved = ''; SU.devForm = {ip: '', name: ''};
  renderView(view === 'settings' ? `[data-focus="side:${SU.sel}"],[data-focus="pane-title"]` : 'h2');
}
// Progress: caption and four segments (wizard only).
function renderNav() {
  const nav = byId('sunav'), idx = SU_WIZARD.findIndex(([k]) => k === SU.view), wiz = idx >= 0;
  nav.hidden = !wiz;
  nav.replaceChildren(...(wiz ? [
    h('p', {class: 'su-caption', text: `Step ${idx + 1} of ${SU_WIZARD.length} · ${SU_WIZARD[idx][1]}`}),
    h('ol', {class: 'su-seg-list'}, SU_WIZARD.map(([k, l], i) => h('li', {class: 'su-seg' + (i <= idx ? ' on' : ''),
      'aria-current': k === SU.view ? 'step' : undefined},
      h('span', {class: 'sr', text: `Step ${i + 1} of ${SU_WIZARD.length}: ${l}`})))),
  ] : []));
}
// The page header: the wizard's brand, or Settings' back button and title (the back button reads Timeline, Setup or
// Settings, the last one on a phone's detail screen).
function renderTop() {
  const settings = SU.view === 'settings', detail = settings && SU.screen === 'detail' && narrow();
  byId('suwrap').classList.toggle('settings', settings);
  byId('sumark').hidden = settings;
  byId('setuptitle').textContent = settings ? (detail ? paneTitle() : 'Settings') : 'Camera setup';
  const b = byId('setupback');
  b.hidden = !settings;
  // Detail screen on a phone: the familiar "< Settings" back. Otherwise a "Done" button on the right, like a settings dialog.
  b.classList.toggle('ghost', detail);
  b.classList.toggle('su-done', settings && !detail);
  if (!settings) return;
  if (detail) { b.removeAttribute('aria-label'); b.replaceChildren(icon('back'), h('span', {text: 'Settings'})); }
  else {
    b.setAttribute('aria-label', SU.playable ? 'Done, back to the timeline' : 'Back to setup');
    b.replaceChildren(h('span', {text: SU.playable ? 'Done' : 'Back to setup'}));
  }
}
// The title of the Settings pane (on a phone's detail screen the header shows it): the camera's name or the item's name.
function paneTitle() {
  const c = SU.state && SU.state.cameras.find(x => x.id === SU.sel);
  return c ? c.name : ({new: 'New camera', account: 'TP-Link account', computer: 'This computer', devices: 'Other devices', about: 'About this app'}[SU.sel] || 'Settings');
}
// A help link for a prerequisite row (external, opens in a new tab); null when the row has none.
function fixLink(r) {
  return r.link && r.link.href ? h('a', {class: 'su-link', href: r.link.href, target: '_blank', rel: 'noopener noreferrer', text: r.link.text}) : null;
}
// Re-renders the current view. focus: a selector (or several, comma-separated, first found wins). Without it the
// control that had focus, if it has a data-focus key, gets focus back.
function pickFocus(root, focus) {
  for (const s of String(focus).split(',')) { const t = root.querySelector(s.trim()); if (t) return t; }
  return null;
}
function renderView(focus) {
  const root = byId('setup'), m = byId('setupmain'), foot = byId('setupfoot');
  const active = document.activeElement, inside = !!active && active !== root && root.contains(active);
  const key = inside && active.dataset ? active.dataset.focus : '';
  const sec = VIEWS[SU.view]();
  if (SU.loadErr && SU.view !== 'settings') sec.prepend(alertBox('bad', 'alert', null, h('p', {text: SU.loadErr}), {role: 'alert'}));
  const f = sec.querySelector(':scope > .su-nav');
  m.replaceChildren(sec);
  foot.replaceChildren(...(f ? [f] : []));
  renderTop();
  renderNav();
  byId('settingsbtn').hidden = !SU.playable;
  let target = focus ? pickFocus(root, focus) : null;
  if (!target && key) target = root.querySelector(`[data-focus="${key}"]`);
  if (!target && focus === undefined && inside) target = m.querySelector('h2');
  if (target) {
    if (!target.hasAttribute('tabindex') && !/^(A|BUTTON|INPUT|SELECT|TEXTAREA|SUMMARY)$/.test(target.tagName)) target.tabIndex = -1;
    target.focus();
  }
}
function openSetup(view, sel) {
  byId('app').hidden = true;
  byId('setup').hidden = false;
  if (view === 'settings') {
    if (sel) SU.sel = sel;
    SU.screen = sel && narrow() ? 'detail' : 'list';
  }
  go(view);
  if (view === 'settings') loadDevices();
}
function closeSetup() {
  if (SU.changed) { location.reload(); return; }   // the timeline reads cameras.json at start: reload to show the new list
  if (!SU.playable) return;
  SU.devForm = {ip: '', name: ''};
  byId('setup').hidden = true;
  byId('app').hidden = false;
  const gear = byId('settingsbtn');
  if (gear) gear.focus();
}
// Header back button: the phone's detail screen goes back to the list; else the timeline (playable) or the setup.
function topBack() {
  if (SU.view === 'settings' && SU.screen === 'detail' && narrow()) return backToList();
  if (SU.playable) return closeSetup();
  go(SU.state && SU.state.cameras.length ? 'verify' : 'prepare');
}
// Phone: the detail screen was entered with pushState, so the system back gesture comes back here (popstate, which
// then finds the list already shown and does nothing).
function backToList() {
  const entered = history.state && history.state.su === 'detail';
  SU.screen = 'list';
  renderView(`[data-focus="side:${SU.sel}"]`);
  if (entered) history.back();
}
function onPop(e) {
  if (SU.view !== 'settings' || SU.screen !== 'detail') return;
  if (e.state && e.state.su === 'detail') return;
  SU.screen = 'list';
  renderView(`[data-focus="side:${SU.sel}"]`);
}
// Escape: the dialog (native) first, then the row in edit mode, then (desktop, playable) close Settings.
function onSetupKey(e) {
  if (e.key !== 'Escape' || byId('suconfirm').open) return;
  if (SU.edit) { e.preventDefault(); endEdit(); return; }
  if (SU.view === 'settings' && !narrow() && SU.playable) closeSetup();
}
// Settings sidebar: Up and Down move between the rows (roving focus); Enter and Space come from the buttons.
function sideKeys(e) {
  if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
  const rows = [...e.currentTarget.querySelectorAll('.su-row')], i = rows.indexOf(document.activeElement);
  if (i < 0) return;
  e.preventDefault();
  rows[(i + (e.key === 'ArrowDown' ? 1 : -1) + rows.length) % rows.length].focus();
}
// Theme: the same key and cycle as the app bar's Theme button (app.js applyTheme, nextTheme, THEMES, theme).
// app.js owns the theme; if it stopped before defining it, these fall back to Auto so Settings still renders.
function themeNow() { try { return theme; } catch (e) { return 'system'; } }
function themeChoices() { try { return THEMES; } catch (e) { return ['system', 'light', 'dark']; } }
function themeWord(t) {
  try { return SU_THEME_NAME[t]; } catch (e) { return {system: 'Auto', light: 'Light', dark: 'Dark'}[t]; }
}
function setTheme(t) {
  try { theme = t; } catch (e) {}
  try { localStorage.setItem('theme', t); } catch (e) {}
  if (typeof applyTheme === 'function') applyTheme(t);
  renderView(`[data-focus="theme:${t}"]`);
}

// ---------- views ----------
const VIEWS = {prepare: viewPrepare, account: viewAccount, add: viewAdd, verify: viewVerify, settings: viewSettings};
function viewPrepare() {
  const rows = prereqRows(SU.state.prerequisites), bad = rows.filter(r => !r.ok);
  return h('section', {class: 'su-view'},
    h('h2', {class: 'su-display', text: 'Before you start', tabindex: '-1'}),
    h('p', {class: 'su-lead', text: 'Do these in the Tapo app first.'}),
    h('ol', {class: 'su-checklist'}, SU_CHECKLIST.map(([t, d], i) =>
      h('li', {}, h('span', {class: 'su-num', text: String(i + 1)}), h('span', {}, h('strong', {text: t}), h('span', {class: 'su-detail', text: d}))))),
    h('div', {class: 'su-stack'},
      h('h3', {class: 'su-h3', text: 'This computer'}),
      bad.length
        ? h('ul', {class: 'su-status-rows'}, bad.map(r => h('li', {},
            h('div', {class: 'su-srow-top'}, h('span', {class: 'su-srow-main'}, icon('cross', 'su-bad'), h('span', {text: r.label})),
              chipEl({tone: 'bad', label: 'Missing'})),
            h('p', {class: 'su-detail', text: r.fix}, ' ', fixLink(r)))))
        : h('p', {class: 'su-srow-main'}, icon('check', 'su-ok'), h('span', {text: 'This computer is ready.'}))),
    h('p', {class: 'su-privacy'}, icon('lock'),
      h('span', {text: 'Passwords stay on this computer. They go only to your cameras and to TP-Link when the app signs in.'})),
    h('div', {class: 'su-more'},
      h('details', {}, h('summary', {}, h('span', {text: 'How we protect your cameras'}), icon('chevron', 'su-chev')),
        h('p', {class: 'su-more-body', text: 'Cameras lock for about 30 minutes after repeated failed sign-ins. This app signs in once per press of Verify and stops at the first refusal.'})),
      h('details', {}, h('summary', {}, h('span', {text: 'Good to know'}), icon('chevron', 'su-chev')),
        h('p', {class: 'su-more-body', text: "TP-Link can change or break third-party access at any time. A camera can't use SD-card recording, Tapo Care and an NVR all at once: two at most."}))),
    h('p', {class: 'su-foot-note', text: SU_LEGAL}),
    navRow(null, btn('Continue', () => go('account'), 'go')));
}
// TP-Link account: one saved-credential row when saved; the form when not saved or when changing.
function viewAccount() {
  const acc = SU.state.account || {}, saved = !!acc.email_set, editing = !saved || SU.edit === 'acct', FORM = 'su-acct-form';
  const box = tplinkBox({formId: FORM, saved, onDone: async () => {
    afterTplinkSaved();
    await refresh();
    if (!saved) return go('add');
    SU.edit = ''; SU.saved = 'acct';
    renderView('[data-focus="acct-change"]');
  }, onCancel: endEdit});
  const editingNow = saved && editing;
  return h('section', {class: 'su-view'},
    h('h2', {class: 'su-title', text: 'TP-Link account', tabindex: '-1'}),
    h('p', {class: 'su-lead', text: 'Signs in to all your cameras. Use the account that owns them.'}),
    saved && !editing
      ? acctRows()
      : h('div', {class: 'su-card-s su-pad'}, box.form, editingNow ? h('div', {class: 'su-bar'}, box.cancelBtn, box.submit) : null),
    navRow(btn('Back', () => go('prepare'), 'ghost'),
      saved && !editing ? btn('Continue', () => go('add'), 'go') : (saved ? null : box.submit)));
}
// The TP-Link form. saved: the box shows the email and asks for the password again. The submit button is returned
// separately so the caller can put it in the footer (form attribute) or inside the box.
function tplinkBox(o) {
  const acc = SU.state.account || {}, saved = !!o.saved;
  const email = h('input', {type: 'email', autocomplete: 'username', maxlength: 254, value: saved ? (acc.email || '') : '',
    'data-focus': 'edit:acct'});
  const pw = h('input', {type: 'password', autocomplete: 'new-password', maxlength: 256});
  const msg = h('p', {class: 'su-msg', role: 'status'});
  const submit = btn(saved ? 'Save' : 'Save and continue', null, 'go', {type: 'submit', form: o.formId});
  watch(email); watch(pw);
  const form = h('form', {class: 'su-stack', id: o.formId, novalidate: true, onsubmit: async e => {
    e.preventDefault();
    const plan = accountPlan({email: email.value, password: pw.value});
    setErr(email, plan.errors.email); setErr(pw, plan.errors.password);
    msg.textContent = '';
    if (!plan.body) { (plan.errors.email ? email : pw).focus(); return; }
    pw.value = '';   // the password leaves the page with the request
    submit.disabled = true; msg.textContent = 'Saving…';
    const r = await api('POST', '/api/setup/account', plan.body);
    submit.disabled = false;
    if (!r.ok) { msg.textContent = plainError(r.text); return; }
    afterTplinkSaved();
    await o.onDone();
  }},
    labelled('TP-Link account email', email),
    labelled('TP-Link password', pw, saved ? 'Enter the password to save the change.' : 'Kept on this computer only.'),
    msg);
  return {form, submit, cancelBtn: btn('Cancel', () => { if (o.onCancel) o.onCancel(); }, 'ghost')};
}
// The saved TP-Link account as a credential box: email, password masked, Change.
function acctRows() {
  const acc = SU.state.account || {};
  return h('div', {class: 'su-card-s'},
    cred('Email', [acc.email || '', savedTag('acct')], btn('Change', () => startEdit('acct', 'acct-change'), 'ghost sm', {'data-focus': 'acct-change'})),
    cred('Password', h('span', {class: 'su-mask', role: 'img', 'aria-label': 'Password saved', text: '••••••••'})));
}
// Add cameras (wizard): a list of rows (name, address, and an optional camera account for Live view). Rows are DOM
// objects kept in SU.add.rows, so a re-render keeps what was typed. Added rows are locked.
function viewAdd() {
  const add = SU.add || (SU.add = {rows: [], busy: false, checking: false, msg: ''});
  if (!add.rows.length) add.rows.push(newAddRow());
  numberRows(add.rows);
  const pending = addBatchPlan(add.rows).length, added = add.rows.filter(r => r.added);
  return h('section', {class: 'su-view'},
    h('h2', {class: 'su-title', text: 'Add your cameras', tabindex: '-1'}),
    h('p', {class: 'su-lead', text: 'Name each camera and enter its address. You can add more later.'}),
    discoverBox(add),
    h('div', {class: 'su-stack', id: 'su-rows'}, add.rows.map(r => r.el)),
    add.msg ? h('p', {class: 'su-msg', role: 'status', text: add.msg}) : null,
    h('div', {class: 'su-bar'}, btn('Add another camera', addAnotherRow, '', {disabled: add.busy})),
    navRow(btn('Back', () => go('account'), 'ghost'),
      pending && added.length ? btn('Continue to Verify', () => go('verify'), '') : null,
      pending ? btn(addButtonLabel(pending), addBatch, 'go', {disabled: add.busy})
        : btn('Continue to Verify', () => go('verify'), 'go')));
}
// Find cameras on a subnet (login-free TCP check on the server). The subnet field is prefilled with this computer's
// network once. "Use" puts a found address into the first empty Address field, or into a new row.
function discoverBox(add) {
  const d = add.disc || (add.disc = {subnet: null, busy: false, found: null, scanned: 0, err: '', used: new Set()});
  if (d.subnet === null) {
    d.subnet = '';
    api('GET', '/api/setup/discover').then(r => {
      if (r.ok && r.data && r.data.subnet && !d.subnet) { d.subnet = r.data.subnet; if (d.input) d.input.value = d.subnet; }
    });
  }
  const input = d.input = h('input', {inputmode: 'decimal', autocomplete: 'off', placeholder: '192.168.1.0/24', value: d.subnet, 'data-focus': 'disc-net'});
  input.addEventListener('input', () => { d.subnet = input.value; setErr(input, ''); });
  const go = btn(d.busy ? 'Searching…' : 'Search', () => runDiscover(add, input), '', {disabled: d.busy, 'data-focus': 'disc-go'});
  return h('details', {class: 'su-more-d', open: !!(d.found || d.err || d.busy)},
    h('summary', {}, h('span', {text: 'Find cameras on my network'}), icon('chevron', 'su-chev')),
    h('div', {class: 'su-stack su-more-body'},
      h('p', {class: 'su-detail', text: 'Looks for cameras at every address of a network. It only checks which addresses answer and never signs in.'}),
      labelled('Network', input, 'Your router usually uses 192.168.1.0/24. Up to 1,000 addresses.'),
      h('div', {class: 'su-bar'}, go),
      d.busy ? h('p', {class: 'su-busy', role: 'status'}, h('span', {class: 'su-spin', 'aria-hidden': 'true'}), h('span', {text: 'Searching…'})) : null,
      d.err ? h('p', {class: 'su-err', role: 'alert', text: d.err}) : null,
      d.found && !d.busy ? discoverResults(add, d) : null));
}
function discoverResults(add, d) {
  if (!d.found.length) return h('p', {class: 'su-note', role: 'status',
    text: `No cameras answered among ${d.scanned} addresses. Check they are on and on this Wi-Fi, or enter the address yourself.`});
  return h('div', {class: 'su-stack', role: 'status'},
    h('p', {class: 'su-detail', text: `Found ${d.found.length} possible ${d.found.length === 1 ? 'camera' : 'cameras'}.`}),
    h('ul', {class: 'su-status-rows', 'aria-label': 'Found on your network'}, d.found.map(f => h('li', {},
      h('span', {class: 'su-srow-main'}, icon('check', 'su-ok'), h('span', {text: f.ip})),
      d.used.has(f.ip) ? h('span', {class: 'su-ok', text: 'Added below'})
        : btn('Use', () => { useFound(add, d, f.ip); }, 'ghost sm', {'aria-label': 'Use ' + f.ip})))));
}
function useFound(add, d, ip) {
  let row = add.rows.find(r => !r.added && !r.ip.value.trim());
  if (!row) { row = newAddRow(); add.rows.push(row); }
  row.ip.value = ip; setErr(row.ip, '');
  d.used.add(ip);
  renderView('#' + row.id + ' input');
}
async function runDiscover(add, input) {
  const d = add.disc;
  if (d.busy) return;
  d.err = ''; d.found = null; d.busy = true;
  const body = d.subnet.trim() ? {subnet: d.subnet.trim()} : {};
  renderView('[data-focus="disc-net"]');
  const r = await api('POST', '/api/setup/discover', body);
  d.busy = false;
  if (r.ok && r.data && Array.isArray(r.data.found)) {
    d.found = r.data.found; d.scanned = r.data.scanned || 0; d.used = new Set();
    if (r.data.subnet) d.subnet = r.data.subnet;
  } else d.err = plainError(r.text);
  renderView('[data-focus="disc-go"]');
}
// One row of the add form. single: the Settings pane's new camera (no head, no reachability).
function newAddRow(single) {
  const id = 'su-row-' + (++SU.uid);
  const name = h('input', {maxlength: 60, autocomplete: 'off', required: true});
  const ip = h('input', {inputmode: 'decimal', autocomplete: 'off', placeholder: '192.168.1.50', required: true});
  const user = h('input', {value: 'rtspadmin', autocomplete: 'off', maxlength: 64});
  const pw = h('input', {type: 'password', autocomplete: 'new-password', maxlength: 256});
  watch(name); watch(ip); watch(user); watch(pw);
  const row = {id, single: !!single, name, ip, user, pw, added: null};
  row.title = h('h3', {class: 'su-name'});
  row.badge = h('span');
  row.removeBtn = btn('Remove', () => removeAddRow(row), 'ghost sm');
  row.rowMsg = h('p', {class: 'su-err', role: 'alert'});
  row.reachEl = h('div', {class: 'su-stack'});
  row.live = h('details', {class: 'su-more-d'}, h('summary', {}, h('span', {text: 'Live view (optional)'}), icon('chevron', 'su-chev')),
    h('div', {class: 'su-stack su-more-body'},
      h('p', {class: 'su-detail', text: 'The camera account for Live view. Created per camera in the Tapo app. You can add it later.'}),
      labelled('Camera account user name', user, 'Default rtspadmin. Change it only if you changed it on the camera.'),
      labelled('Camera account password', pw)));
  const fields = h('div', {class: 'su-stack'},
    labelled('Camera name', name, 'For example: Kitchen'),
    labelled('Address', ip, 'In the Tapo app under Device info, or in your router.'));
  row.el = single
    ? h('div', {class: 'su-stack', id}, fields, row.live, row.rowMsg)
    : h('article', {class: 'su-card-s su-pad', id},
      h('div', {class: 'su-cam-head'}, row.title, h('div', {class: 'su-cam-tools'}, row.badge, row.removeBtn)),
      fields, row.live, row.rowMsg, row.reachEl);
  return row;
}
function numberRows(rows) {
  rows.forEach((r, i) => { if (!r.single) { r.title.textContent = `Camera ${i + 1}`; r.removeBtn.hidden = rows.length < 2 || !!r.added; } });
}
function addAnotherRow() {
  const r = newAddRow();
  SU.add.rows.push(r);
  renderView('#' + r.id + ' input');
}
function removeAddRow(r) {
  SU.add.rows = SU.add.rows.filter(x => x !== r);
  renderView('#su-rows input');
}
// Validates every pending row first (nothing is sent if one is wrong), then sends them in order and stops at the first
// error. Then each added camera gets its reachability check (login-free).
async function addBatch() {
  const add = SU.add;
  if (!add || add.busy) return;
  const rows = add.rows;
  const data = rows.map(r => ({name: r.name.value, ip: r.ip.value, user: r.user.value, password: r.pw.value, added: !!r.added}));
  let focusEl = null;
  rows.forEach((r, i) => {
    if (r.added) return;
    const e = validateAddRow(data[i]);
    setErr(r.name, e.name || ''); setErr(r.ip, e.ip || ''); setErr(r.user, e.user || '');
    r.rowMsg.textContent = '';
    if (e.user) r.live.open = true;   // the camera account sits inside its closed part
    const first = [[r.name, e.name], [r.ip, e.ip], [r.user, e.user]].find(x => x[1]);
    if (first && !focusEl) focusEl = first[0];
  });
  if (focusEl) { focusEl.focus(); return; }
  if (!addBatchPlan(rows).length) return;
  add.busy = true; add.msg = 'Adding the cameras…';
  renderView('h2');
  const res = await runAddBatch(data, async (i, body) => {
    rows[i].pw.value = '';   // the password leaves the page with its request
    const r = await api('POST', '/api/setup/cameras', body);
    if (!r.ok) return {ok: false, text: plainError(r.text)};
    if (!r.data || !r.data.id) return {ok: false, text: 'The app did not confirm that the camera was saved. Check Settings before adding it again.'};
    return {ok: true, id: r.data.id};
  }, (i, id) => {
    SU.changed = true;
    const r = rows[i], d = data[i];
    r.added = {id, name: d.name.trim(), ip: d.ip.trim(), ports: null, err: ''};
    [r.name, r.ip, r.user, r.pw].forEach(x => { x.disabled = true; });
    r.badge.replaceChildren(chipEl({tone: 'ok', label: 'Added'}));
  });
  if (res.stopped >= 0) rows[res.stopped].rowMsg.textContent = res.text;
  await refresh();
  add.busy = false; add.msg = '';
  renderView('h2');
  await checkAllReach();
}
// Login-free port check, one added camera after the other. The result is shown inside its own card.
async function checkAllReach() {
  const add = SU.add;
  if (!add || add.checking) return;
  add.checking = true;
  try {
    for (const r of add.rows) if (r.added && SU.view === 'add') await checkReach(r);
  } finally { add.checking = false; }
}
async function checkReach(r) {
  const a = r.added;
  if (!a) return;
  a.ports = null; a.err = '';
  paintReach(r);
  const x = await api('POST', '/api/setup/reach', {ip: a.ip, id: a.id});
  if (x.ok && x.data && x.data.ports) a.ports = x.data.ports; else a.err = plainError(x.text);
  paintReach(r);
  const again = r.reachEl.querySelector('[data-focus^="reach:"]');
  if (again && document.activeElement && r.reachEl.contains(document.activeElement)) again.focus();
}
function paintReach(r) { r.reachEl.replaceChildren(...reachNodes(r)); }
function reachNodes(r) {
  const a = r.added;
  if (!a) return [];
  const again = btn('Check again', () => checkReach(r), 'ghost sm', {'data-focus': 'again:' + r.id});
  if (!a.ports) return [h('p', {class: 'su-busy', role: 'status', tabindex: '-1', 'data-focus': 'reach:' + r.id},
    a.err ? h('span', {class: 'su-err', role: 'alert', text: a.err}) : [h('span', {class: 'su-spin', 'aria-hidden': 'true'}), h('span', {text: 'Checking the address…'})]),
    again];
  const ok = Object.values(a.ports).some(Boolean);
  return [h('p', {class: 'su-reach ' + (ok ? 'su-ok' : 'su-bad'), role: 'status', tabindex: '-1', 'data-focus': 'reach:' + r.id},
      icon(ok ? 'check' : 'cross'), h('span', {text: ok ? 'Found on your network' : "No answer at this address. Check it's on and on this Wi-Fi."})),
    h('details', {class: 'su-more-d'}, h('summary', {}, h('span', {text: 'Details'}), icon('chevron', 'su-chev')),
      h('ul', {class: 'su-status-rows su-more-body', 'aria-label': 'Port check for ' + a.name}, portRows(a))),
    h('div', {class: 'su-bar'}, again)];
}
function portRows(a) {
  return SU_PORTS.map(([p, label]) => h('li', {}, h('span', {class: 'su-srow-main'},
    icon(a.ports[p] ? 'check' : 'cross', a.ports[p] ? 'su-ok' : 'su-bad'), h('span', {text: label})),
    h('span', {class: a.ports[p] ? 'su-ok' : 'su-bad', text: a.ports[p] ? 'Answers' : 'No answer'})));
}
// Verify (the last wizard step): one card per camera, one primary action, one footer "Open timeline".
function viewVerify() {
  const cams = SU.state.cameras, anyTried = Object.keys(SU.vs).length > 0 || cams.some(c => c.status === 'failed' || c.status === 'disabled_by_gate');
  const primary = SU.playable || SU.edit ? '' : firstAction(cams);   // one primary: while a row is being edited, its Save is it
  const busyName = (cams.find(c => c.id === SU.busy) || {}).name || '';
  const ready = SU.playable, reason = openReason(cams, busyName);
  return h('section', {class: 'su-view'},
    h('h2', {class: 'su-title', text: verifyTitle(cams, anyTried), tabindex: '-1'}),
    h('p', {class: 'su-lead', text: verifyLead(cams)}),
    cams.length ? h('div', {class: 'su-stack'}, cams.map(c => verifyCard(c, primary, cams))) : h('p', {class: 'su-note', text: 'No cameras yet.'}),
    h('div', {class: 'su-bar'}, btn('Add another camera', () => go('add'), '')),
    navRow(btn('Back', () => go('add'), 'ghost'),
      ready ? null : h('p', {class: 'su-reason', id: 'su-open-reason', text: reason}),
      btn('Open timeline', closeSetup, ready ? 'go' : '', {disabled: !ready, 'aria-describedby': ready ? null : 'su-open-reason'})));
}
// The camera whose action is the page's primary (the first one that can be pressed).
function firstAction(cams) {
  const c = cams.find(x => { const s = camState(x, SU.vs[x.id] || null, SU.busy === x.id, waitingNow(x)); return s.action && !s.action.off; });
  return c ? c.id : '';
}
function waitingNow(c) { return c.status === 'waiting' || (SU.deadline[c.id] || 0) > Date.now(); }
function verifyCard(c, primaryId, cams) {
  const waiting = waitingNow(c), st = camState(c, SU.vs[c.id] || null, SU.busy === c.id, waiting);
  if (st.key === 'ok')   // verified: one line
    return h('article', {class: 'su-card-s su-line'}, h('div', {class: 'su-cam-head'},
      h('h3', {class: 'su-name', text: c.name, 'data-focus': 'card:' + c.id, tabindex: '-1'}), chipEl(st.chip)));
  const n = cams.length, i = cams.indexOf(c), parts = [
    h('div', {class: 'su-cam-head'}, h('h3', {class: 'su-name', text: c.name, 'data-focus': 'card:' + c.id, tabindex: '-1'}),
      chipEl(st.chip, waiting ? SU.deadline[c.id] : 0)),
    h('p', {class: 'su-meta'}, h('span', {class: 'su-mono', text: c.ip}), n > 1 ? ` · Camera ${i + 1} of ${n}` : null)];
  if (waiting) parts.push(waitBar(c));
  if (st.alert) parts.push(alertBox(st.alert.tone, 'alert', null, h('p', {text: st.alert.text}), {role: st.alert.tone === 'bad' ? 'alert' : 'status'}));
  if (SU.edit === 'acct' && st.action && st.action.key === 'tplink') parts.push(tplinkEditor(c));
  // The other cameras' Verify buttons are held while a refusal stops the run; the refused camera's alert says why.
  const hold = verifyHold(c, SU.busy, SU.authStop);
  if (st.action && !(SU.edit === 'acct' && st.action.key === 'tplink'))
    parts.push(h('div', {class: 'su-bar'}, actionButton(c, st.action, c.id === primaryId, !!st.action.off || !!hold)));
  return h('article', {class: 'su-card-s su-pad'}, parts);
}
function tplinkEditor(c) {
  const box = tplinkBox({formId: 'su-acct-card-' + c.id, saved: !!SU.state.account.email_set, onDone: async () => {
    afterTplinkSaved(); SU.edit = ''; SU.saved = ''; await refresh(); renderView(`[data-focus="card:${c.id}"]`);
  }, onCancel: endEdit});
  return h('div', {class: 'su-stack'}, box.form, h('div', {class: 'su-bar'}, box.cancelBtn, box.submit));
}
// The one action of a camera (state table). primary: the page's .go; disabled: the reason is shown elsewhere.
function actionButton(c, a, primary, disabled) {
  const onclick = {
    verify: () => verifyCam(c), verifyagain: () => verifyCam(c), turnon: () => setEnabled(c, true),
    tplink: () => openAccountEdit(c), account: () => openAccountEdit(c), address: () => editAddress(c, 'act:' + c.id),
  }[a.key];
  return btn(a.label, onclick, primary ? 'go' : '', {'data-focus': 'act:' + c.id, disabled});
}
// "Change TP-Link password": the account editor opens where the user is (the card in the wizard, the account pane in Settings).
function openAccountEdit(c) {
  if (SU.view === 'settings') SU.sel = 'account';
  SU.edit = 'acct'; SU.opener = 'act:' + c.id; SU.errs = {};
  renderView('[data-focus="edit:acct"]');
}
function waitBar(c) {
  const until = SU.deadline[c.id], span = SU.span[c.id] || Math.max(1, until - Date.now());
  const left = Math.max(0, until - Date.now());
  const fill = h('div', {class: 'su-bar-fill', 'data-until': String(until), 'data-span': String(span)});
  fill.style.setProperty('--frac', String(Math.min(1, left / span)));
  return h('div', {class: 'su-bar-track', 'aria-hidden': 'true'}, fill);
}
function viewSettings() {
  const cams = SU.state.cameras;
  if (!['account', 'computer', 'devices', 'about', 'new'].includes(SU.sel) && !cams.some(c => c.id === SU.sel)) SU.sel = pickSelection(cams);
  return h('div', {class: 'su-set', 'data-screen': SU.screen}, sideNav(cams),
    h('div', {class: 'su-pane', id: 'supane'}, h('div', {class: 'su-pane-in'}, paneFor(cams))));
}
// The sidebar: cameras (one row each, then Add camera), the TP-Link account, this computer, about.
function sideNav(cams) {
  const row = (sel, label, o) => h('button', {type: 'button', class: 'su-row' + (o.cam ? ' su-row-cam' : ''),
    'aria-current': SU.sel === sel ? 'page' : undefined, tabindex: SU.sel === sel ? '0' : '-1', 'data-sel': sel,
    'data-focus': 'side:' + sel, onclick: () => selectPane(sel)},
    o.plus ? icon('plus', 'su-row-ico') : (o.dot ? h('span', {class: 'su-dot ' + o.dot, 'aria-hidden': 'true'}) : null),
    h('span', {class: 'su-row-main'}, h('span', {class: 'su-row-name', text: label}), o.sub ? h('span', {class: 'su-row-sub', text: o.sub}) : null),
    narrow() ? icon('chevron', 'su-row-chev') : null);
  const camRows = cams.map(c => {
    const s = camState(c, SU.vs[c.id] || null, SU.busy === c.id, waitingNow(c));
    const r = row(c.id, c.name, {cam: true, dot: s.dot, sub: s.nav});
    // The selected camera carries its own Up and Down arrows (a sibling of the row button: buttons do not nest).
    if (c.id !== SU.sel || cams.length < 2 || narrow()) return r;
    r.classList.add('su-row-mv-pad');
    return h('div', {class: 'su-row-wrap'}, r, h('div', {class: 'su-row-mv'}, moveButton(c, -1), moveButton(c, 1)));
  });
  const addRow = SU.sel === 'new' ? row('new', 'New camera', {cam: true, dot: 'mut'}) : row('new', 'Add camera', {cam: true, plus: true});
  // Group labels are divs named by aria-labelledby, so the pane's title is the first heading of the page.
  const group = (id, label, rows) => h('section', {class: 'su-grp', 'aria-labelledby': id}, h('div', {class: 'su-grp-h', id, text: label}), rows);
  return h('nav', {class: 'su-side', 'aria-label': 'Settings', onkeydown: sideKeys},
    group('su-g-cam', 'Cameras', [camRows, addRow]),
    group('su-g-gen', 'General', [row('account', 'TP-Link account', {}), row('computer', 'This computer', {}),
      row('devices', 'Other devices', {}), row('about', 'About', {})]));
}
function selectPane(sel) {
  const prior = SU.sel;
  if (sel === 'new') SU.prevSel = prior === 'new' ? SU.prevSel : prior;
  const wasList = SU.screen === 'list';
  SU.sel = sel; SU.edit = ''; SU.opener = ''; SU.errs = {}; SU.saved = ''; SU.devForm = {ip: '', name: ''};
  SU.newRow = null;
  if (narrow()) {
    SU.screen = 'detail';
    if (wasList) history.pushState({su: 'detail'}, '');
    renderView('[data-focus="pane-title"]');
  } else renderView(`[data-focus="side:${sel}"]`);
  if (sel === 'devices') loadDevices();
}
function paneFor(cams) {
  if (SU.sel === 'new') return paneNew();
  if (SU.sel === 'account') return paneAccountPane();
  if (SU.sel === 'computer') return paneComputer();
  if (SU.sel === 'devices') return paneDevices();
  if (SU.sel === 'about') return paneAbout();
  const c = cams.find(x => x.id === SU.sel);
  return c ? paneCamera(c) : paneEmpty();
}
function section(title, body) { return h('section', {class: 'su-sec'}, h('h3', {class: 'su-sec-h', text: title}), body); }
function paneEmpty() {
  return h('div', {class: 'su-stack'}, h('h2', {class: 'su-id-name', text: 'No cameras yet', 'data-focus': 'pane-title', tabindex: '-1'}),
    h('p', {class: 'su-lead', text: 'Add a camera to see its recordings here.'}),
    h('div', {class: 'su-bar'}, btn('Add camera', () => selectPane('new'), 'go')));
}
// One camera: identity (name, state), then Connection, Live view, Playback and Remove. Nothing is collapsed except Details.
function paneCamera(c) {
  const st = camState(c, SU.vs[c.id] || null, SU.busy === c.id, waitingNow(c));
  return [identity(c, st),
    section('Connection', connectionCard(c)),
    section('Live view', liveCard(c)),
    section('Playback', playbackCard(c)),
    SU.state.cameras.length > 1 && narrow() ? section('Order', orderCard(c)) : null,   // on a wide screen the sidebar row has the arrows
    section('Remove', removeCard(c))];
}
function identity(c, st) {
  const editing = SU.edit === 'name:' + c.id;
  const hold = verifyHold(c, SU.busy, SU.authStop);
  const right = editing ? null : h('div', {class: 'su-id-r'},
    chipEl(st.chip, waitingNow(c) ? SU.deadline[c.id] : 0), savedTag('state:' + c.id),
    st.action ? actionButton(c, st.action, !SU.edit, !!st.action.off || !!hold) : null,
    c.verified && c.enabled ? btn('Turn off', () => setEnabled(c, false, true), '', {'data-focus': 'off:' + c.id}) : null);
  return h('div', {class: 'su-ident'},
    editing ? nameEditor(c) : h('div', {class: 'su-id'},
      h('div', {class: 'su-id-main'},
        h('h2', {class: 'su-id-name', text: c.name, 'data-focus': 'pane-title', tabindex: '-1'}),
        h('button', {type: 'button', class: 'su-btn ghost sm', 'data-focus': 'rename:' + c.id,
          onclick: () => startEdit('name:' + c.id, 'rename:' + c.id)}, icon('pencil'), 'Rename'),
        savedTag('name:' + c.id)),
      right),
    editing ? null : errAt('state:' + c.id),
    editing || !st.alert ? null : alertBox(st.alert.tone, 'alert', null, h('p', {text: st.alert.text}), {role: st.alert.tone === 'bad' ? 'alert' : 'status'}));
}
function nameEditor(c) {
  const key = 'name:' + c.id, input = h('input', {value: c.name, maxlength: 60, autocomplete: 'off', required: true, 'data-focus': 'edit:' + key});
  if (SU.errs[key]) setErr(input, SU.errs[key]);
  watch(input);
  return h('form', {class: 'su-stack', novalidate: true, onsubmit: e => { e.preventDefault(); saveName(c, input); }},
    labelled('Camera name', input),
    h('div', {class: 'su-bar'}, btn('Cancel', endEdit, 'ghost'), btn('Save', null, 'go', {type: 'submit'})));
}
async function saveName(c, input) {
  const err = validateName(input.value);
  setErr(input, err);
  if (err) { input.focus(); return; }
  const name = input.value.trim();
  if (name === c.name) return endEdit();
  if (await patchCam(c, {name}, 'name:' + c.id)) { SU.edit = ''; SU.saved = 'name:' + c.id; SU.opener = ''; renderView(`[data-focus="rename:${c.id}"],[data-focus="pane-title"]`); }
  else renderView(`[data-focus="edit:name:${c.id}"]`);
}
function startEdit(key, opener) {
  SU.edit = key; SU.opener = opener || ''; SU.saved = '';
  if (key === 'device-add') delete SU.errs.add; else SU.errs = {};   // the add form clears only its own error
  renderView(`[data-focus="edit:${key}"]`);
}
function endEdit() {
  const o = SU.opener, was = SU.edit;
  SU.edit = ''; SU.opener = ''; SU.errs = {};
  if (was === 'device-add') SU.devForm = {ip: '', name: ''};   // Cancel or Escape drops the draft
  renderView(o ? `[data-focus="${o}"],[data-focus="pane-title"]` : '[data-focus="pane-title"],h2');
}
// PATCH one camera. Success: the camera's old Verify result is stale. Error: under the key.
async function patchCam(c, body, key) {
  const r = await api('PATCH', `/api/cameras/${c.id}`, body);
  SU.errs = {};
  if (r.ok) { SU.changed = true; delete SU.vs[c.id]; SU.saved = key; }
  else SU.errs[key] = plainError(r.text);
  await refresh();
  return r.ok;
}
// Switch on (verified cameras only; "Saved" shows next to the state) or off (a toast with Undo).
async function setEnabled(c, on, withUndo) {
  const ok = await patchCam(c, {enabled: on}, 'state:' + c.id);
  if (ok && !on && withUndo) toast(`${c.name} is off.`, () => setEnabled(c, true));
  renderView(`[data-focus="act:${c.id}"],[data-focus="off:${c.id}"],[data-focus="pane-title"]`);
}
// Address: a new address on a verified camera asks first. The editor is in the Connection card. From the wizard it
// opens Settings on that camera (SU.edit is set after the view changes, so go() does not clear it).
function editAddress(c, opener) {
  const open = () => {
    if (SU.view === 'settings') return startEdit('addr:' + c.id, opener);
    SU.view = 'settings'; SU.add = null; SU.sel = c.id; SU.edit = 'addr:' + c.id; SU.opener = opener; SU.errs = {};
    SU.screen = narrow() ? 'detail' : 'list';
    renderView(`[data-focus="edit:addr:${c.id}"]`);
  };
  if (c.verified) confirmDialog(dialogCopy('address', c.name), opener, open);
  else open();
}
// Confirm dialog: Cancel is focused first, Esc closes it, focus goes back to the opener.
function confirmDialog(copy, opener, onOk) {
  const dlg = byId('suconfirm');
  const cancel = btn('Cancel', () => dlg.close(), '', {'data-focus': 'dlg-cancel'});
  const ok = btn(copy.ok, () => { dlg.close(); onOk(); }, copy.bad ? 'bad' : 'go');
  dlg.replaceChildren(h('h2', {class: 'su-dlg-t', id: 'suconfirmt', text: copy.title, tabindex: '-1'}),
    h('p', {class: 'su-dlg-b', text: copy.body}), h('div', {class: 'su-dlg-act'}, cancel, ok));
  dlg.onclose = () => {
    const t = (opener && byId('setup').querySelector(`[data-focus="${opener}"]`)) || byId('setupmain').querySelector('h2');
    if (t) t.focus();
  };
  dlg.showModal();
  cancel.focus();
}
// Connection: address (verified cameras confirm first), model reported by the device, sign-in method.
function connectionCard(c) {
  const explicit = !!c.auth && c.auth !== 'auto';
  const model = c.model && c.model !== 'other' ? c.model : (c.model_seen ? seenModel(c.model_seen) || c.model_seen : '');
  return h('div', {class: 'su-stack'},
    h('div', {class: 'su-card-s'},
      addressRow(c),
      cred('Model', model ? `${model}, reported by the camera` : 'Not known until the camera is verified'),
      cred('Sign-in', [explicit ? 'Manual' : signInValue('auto'), savedTag('auto:' + c.id)],
        explicit ? btn('Use automatic sign-in', () => useAutomatic(c), 'ghost sm', {'data-focus': 'auto:' + c.id}) : null)),
    errAt('auto:' + c.id));
}
function addressRow(c) {
  const key = 'addr:' + c.id;
  if (SU.edit !== key)
    return cred('Address', h('span', {class: 'su-mono', text: c.ip}),
      h('span', {class: 'su-cred-a'}, savedTag(key), btn('Change', () => editAddress(c, 'addr-change:' + c.id), 'ghost sm', {'data-focus': 'addr-change:' + c.id})));
  const input = h('input', {value: c.ip, inputmode: 'decimal', autocomplete: 'off', 'data-focus': 'edit:' + key});
  if (SU.errs[key]) setErr(input, SU.errs[key]);
  watch(input);
  const hint = c.verified ? 'Changing the address switches the camera off until you verify it at the new address.'
    : 'Enter the address the camera has on your network.';
  return h('form', {class: 'su-cred su-cred-wide', novalidate: true, onsubmit: e => { e.preventDefault(); saveAddress(c, input); }},
    h('span', {class: 'su-cred-l', text: 'Address'}),
    h('div', {class: 'su-stack'}, labelled('IP address', input, hint),
      h('div', {class: 'su-bar'}, btn('Cancel', endEdit, 'ghost'), btn('Save', null, 'go', {type: 'submit'}))));
}
async function saveAddress(c, input) {
  const a = input.value.trim(), err = validateAddress(a), key = 'addr:' + c.id;
  setErr(input, err);
  if (err) { input.focus(); return; }
  if (a === c.ip) return endEdit();
  if (await patchCam(c, {ip: a}, key)) {
    SU.edit = ''; SU.opener = '';
    renderView(`[data-focus="addr-change:${c.id}"],[data-focus="pane-title"]`);
  } else renderView(`[data-focus="edit:${key}"]`);
}
function useAutomatic(c) {
  confirmDialog(dialogCopy('auto', c.name), 'auto:' + c.id, async () => {
    const r = await api('POST', `/api/cameras/${c.id}/credentials`, {auth: 'auto', reset_auth: true});
    SU.errs = {};
    if (r.ok) { SU.changed = true; delete SU.vs[c.id]; SU.saved = 'auto:' + c.id; }
    else SU.errs['auto:' + c.id] = plainError(r.text);
    await refresh();
    renderView(`[data-focus="act:${c.id}"],[data-focus="pane-title"]`);
  });
}
// Live view: one saved-credential row. Not added: Add. Saved: user and masked password, Change. Refused by the camera:
// the chip and one line of text. Developer setting: named, not editable here.
function liveCard(c) {
  const lr = liveRow(c), key = 'live:' + c.id;
  if (SU.edit === key) return liveEditor(c, lr);
  const value = lr.mode === 'dev' ? h('span', {text: 'Using the developer setting'})
    : lr.mode === 'saved'
      ? h('span', {}, lr.user + ' · ', h('span', {class: 'su-mask', role: 'img', 'aria-label': 'Password saved', text: '••••••••'}))
      : h('span', {class: 'su-faint', text: 'Not added'});
  const action = lr.mode === 'none' ? btn('Add', () => startEdit(key, 'live-add:' + c.id), 'ghost sm', {'data-focus': 'live-add:' + c.id})
    : lr.mode === 'saved' ? btn('Change', () => startEdit(key, 'live-change:' + c.id), 'ghost sm', {'data-focus': 'live-change:' + c.id}) : null;
  return h('div', {class: 'su-stack'},
    h('div', {class: 'su-card-s'},
      cred('Camera account', h('span', {class: 'su-cred-chip'}, value, lr.refused ? chipEl({tone: 'bad', label: 'Not accepted'}) : null), action),
      lr.refused ? h('p', {class: 'su-cred-note su-bad', text: "The camera didn't accept this camera account. Check it in the Tapo app, then change it here."}) : null,
      savedTag(key) ? h('div', {class: 'su-cred-note'}, savedTag(key)) : null,
      errAt(key) ? h('div', {class: 'su-cred-note'}, errAt(key)) : null),
    h('p', {class: 'su-detail', text: 'Used only for Live view. Verify does not need it.'}));
}
function liveEditor(c, lr) {
  const key = 'live:' + c.id, saved = lr.mode === 'saved';
  const user = h('input', {value: saved ? lr.user : 'rtspadmin', autocomplete: 'off', maxlength: 64, 'data-focus': 'edit:' + key});
  const pw = h('input', {type: 'password', autocomplete: 'new-password', maxlength: 256});
  watch(user); watch(pw);
  return h('form', {class: 'su-card-s su-pad', novalidate: true, onsubmit: async e => {
    e.preventDefault();
    const u = user.value.trim(), p = pw.value, pe = validateSecret(p, 'camera account password');
    setErr(user, u ? '' : 'Enter the camera account user name.');
    setErr(pw, pe);
    if (!u || pe) { (u ? pw : user).focus(); return; }
    pw.value = '';   // the password leaves the page with the request
    const r = await api('POST', `/api/cameras/${c.id}/credentials`, {camera_user: u, camera_password: p});
    SU.errs = {};
    if (r.ok) { SU.changed = true; SU.saved = key; }
    else SU.errs[key] = plainError(r.text);
    const o = SU.opener;
    SU.edit = r.ok ? '' : SU.edit; SU.opener = r.ok ? '' : o;
    await refresh();
    renderView(r.ok ? `[data-focus="${o}"],[data-focus="pane-title"]` : `[data-focus="edit:${key}"]`);
  }},
    labelled('Camera account user name', user, saved ? 'Change only if you changed it in the Tapo app.'
      : 'Default rtspadmin. Change it only if you changed it on the camera.'),
    labelled('Camera account password', pw, saved ? 'Enter the password to save the change.' : null),
    errAt(key),
    h('div', {class: 'su-bar'}, btn('Cancel', endEdit, 'ghost'), btn('Save', null, 'go', {type: 'submit'})));
}
// Playback: switches (saved on change) and two numbers (saved on Enter or leaving the field). Details is the one disclosure.
function playbackCard(c) {
  const fl = c.flags || {}, ptzCam = ['C200', 'C210'].includes(c.model || seenModel(c.model_seen)), rows = [
    togRow(c, 'Event thumbnails', 'thumbs', fl.thumbs !== undefined ? !!fl.thumbs : true),
    togRow(c, 'Fast-forward', 'fast_forward', !!fl.fast_forward, 'Lets you play recordings at 2x and faster.')];
  if (ptzCam || fl.ptz) rows.push(togRow(c, 'Pan and tilt (PTZ)', 'ptz', !!fl.ptz));
  if (fl.ptz) rows.push(togRow(c, 'Invert pan and tilt', 'ptz_invert', !!fl.ptz_invert),
    numRow(c, 'Pan and tilt step', 'ptz_step_deg', 'degrees', 1, 180, false, fl.ptz_step_deg));
  return h('div', {class: 'su-stack'},
    h('div', {class: 'su-card-s'}, rows),
    h('details', {class: 'su-more-d'}, h('summary', {}, h('span', {text: 'Details'}), icon('chevron', 'su-chev')),
      h('div', {class: 'su-stack su-more-body'},
        h('div', {class: 'su-card-s'}, numRow(c, 'Clip piece length', 'win', 'seconds', 1, 3600, true, fl.win))),
      h('dl', {class: 'su-dl su-more-body'},
        h('dt', {text: 'Connection'}), h('dd', {text: fl.p2p ? 'Direct link' : 'Network link'}),
        h('dt', {text: 'Playback'}), h('dd', {text: fl.stream_playback ? 'Streamed' : 'From the camera clips'}),
        h('dt', {text: 'Firmware'}), h('dd', {text: c.fw_seen || 'Not reported yet'}),
        h('dt', {text: 'Internal ID'}), h('dd', {class: 'su-mono', text: c.id}),
        c.model_note ? h('dt', {text: 'Note'}) : null, c.model_note ? h('dd', {text: c.model_note}) : null)));
}
function togRow(c, label, key, on, hint) {
  const lid = 'su-l' + (++SU.uid), k = 'flag:' + c.id + ':' + key;
  const sw = h('button', {type: 'button', class: 'su-switch', role: 'switch', 'aria-checked': on ? 'true' : 'false',
    'aria-labelledby': lid, 'data-focus': 'tog:' + c.id + ':' + key,
    onclick: () => saveFlag(c, key, !on, k, 'tog:' + c.id + ':' + key)});
  return h('div', {class: 'su-tog'}, h('div', {}, h('span', {class: 'su-tog-l', id: lid, text: label}),
      hint ? h('p', {class: 'su-detail', text: hint}) : null),
    h('div', {class: 'su-tog-r'}, savedTag(k), errAt(k), sw));
}
function numRow(c, label, key, unit, min, max, whole, cur) {
  const lid = 'su-l' + (++SU.uid), k = 'flag:' + c.id + ':' + key, focus = 'num:' + c.id + ':' + key;
  const input = h('input', {type: 'number', class: 'su-num-in', min, max, step: whole ? 1 : 'any', inputmode: 'decimal',
    value: cur === undefined || cur === null ? '' : String(cur), placeholder: key === 'ptz_step_deg' ? '10' : null, 'aria-labelledby': lid, 'data-focus': focus});
  const err = h('p', {class: 'su-err'});
  const save = () => {
    const msg = numberError(label, min, max, whole, input.value);
    err.textContent = msg;
    if (msg) { input.setAttribute('aria-invalid', 'true'); return; }
    input.removeAttribute('aria-invalid');
    const v = Number(input.value.trim());
    if (cur !== undefined && cur !== null && v === Number(cur)) return;
    saveFlag(c, key, v, k, focus);
  };
  input.addEventListener('blur', save);
  input.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); save(); } });
  return h('div', {class: 'su-tog'}, h('span', {class: 'su-tog-l', id: lid, text: `${label} (${unit})`}),
    h('div', {class: 'su-tog-r'}, input, savedTag(k)), err);
}
// One flag saved (PATCH flags). The row keeps focus when it re-renders.
async function saveFlag(c, key, val, savedKey, focus) {
  await patchCam(c, {flags: {[key]: val}}, savedKey);
  renderView(`[data-focus="${focus}"]`);
}
// Remove: a confirm dialog, then the camera goes (its saved passwords are deleted, the sign-in history is kept).
// An icon button that moves camera c one place (dir -1 up, +1 down) in the order; disabled at the end of the list.
function moveButton(c, dir) {
  const ids = SU.state.cameras.map(x => x.id), j = ids.indexOf(c.id) + dir;
  return h('button', {type: 'button', class: 'su-mv', 'data-focus': `move${dir}:${c.id}`, disabled: j < 0 || j >= ids.length,
    'aria-label': `Move ${c.name} ${dir < 0 ? 'up' : 'down'}`, onclick: () => moveCam(c, dir)}, icon(dir < 0 ? 'up' : 'down'));
}
// Order: where the camera sits in the timeline's switcher and in this list. One step at a time, so a keyboard works.
function orderCard(c) {
  const ids = SU.state.cameras.map(x => x.id), i = ids.indexOf(c.id), key = 'order:' + c.id;
  const mk = (dir, label, ico) => {
    const b = btn(label, () => moveCam(c, dir), '', {'data-focus': `move${dir}:${c.id}`, disabled: i + dir < 0 || i + dir >= ids.length});
    b.prepend(icon(ico));
    return b;
  };
  return h('div', {class: 'su-card-s'},
    h('div', {class: 'su-cred su-cred-solo'},
      h('span', {class: 'su-cred-v'}, `Camera ${i + 1} of ${ids.length}`, savedTag(key)),
      h('div', {class: 'su-cred-a'}, mk(-1, 'Move up', 'up'), mk(1, 'Move down', 'down'))),
    errAt(key));
}
async function moveCam(c, dir) {
  const ids = SU.state.cameras.map(x => x.id), order = movedOrder(ids, c.id, dir), key = 'order:' + c.id;
  const r = await api('PUT', '/api/cameras/order', {order});
  SU.errs = {};
  if (r.ok) { SU.changed = true; SU.saved = key; } else SU.errs[key] = plainError(r.text);
  await refresh();
  // The pressed button may now be disabled (the camera reached an end): focus goes to the other one.
  renderView(`[data-focus="move${dir}:${c.id}"]:not(:disabled),[data-focus="move${-dir}:${c.id}"]:not(:disabled),[data-focus="pane-title"]`);
}
function removeCard(c) {
  return h('div', {class: 'su-card-s su-danger'},
    h('div', {class: 'su-cred su-cred-solo'}, h('span', {class: 'su-cred-v', text: `Remove ${c.name} from this app`}),
      h('div', {class: 'su-cred-a'}, btn('Remove camera', () => confirmDialog(dialogCopy('remove', c.name), 'remove:' + c.id, () => removeCam(c)),
        'bad-o', {'data-focus': 'remove:' + c.id}))),
    errAt('remove:' + c.id));
}
async function removeCam(c) {
  const r = await api('DELETE', `/api/cameras/${c.id}`, {confirm: c.id});
  SU.errs = {};
  if (!r.ok) {
    SU.errs['remove:' + c.id] = plainError(r.text);
    await refresh();
    return renderView(`[data-focus="remove:${c.id}"]`);
  }
  SU.changed = true; delete SU.vs[c.id];
  const kept = r.data && r.data.secret_removed === false;   // the camera is gone, but its stored password is not
  if (!kept) toast(`${c.name} is removed. Its saved passwords were deleted.`);
  await refresh();
  SU.sel = kept ? 'computer' : pickSelection(SU.state.cameras);
  if (kept) SU.errs = {computer: 'The camera was removed, but its stored password could not be deleted. Check the saved passwords here.'};
  if (narrow()) {
    SU.screen = 'list';
    if (history.state && history.state.su === 'detail') history.back();
  }
  renderView(`[data-focus="side:${SU.sel}"],[data-focus="pane-title"]`);
}
// Settings: the new camera (single row, the same fields as the wizard). Cancel reselects the previous camera.
function paneNew() {
  if (!SU.newRow) SU.newRow = newAddRow(true);
  const row = SU.newRow;
  return [h('div', {class: 'su-ident'}, h('h2', {class: 'su-id-name', text: 'New camera', 'data-focus': 'pane-title', tabindex: '-1'})),
    h('div', {class: 'su-card-s su-pad'}, row.el, row.rowMsg),
    h('div', {class: 'su-bar su-bar-end'},
      btn('Cancel', () => selectPane(SU.prevSel && SU.prevSel !== 'new' ? SU.prevSel : pickSelection(SU.state.cameras)), 'ghost'),
      btn('Add camera', addSingle, 'go', {'data-focus': 'add-single'}))];
}
async function addSingle() {
  const row = SU.newRow;
  if (!row) return;
  const data = {name: row.name.value, ip: row.ip.value, user: row.user.value, password: row.pw.value, added: false};
  const e = validateAddRow(data);
  setErr(row.name, e.name || ''); setErr(row.ip, e.ip || ''); setErr(row.user, e.user || '');
  row.rowMsg.textContent = '';
  if (e.user) row.live.open = true;
  const first = [[row.name, e.name], [row.ip, e.ip], [row.user, e.user]].find(x => x[1]);
  if (first) { first[0].focus(); return; }
  let added = '';
  const res = await runAddBatch([data], async (i, body) => {
    row.pw.value = '';   // the password leaves the page with the request
    const r = await api('POST', '/api/setup/cameras', body);
    if (!r.ok) return {ok: false, text: plainError(r.text)};
    if (!r.data || !r.data.id) return {ok: false, text: 'The app did not confirm that the camera was saved. Check Settings before adding it again.'};
    return {ok: true, id: r.data.id};
  }, (i, id) => { added = id; });
  if (res.stopped >= 0) { row.rowMsg.textContent = res.text; return; }
  SU.changed = true; SU.newRow = null; SU.edit = '';
  await refresh();
  SU.sel = added;
  toast(`${data.name.trim()} is added. Verify it to switch it on.`);
  renderView(`[data-focus="pane-title"]`);
}
// The TP-Link account pane: one saved-credential box, or the form (not saved yet, or changing).
function paneAccountPane() {
  const saved = !!SU.state.account.email_set, editing = !saved || SU.edit === 'acct';
  const box = tplinkBox({formId: 'su-acct-pane', saved, onDone: async () => {
    afterTplinkSaved(); SU.edit = ''; SU.saved = 'acct'; await refresh();
    renderView('[data-focus="acct-change"],[data-focus="pane-title"]');
  }, onCancel: endEdit});
  return [h('div', {class: 'su-ident'}, h('h2', {class: 'su-id-name', text: 'TP-Link account', 'data-focus': 'pane-title', tabindex: '-1'})),
    h('p', {class: 'su-lead', text: 'Signs in to all your cameras. Use the account that owns them.'}),
    section('Account', saved && !editing
      ? acctRows()
      : h('div', {class: 'su-card-s su-pad'}, box.form, h('div', {class: 'su-bar'}, saved ? box.cancelBtn : null, box.submit)))];
}
// This computer: theme, password storage (its problem is shown here only), and the port line.
function paneComputer() {
  const rows = prereqRows(SU.state.prerequisites), [tools, saved] = rows;
  return [h('div', {class: 'su-ident'}, h('h2', {class: 'su-id-name', text: 'This computer', 'data-focus': 'pane-title', tabindex: '-1'})),
    section('Appearance', h('div', {class: 'su-card-s'}, cred('Theme', themeGroup()))),
    section('Video tools', tools.ok
      ? h('p', {class: 'su-srow-main'}, icon('check', 'su-ok'), h('span', {text: 'This computer has the video tools.'}))
      : alertBox('bad', 'alert', tools.fix, h('p', {}, fixLink(tools)), {role: 'alert'})),
    section('Saved passwords', h('div', {class: 'su-stack'},
      h('div', {class: 'su-card-s'}, cred('Saved passwords', saved.ok ? 'Kept on this computer' : 'Not usable here')),
      saved.ok ? null : alertBox('bad', 'alert', saved.fix, h('p', {}, fixLink(saved)), {role: 'alert'}),
      errAt('computer'))),
    h('p', {class: 'su-foot-note', text: `Port: ${location.port || '80'} · Change it with KINORA_PORT and restart.`})];
}
// Auto / Light / Dark: a radio group (aria-checked on the chosen one, one tab stop, arrow keys apply).
function themeGroup() {
  const group = h('div', {class: 'su-seg-group', role: 'radiogroup', 'aria-label': 'Colour theme', onkeydown: themeKeys});
  for (const t of themeChoices()) {
    const on = themeNow() === t;
    group.append(h('button', {type: 'button', class: 'su-seg-btn', role: 'radio', 'aria-checked': on ? 'true' : 'false',
      tabindex: on ? '0' : '-1', 'data-focus': 'theme:' + t, onclick: () => setTheme(t), text: themeWord(t)}));
  }
  return group;
}
// Arrow keys move the choice in the radio group and apply it (roving tab stop: the chosen theme is the one tab stop).
function themeKeys(e) {
  if (!/^Arrow(Left|Right|Up|Down)$/.test(e.key)) return;
  const btns = [...e.currentTarget.querySelectorAll('[role="radio"]')], i = btns.indexOf(e.target);
  if (i < 0) return;
  e.preventDefault();
  const choices = themeChoices();
  setTheme(choices[(i + (e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : -1) + btns.length) % btns.length]);
}
function paneAbout() {
  const version = SU.state && SU.state.version;
  return [h('div', {class: 'su-ident'}, h('h2', {class: 'su-id-name', text: 'About this app', 'data-focus': 'pane-title', tabindex: '-1'})),
    section('Kinora', h('div', {class: 'su-stack'},
      h('p', {class: 'su-lead', text: "Watch the recordings on your Tapo cameras' SD cards, on this computer."}),
      version ? h('div', {class: 'su-card-s'}, cred('Version', version)) : null,
      h('p', {class: 'su-foot-note', text: SU_LEGAL})))];
}
// ---------- Other devices: who may use this server from another computer (kinora/services/devices_api.py) ----------
// Errors are kept under their row: SU.errs.sharing, .address, .pending, .list, .add. Every action answers with the
// whole list (applyDevices), so the page always shows what the server stored. A waiting device's last-seen time is
// painted in place (paintSeen); the list is re-read every 5 s while this pane is open (devicesTick).
function devicesVisible() {
  return !byId('setup').hidden && SU.view === 'settings' && SU.sel === 'devices' && (!narrow() || SU.screen === 'detail');
}
function typingNow() {
  const a = document.activeElement;
  return !!a && byId('setup').contains(a) && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName);
}
function applyDevices(data) {
  if (!data || !Array.isArray(data.devices) || !Array.isArray(data.pending)) return false;
  SU.dev = data;
  return true;
}
async function loadDevices() {
  const before = devicesKey(SU.dev);
  const r = await api('GET', '/api/setup/devices');
  if (r.ok && applyDevices(r.data)) {
    SU.devErr = '';
    if (devicesKey(SU.dev) !== before) SU.devDirty = true;
  } else {
    const msg = r.ok ? 'The list did not load. Try again.' : r.text;
    if (msg !== SU.devErr || !SU.dev) { SU.devErr = msg; SU.devDirty = true; }
  }
  redrawDevices();
  paintSeen();
}
// Redraws the pane when the list changed, unless a text field has focus (the change waits for the next tick).
function redrawDevices() {
  if (!SU.devDirty || !devicesVisible() || typingNow()) return;
  SU.devDirty = false;
  renderView();
}
async function devicesTick() {
  if (devicesVisible()) await loadDevices();
}
function paintSeen() {
  const now = Date.now() / 1000;
  document.querySelectorAll('#setup [data-seen-ip]').forEach(n => {
    const p = (SU.dev && SU.dev.pending || []).find(x => x.ip === n.dataset.seenIp);
    if (p) n.textContent = 'tried to connect ' + agoText(now - p.last_seen);
  });
}
function paneDevices() {
  const head = [h('div', {class: 'su-ident'}, h('h2', {class: 'su-id-name', text: 'Other devices', 'data-focus': 'pane-title', tabindex: '-1'})),
    h('p', {class: 'su-lead', text: 'Use this Kinora from other computers on your home network. They can watch recordings, go live and export, but cannot change settings or passwords.'})];
  const d = SU.dev;
  if (!d) return [...head, alertBox('bad', 'alert', null, h('p', {text: SU.devErr || 'The list did not load. Try again.'}),
    {role: 'alert'}), h('div', {class: 'su-bar'}, btn('Try again', loadDevices, 'go', {'data-focus': 'dev-retry'}))];
  const parts = [...head, section('Sharing', sharingBox(d))];
  if (d.sharing && (d.address || d.address_error)) parts.push(section('Address', addressBox(d)));
  if (d.pending.length) parts.push(section('Waiting for approval', pendingBox(d)));
  parts.push(section('Allowed devices', h('div', {class: 'su-stack'}, allowedBox(d), addDeviceArea())));
  parts.push(h('p', {class: 'su-foot-note', text: 'There is no password on this connection: only turn it on for a network you trust.'}));
  return parts;
}
function sharingBox(d) {
  if (d.from_command_line) return h('div', {class: 'su-stack'}, h('div', {class: 'su-card-s'}, cred('Let other computers connect', 'On (set when Kinora was started)')));
  return h('div', {class: 'su-stack'}, h('div', {class: 'su-card-s'}, cred('Let other computers connect', sharingGroup(!!d.sharing))), errAt('sharing'));
}
// Off / On: a radio group like the theme choice (aria-checked on the chosen one, one tab stop, arrow keys apply).
function sharingGroup(on) {
  const group = h('div', {class: 'su-seg-group', role: 'radiogroup', 'aria-label': 'Let other computers connect', onkeydown: sharingKeys});
  for (const [key, label, value] of [['off', 'Off', false], ['on', 'On', true]]) {
    const chosen = on === value;
    group.append(h('button', {type: 'button', class: 'su-seg-btn', role: 'radio', 'aria-checked': chosen ? 'true' : 'false',
      tabindex: chosen ? '0' : '-1', 'data-focus': 'share:' + key, onclick: () => setSharing(value), text: label}));
  }
  return group;
}
function sharingKeys(e) {
  if (!/^Arrow(Left|Right|Up|Down)$/.test(e.key)) return;
  const btns = [...e.currentTarget.querySelectorAll('[role="radio"]')], i = btns.indexOf(e.target);
  if (i < 0) return;
  e.preventDefault();
  setSharing(btns[(i + 1) % btns.length].dataset.focus === 'share:on');
}
async function setSharing(on) {
  if (!SU.dev || !!SU.dev.sharing === on) return;
  delete SU.errs.sharing;
  const r = await api('PUT', '/api/setup/sharing', {enabled: on});
  if (!r.ok) SU.errs.sharing = r.text;
  else {
    applyDevices(r.data);
    toast(on ? 'Other computers can now open Kinora.' : 'Other computers can no longer connect.');
  }
  renderView(`[data-focus="share:${on ? 'on' : 'off'}"]`);
}
function addressBox(d) {
  const kids = [];
  if (!d.address) SU.qr = false;   // the QR code is of the address the network socket listens on: no address, no card
  if (d.address) kids.push(h('div', {class: 'su-card-s'},
    cred('Open this on the other computer', h('span', {class: 'su-mono', text: d.address}),
      btn('Copy', () => copyAddress(d.address), 'ghost sm', {'data-focus': 'copy-address'}), 'su-cred-addr'),
    qrDisclosure(d.address)));
  if (d.address_error) kids.push(h('p', {class: 'su-foot-note', text: d.address_error}));
  kids.push(errAt('address'));
  return h('div', {class: 'su-stack'}, kids);
}
// The QR code for a phone: a native disclosure under the address, open exactly when SU.qr says so. The toggle event keeps
// SU.qr in step with the native state (no re-render). Nothing in the body is a tab stop.
function qrDisclosure(address) {
  const img = h('img', {class: 'su-qr-img', src: '/api/setup/devices/qr?a=' + encodeURIComponent(address), alt: 'QR code for ' + address,
    width: '160', height: '160', loading: 'lazy'});
  img.addEventListener('error', () => img.replaceWith(h('p', {class: 'su-hint', text: 'The QR code did not load. Type the address on your phone instead.'})));
  const det = h('details', {class: 'su-more-d', open: !!SU.qr},
    h('summary', {'data-focus': 'qr-toggle'}, h('span', {text: 'QR code for a phone'}), icon('chevron', 'su-chev')),
    h('div', {class: 'su-more-body su-qr'}, img,
      h('div', {class: 'su-qr-text'},
        h('p', {class: 'su-qr-do', text: "Point your phone's camera at this code."}),
        h('p', {class: 'su-hint', text: 'Your phone must be on this Wi-Fi. Then allow it under Waiting for approval.'}))));
  det.addEventListener('toggle', () => { SU.qr = det.open; });
  return det;
}
async function copyAddress(addr) {
  delete SU.errs.address;
  try {
    await navigator.clipboard.writeText(addr);
    toast('Copied');
  } catch (e) {
    SU.errs.address = 'Could not copy. Select the address and copy it.';
    renderView('[data-focus="copy-address"]');
  }
}
function pendingBox(d) {
  return h('div', {class: 'su-stack'},
    h('div', {class: 'su-card-s'}, d.pending.map(p => h('div', {class: 'su-cred'},
      h('span', {class: 'su-cred-l su-mono', text: p.ip}),
      h('div', {class: 'su-cred-v'}, h('span', {'data-seen-ip': p.ip, text: 'tried to connect ' + agoText(Date.now() / 1000 - p.last_seen)})),
      h('div', {class: 'su-cred-a'},
        btn('Allow', () => allowDevice(p.ip), 'go sm', {'data-focus': 'allow:' + p.ip}),
        btn('Dismiss', () => dismissDevice(p.ip), 'ghost sm', {'data-focus': 'dismiss:' + p.ip}))))),
    h('p', {class: 'su-foot-note', text: 'Only allow a device you recognise: anyone on your network can ask.'}),
    errAt('pending'));
}
async function allowDevice(ip) {
  delete SU.errs.pending;
  const r = await api('POST', '/api/setup/devices', {ip, name: ''});
  if (!r.ok) { SU.errs.pending = r.text; renderView(`[data-focus="allow:${ip}"]`); return; }
  applyDevices(r.data);
  toast(`${ip} can now connect.`, () => undoAllow(ip));
  renderView('[data-focus="pane-title"]');
}
async function undoAllow(ip) {
  const r = await api('DELETE', '/api/setup/devices/' + encodeURIComponent(ip), {});
  if (!r.ok) { SU.errs.list = r.text; renderView('[data-focus="pane-title"]'); return; }
  applyDevices(r.data);
  toast(`${ip} is no longer allowed.`);
  renderView('[data-focus="pane-title"]');
}
async function dismissDevice(ip) {
  delete SU.errs.pending;
  const r = await api('POST', '/api/setup/devices/dismiss', {ip});
  if (!r.ok) { SU.errs.pending = r.text; renderView(`[data-focus="dismiss:${ip}"]`); return; }
  applyDevices(r.data);
  toast(`${ip} dismissed.`);
  renderView('[data-focus="pane-title"]');
}
function allowedBox(d) {
  const kids = [];
  if (d.devices.length) kids.push(h('div', {class: 'su-card-s'}, d.devices.map(x => cred(deviceLabel(x),
    x.name ? h('span', {class: 'su-mono', text: x.ip}) : null,
    btn('Remove', () => removeDevice(x), 'ghost sm', {'data-focus': 'remove:' + x.ip})))));
  else kids.push(h('p', {class: 'su-hint', text: 'No devices yet. Turn sharing on, open the address on the other computer, then allow it here.'}));
  if (d.command_line.length) kids.push(h('div', {class: 'su-card-s'}, d.command_line.map(n => cred(n, 'Set when Kinora was started'))));
  kids.push(errAt('list'));
  return h('div', {class: 'su-stack'}, kids);
}
async function removeDevice(d) {
  delete SU.errs.list;
  const r = await api('DELETE', '/api/setup/devices/' + encodeURIComponent(d.ip), {});
  if (!r.ok) { SU.errs.list = r.text; renderView(`[data-focus="remove:${d.ip}"]`); return; }
  applyDevices(r.data);
  toast(`${deviceLabel(d)} can no longer connect.`, () => restoreDevice(d));
  renderView('[data-focus="pane-title"]');
}
async function restoreDevice(d) {
  const r = await api('POST', '/api/setup/devices', {ip: d.ip, name: d.name || ''});
  if (!r.ok) { SU.errs.list = r.text; renderView('[data-focus="pane-title"]'); return; }
  applyDevices(r.data);
  toast(`${deviceLabel(d)} can connect again.`);
  renderView('[data-focus="pane-title"]');
}
// Add a device: a ghost button under the list. Pressing it opens the form in place (SU.edit 'device-add', collapsed by default).
function addDeviceArea() {
  if (SU.edit !== 'device-add') return h('div', {class: 'su-bar'},
    h('button', {type: 'button', class: 'su-btn ghost', 'data-focus': 'dev-open', onclick: openDeviceForm}, icon('plus', 'su-btn-ico'), 'Add a device'));
  return addDeviceForm();
}
function openDeviceForm() {
  startEdit('device-add', 'dev-open');
  const ip = document.querySelector('#setup [data-focus="dev-ip"]');
  if (ip) ip.focus();
}
// Add a device: IP (required) and Name (optional). The draft lives in SU.devForm, so a redraw keeps what was typed.
function addDeviceForm() {
  const f = SU.devForm;
  const ip = h('input', {type: 'text', inputmode: 'decimal', autocomplete: 'off', spellcheck: 'false', value: f.ip, 'data-focus': 'dev-ip',
    oninput: () => { f.ip = ip.value; setErr(ip, ''); }});
  const name = h('input', {type: 'text', maxlength: '60', autocomplete: 'off', value: f.name, 'data-focus': 'dev-name',
    oninput: () => { f.name = name.value; setErr(name, ''); }});
  const submit = h('button', {type: 'submit', class: 'su-btn go', 'data-focus': 'dev-add', text: 'Add device'});
  return h('form', {class: 'su-stack', novalidate: true, onsubmit: e => { e.preventDefault(); addDevice(); }},
    labelled('IP address', ip, 'The address the other computer has on your network, such as 192.168.1.20.'),
    labelled('Name (optional)', name, 'For example Anna\'s laptop.'),
    errAt('add'),
    h('div', {class: 'su-bar'}, btn('Cancel', endEdit, 'ghost', {'data-focus': 'dev-cancel'}), submit));
}
async function addDevice() {
  const f = SU.devForm, ipEl = document.querySelector('#setup [data-focus="dev-ip"]'), nameEl = document.querySelector('#setup [data-focus="dev-name"]');
  const eIp = validateDeviceIp(f.ip), eName = validateDeviceName(f.name);
  if (ipEl) setErr(ipEl, eIp);
  if (nameEl) setErr(nameEl, eName);
  if (eIp || eName) { (eIp && ipEl ? ipEl : nameEl).focus(); return; }
  delete SU.errs.add;
  const ip = f.ip.trim(), name = f.name.trim();
  const r = await api('POST', '/api/setup/devices', {ip, name});
  if (!r.ok) { SU.errs.add = r.text; renderView('[data-focus="dev-ip"]'); return; }
  applyDevices(r.data);
  SU.devForm = {ip: '', name: ''}; SU.edit = ''; SU.opener = '';
  toast(`${name || ip} can now connect.`);
  renderView('[data-focus="dev-open"],[data-focus="pane-title"]');
}

// Verify (state table): the Verify button of one camera. One camera at a time; a refused login stops the run.
async function verifyCam(c) {
  if (SU.busy) return;
  SU.busy = c.id; delete SU.deadline[c.id]; delete SU.vs[c.id]; SU.errs = {}; SU.saved = '';
  renderView(`[data-focus="card:${c.id}"],[data-focus="pane-title"]`);
  try {
    const r = await api('POST', '/api/setup/verify', {id: c.id});
    const d = r.data || {};   // a body that is not JSON must not leave the page stuck in "verifying"
    if (!r.ok) SU.vs[c.id] = {kind: 'note', text: plainError(r.text)};
    else if (d.state === 'ok') {
      SU.vs[c.id] = {kind: 'ok'};
      SU.changed = true;
      if (d.mismatch) { const body = modelFixBody(d.model); if (body) await api('PATCH', `/api/cameras/${c.id}`, body); }   // the device's model, silently
    } else if (d.state === 'waiting') {
      const ms = Math.max(0, Number(d.wait_s) || 0) * 1000;
      SU.deadline[c.id] = Date.now() + ms;
      SU.span[c.id] = ms;
    } else {
      SU.vs[c.id] = {kind: 'failed', reason: String(d.reason || '')};
      if (isAuthFailure(d.reason)) SU.authStop = c.id;
    }
  } finally {
    SU.busy = '';
  }
  await refresh();
  renderView(`[data-focus="act:${c.id}"],[data-focus="card:${c.id}"],[data-focus="pane-title"]`);
}
// Countdown: text once a second (aria-hidden). When a wait ends the page is redrawn, unless a text field has focus.
function tickCountdowns() {
  const now = Date.now();
  paintSeen();
  let ended = false;
  document.querySelectorAll('#setup [data-until]').forEach(n => {
    const left = (+n.dataset.until - now) / 1000;
    if (left > 0) {
      if (n.classList.contains('su-bar-fill')) n.style.setProperty('--frac', String(Math.min(1, left * 1000 / (+n.dataset.span || 1))));
      else n.textContent = formatCountdown(left);
      return;
    }
    n.removeAttribute('data-until');
    ended = true;
    if (n.classList.contains('su-bar-fill')) n.style.setProperty('--frac', '0');
    else n.textContent = '';
  });
  const a = document.activeElement, typing = !!a && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName);
  if (ended && SU.view !== 'add' && !typing) renderView();
}

// Called from the player's app bar (Add camera): Settings with the new-camera pane open.
function openSetupAdd() { SU.sel = 'new'; openSetup('settings', 'new'); }

// ---------- boot ----------
async function boot() {
  byId('settingsbtn').addEventListener('click', () => openSetup('settings'));
  byId('setupback').addEventListener('click', topBack);
  byId('setup').addEventListener('keydown', onSetupKey);
  window.addEventListener('popstate', onPop);
  byId('sumark').replaceChildren(icon('mark', 'su-mark'));
  setInterval(tickCountdowns, 1000);
  setInterval(devicesTick, 5000);
  const [cams, st] = await Promise.all([api('GET', '/api/cameras'), api('GET', '/api/setup/state')]);
  const stateOk = st.ok && st.data && Array.isArray(st.data.cameras);   // a body that is not the expected JSON is a failed read
  if (stateOk) absorb(st.data);
  else {
    absorb({cameras: [], account: {email_set: false, email: null}, prerequisites: {}, step: 'prepare'});
    SU.loadErr = 'Could not read the setup state: ' + plainError(st.text);
  }
  SU.playable = !!(cams.ok && Array.isArray(cams.data) && cams.data.some(isPlayable));   // the same rule as app.js
  byId('settingsbtn').hidden = !SU.playable;
  if (SU.playable) return;   // app.js has started the timeline
  const step = stateOk ? st.data.step : 'prepare';
  openSetup(['prepare', 'account', 'verify'].includes(step) ? step : (step === 'done' ? 'verify' : 'prepare'));
}
boot();
