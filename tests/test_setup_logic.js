// Offline test of the setup wizard and Settings pure logic (static/setup.js between the setup-logic markers). Run: node tests/test_setup_logic.js
const fs = require('fs'), vm = require('vm');
const src = fs.readFileSync(__dirname + '/../kinora/web/static/setup.js', 'utf8');
const block = src.split('/* setup-logic:begin */')[1].split('/* setup-logic:end */')[0];
const names = ['signInText', 'signInValue', 'cameraAccountGiven', 'loginLabel', 'isPlayable', 'formatCountdown', 'validateIp', 'isPrivateIp', 'validateAddress',
  'validateName', 'validateEmail', 'validateSecret', 'validateAddForm', 'numberError', 'verifyHold', 'modelMismatch', 'seenModel',
  'modelPatchBody', 'modelFixBody', 'isAuthFailure', 'dialogCopy', 'friendlyVerifyReason', 'plainError', 'camState', 'liveRow',
  'pickSelection', 'verifyTitle', 'verifyLead', 'openReason', 'prereqRows', 'themeLabel', 'themeAria', 'themeIconName',
  'addButtonLabel', 'accountPlan', 'validateAddRow', 'addBatchPlan', 'addBatchBody', 'runAddBatch', 'movedOrder'];
const ctx = {}; vm.createContext(ctx);
vm.runInContext(block + ';' + names.map(n => `this.${n}=${n};`).join(''), ctx);
const F = ctx;
let ok = true;
const check = (n, c) => { console.log((c ? 'PASS ' : 'FAIL ') + n); ok = ok && !!c; };

// The logic must not touch the page: no DOM, no network, no storage.
check('logic block has no DOM, network or storage access', !/document\.|window\.|fetch\(|localStorage|sessionStorage|XMLHttpRequest/.test(block));

// Sign-in is automatic: no model or sign-in question. The explicit methods of older cameras keep their labels.
check('sign-in: automatic is the text for the automatic method and for a camera with none', F.loginLabel('auto') === 'automatic' && F.signInText('auto') === 'Sign-in: automatic' && F.loginLabel(undefined) === 'automatic');
check('login labels for the explicit methods', F.loginLabel('admin_cloud') === 'Tapo account' && F.loginLabel('cam_cloud') === 'Camera account' && F.signInText('cam_cloud') === 'Sign-in: Camera account');
check('sign-in value: automatic with the TP-Link account, or Manual for an explicit method', F.signInValue('auto') === 'Automatic, with your TP-Link account' && F.signInValue(undefined) === F.signInValue('auto') && F.signInValue('cam_cloud') === 'Manual');

// Countdown text
check('countdown under a minute is in seconds', F.formatCountdown(45) === '45 s' && F.formatCountdown(0) === '0 s');
check('countdown minutes and seconds', F.formatCountdown(125) === '2:05' && F.formatCountdown(180) === '3:00');
check('countdown rounds up and never goes negative', F.formatCountdown(59.2) === '1:00' && F.formatCountdown(-3) === '0 s' && F.formatCountdown('x') === '0 s');

// IP, name, email, secret validation (mirrors the server rules)
check('IPv4 accepted, spaces trimmed', F.validateIp('192.168.1.201') === '' && F.validateIp(' 10.0.0.5 ') === '');
check('IPv4 rejected: short, large octet, leading zero, letters', F.validateIp('192.168.1') !== '' && F.validateIp('256.1.1.1') !== '' && F.validateIp('01.2.3.4') !== '' && F.validateIp('a.b.c.d') !== '');
check('private ranges only: 10/8, 172.16-31, 192.168', F.validateAddress('10.0.0.1') === '' && F.validateAddress('172.16.0.1') === '' && F.validateAddress('172.31.255.255') === '' && F.validateAddress('192.168.1.5') === '');
check('public and near-miss ranges refused', F.validateAddress('8.8.8.8') !== '' && F.validateAddress('172.15.0.1') !== '' && F.validateAddress('172.32.0.1') !== '' && F.validateAddress('192.169.0.1') !== '');
check('isPrivateIp agrees with validateAddress', F.isPrivateIp('172.20.1.1') === true && F.isPrivateIp('100.64.0.1') === false);
check('name: required, trimmed, at most 60', F.validateName('') !== '' && F.validateName('   ') !== '' && F.validateName('a'.repeat(61)) !== '' && F.validateName('Kitchen') === '' && F.validateName('a'.repeat(60)) === '');
check('email: shape checked, trimmed', F.validateEmail('a@b.co') === '' && F.validateEmail(' me@x.org ') === '' && F.validateEmail('nope') !== '' && F.validateEmail('a@b') !== '' && F.validateEmail('a b@c.com') !== '');
check('secret: required and at most 256', F.validateSecret('', 'pw') !== '' && F.validateSecret('x', 'pw') === '' && F.validateSecret('x'.repeat(257), 'pw') !== '');
const addOk = {name: 'Kitchen', ip: '192.168.1.201', user: 'rtspadmin', password: 'pw'};
check('add form: complete record is valid', F.validateAddForm(addOk) === '');
check('add form: the camera account is optional (no password, no user needed)', F.validateAddForm({name: 'Kitchen', ip: '192.168.1.201'}) === ''
  && F.validateAddForm({...addOk, password: ''}) === '');
check('add form: a camera password without a user name is refused', F.validateAddForm({...addOk, user: ''}) !== '');
check('add form: the first problem is reported', F.validateAddForm({...addOk, name: ''}).includes('name'));
check('add form: the sign-in method is not asked any more', F.validateAddForm({...addOk, auth: ''}) === '');

// Clip piece length and pan/tilt step: a number in range, whole when asked
check('number: clip piece length is a whole number from 1 to 3600', F.numberError('Clip piece length', 1, 3600, true, '30') === ''
  && F.numberError('Clip piece length', 1, 3600, true, '30.5') !== '' && F.numberError('Clip piece length', 1, 3600, true, '0') !== ''
  && F.numberError('Clip piece length', 1, 3600, true, '3601') !== '' && F.numberError('Clip piece length', 1, 3600, true, '') !== '');
check('number: pan and tilt step may be fractional, from 1 to 180', F.numberError('Pan and tilt step', 1, 180, false, '2.5') === ''
  && /from 1 to 180/.test(F.numberError('Pan and tilt step', 1, 180, false, '181')) && F.numberError('Pan and tilt step', 1, 180, false, 'x') !== '');

// Verify rules: one at a time, and a refused login stops the run
check('verify: free when nothing is running', F.verifyHold({id: 'c200'}, '', '') === '');
check('verify: held while another camera runs', F.verifyHold({id: 'c200'}, 'c199', '') !== '' && F.verifyHold({id: 'c200'}, 'c200', '') !== '');
check('verify: stopped by a refused login for every other camera, not for that one', F.verifyHold({id: 'c199'}, '', 'c200') !== '' && F.verifyHold({id: 'c200'}, '', 'c200') === ''
  && /change the TP-Link password first/.test(F.verifyHold({id: 'c199'}, '', 'c200')));
check('auth failure recognised, other failures not', F.isAuthFailure('Kitchen: login failed: The camera refused the TP-Link password. Check it') && F.isAuthFailure('Invalid authentication') && !F.isAuthFailure('the camera is not reachable on the network'));
check('a refused camera account password also stops the run (cam_cloud)', F.isAuthFailure('Kitchen: login failed: The camera refused the camera account password. Check it in Settings') && !F.isAuthFailure('no camera account password stored'));

// Dialog copy: the words the owner sees before a removal, an address change or a sign-in change
check('remove dialog: title names the camera, destructive, recordings untouched', (() => { const d = F.dialogCopy('remove', 'Kitchen');
  return d.title === 'Remove Kitchen?' && d.ok === 'Remove camera' && d.bad === true && /SD card are not touched/.test(d.body); })());
check('address dialog: the camera stays off until it is verified at the new address', (() => { const d = F.dialogCopy('address', 'Kitchen');
  return d.title === "Change Kitchen's address?" && d.ok === 'Change address' && /stays off until you verify it at the new address/.test(d.body) && d.bad === false; })());
check('automatic sign-in dialog: nothing signs in now', (() => { const d = F.dialogCopy('auto', 'Kitchen');
  return d.title === 'Use automatic sign-in for Kitchen?' && d.body === 'Kitchen stays off until you verify it again. Nothing signs in now.' && d.ok === 'Use automatic sign-in'; })());

// Friendly error text: never the raw gate text
const raw = 'Kitchen is disabled in cameras.json; run python -m kinora.camera.gate credentials-changed c201 --reset-auth';
check('gate text with cameras.json or CLI is not shown raw', !/cameras\.json|credentials-changed|--reset-auth/.test(F.friendlyVerifyReason(raw)) && F.friendlyVerifyReason(raw).includes('switched this camera off'));
const refusedText = 'The camera refused the TP-Link password. Check it (it must be the account that owns this camera), then Verify once after the wait shown.';
check('refused sign-in: the server sentence is shown as it is, with the next step', F.friendlyVerifyReason('Kitchen: login failed: ' + refusedText) === refusedText);
check('a cam_cloud camera that changed its sign-in is told to save the camera account', F.friendlyVerifyReason('Kitchen is disabled: The camera changed how it signs in. Save the camera account again in Settings, then Verify.')
  === 'The camera changed how it signs in. Save the camera account again in Settings, then Verify.');
check('TP-Link account first and the changed sign-in: the server sentence is shown', F.friendlyVerifyReason('Save your TP-Link account first') === 'Save your TP-Link account first'
  && /changed how it signs in/.test(F.friendlyVerifyReason('Kitchen is disabled: The camera changed how it signs in. Save the TP-Link password again in Settings, then Verify.')));
check('lockout: the minutes from the camera are used', /25 minutes/.test(F.friendlyVerifyReason('x: the camera locked itself after too many login attempts (about 25 min left)')));
check('gate budget and spacing have their own explanations', /allowance/.test(F.friendlyVerifyReason('x: login budget for this hour used up')) && /spaced out/.test(F.friendlyVerifyReason('x: waiting 40 s before the next login')));
check('lockout and no-answer copy use the agreed wording', /paused sign-ins for about 25 minutes/.test(F.friendlyVerifyReason('x: about 25 min left')) && /^No answer from the camera\./.test(F.friendlyVerifyReason('x: Max retries exceeded')));
check('user-facing sign-in copy never says login for the refused, lockout and network texts', ['x: login failed: ' + refusedText, 'x: about 25 min left', 'x: Max retries exceeded'].every(t => !/login/i.test(F.friendlyVerifyReason(t))));
check('unknown text falls back to a generic line, never the raw text', F.friendlyVerifyReason('weird internal detail 42').indexOf('weird') === -1 && F.friendlyVerifyReason('').length > 0);
check('a refused camera account password is shown as its own sentence, with the automatic sign-in hint', (() => {
  const r = F.friendlyVerifyReason('x: login failed: The camera refused the camera account password. Check it in Settings, then Verify once after the wait shown. To sign in with the TP-Link account instead, use Use automatic sign-in in Settings.');
  return /camera account password/.test(r) && /Use automatic sign-in/.test(r) && !/TP-Link password/.test(r); })());
check('a camera with no camera account is told to add it in Settings (for Live view)', /Live view/.test(F.friendlyVerifyReason('x: this camera has no camera account password stored: add it in Settings')));
check('plainError: file or CLI hints are replaced, normal text kept', F.plainError('see cameras.json').indexOf('cameras.json') === -1 && F.plainError('another camera is being verified: wait for it to finish') === 'another camera is being verified: wait for it to finish' && F.plainError('').length > 0);

// Card state (wizard-ui-v2 table): one chip, at most one alert, one action
const base = {enabled: false, verified: false, status: 'not_verified', mac_mismatch: false, secret_sources: {}};
const st = (cam, v, busy, waiting) => F.camState({...base, ...cam}, v || null, !!busy, !!waiting);
check('card: a new camera is Not verified with the Verify action and no alert', (() => { const s = st({});
  return s.key === 'new' && s.chip.label === 'Not verified' && s.chip.tone === 'mut' && s.action.key === 'verify' && s.alert === null && s.nav === 'Not verified'; })());
check('card: verified and on is Ready with no alert, no action and no sidebar line', (() => { const s = st({verified: true, enabled: true, status: 'ok'});
  return s.key === 'ok' && s.chip.label === 'Ready' && s.chip.tone === 'ok' && s.alert === null && s.action === null && s.nav === ''; })());
check('card: verified and switched off offers Turn on', (() => { const s = st({verified: true, status: 'ok'});
  return s.key === 'off' && s.chip.label === 'Off' && s.action.key === 'turnon' && s.nav === 'Off'; })());
check('card: a different device at the address offers Edit address with one alert', (() => { const s = st({verified: true, enabled: true, mac_mismatch: true, status: 'ok'});
  return s.key === 'mac' && s.chip.label === 'Different device' && s.alert.text === 'Another device answers at this address.' && s.action.key === 'address'; })());
check('card: gate pause is "Paused", names the lockout, and offers the TP-Link password', (() => { const s = st({status: 'disabled_by_gate'});
  return s.key === 'paused' && s.chip.label === 'Paused' && /30-minute lockout/.test(s.alert.text) && s.action.key === 'tplink' && s.action.label === 'Change TP-Link password'; })());
check('card: right after a refusal the card says Sign-in refused, even when the server already reports the gate pause', (() => {
  const s = st({status: 'disabled_by_gate'}, {kind: 'failed', reason: 'Kitchen: login failed: The camera refused the TP-Link password. Check it'});
  return s.key === 'refused' && s.chip.label === 'Sign-in refused'; })());
check('card: a refused TP-Link password is "Sign-in refused" with the shared-password sentence', (() => {
  const s = st({}, {kind: 'failed', reason: 'Kitchen: login failed: The camera refused the TP-Link password. Check it'});
  return s.key === 'refused' && s.chip.label === 'Sign-in refused' && /other verifications are paused/i.test(s.alert.text) && s.action.key === 'tplink'; })());
check('card: no answer offers Verify again and the Wi-Fi hint', (() => {
  const s = st({}, {kind: 'failed', reason: 'x: Max retries exceeded'});
  return s.key === 'noanswer' && s.chip.label === 'No answer' && /Wi-Fi/.test(s.alert.text) && s.action.key === 'verifyagain' && s.nav === 'No answer'; })());
check('card: other failures show the plain explanation, Verify again', (() => {
  const s = st({}, {kind: 'failed', reason: 'weird internal detail 42'});
  return s.key === 'failed' && s.alert.text.indexOf('weird') === -1 && s.action.key === 'verifyagain'; })());
check('card: a missing TP-Link account is asked for first (no raw text)', (() => {
  const s = st({}, {kind: 'failed', reason: 'Save your TP-Link account first'});
  return s.key === 'noaccount' && s.action.key === 'account' && s.alert.text === 'Save your TP-Link account first.'; })());
check('card: a wait shows Wait with a countdown and a disabled Verify', (() => { const s = st({status: 'waiting', wait_s: 40});
  return s.key === 'waiting' && s.chip.label === 'Wait' && s.chip.count === true && s.action.off === true && s.alert === null; })());
check('card: a run in progress is Verifying (busy or server status)', st({}, null, true).key === 'verifying' && st({status: 'verifying'}).key === 'verifying'
  && st({}, null, true).action.off === true);
check('card: a note from the page (for example another camera is running) is a warning, not a failure', (() => {
  const s = st({}, {kind: 'note', text: 'another camera is being verified'});
  return s.key === 'note' && s.alert.tone === 'warn' && s.alert.text === 'another camera is being verified'; })());
check('card: a sign-in that failed earlier and is not verified is Last sign-in failed', st({status: 'failed'}).key === 'lastfail');
check('card: every state has a chip label, a tone, a sidebar dot and at most one alert', (() => {
  const cases = [st({}), st({verified: true, enabled: true, status: 'ok'}), st({verified: true, status: 'ok'}),
    st({status: 'disabled_by_gate'}), st({status: 'waiting'}), st({}, null, true),
    st({verified: true, enabled: true, mac_mismatch: true, status: 'ok'}), st({status: 'failed'}),
    st({}, {kind: 'failed', reason: 'Invalid authentication'}), st({}, {kind: 'failed', reason: 'x: Max retries exceeded'})];
  return cases.every(s => s.chip.label && ['ok', 'wait', 'bad', 'mut'].includes(s.chip.tone) && ['ok', 'wait', 'bad', 'mut'].includes(s.dot)
    && (s.alert === null || (s.alert.text && s.alert.tone))); })());

// Live view row: none, saved (the user, never the password), refused by the camera, developer setting
check('live view: not added when no camera account is saved', F.liveRow({secret_sources: {camera: 'missing'}, has_camera_account: false}).mode === 'none');
check('live view: saved shows the user name and no secret, refused when the camera refused it', (() => {
  const r = F.liveRow({secret_sources: {camera: 'wizard'}, has_camera_account: true, camera_user: 'rtspadmin', live_refused: true});
  return r.mode === 'saved' && r.user === 'rtspadmin' && r.refused === true && !('password' in r); })());
check('live view: the developer setting is named as such', F.liveRow({secret_sources: {camera: 'dev_override'}, has_camera_account: false}).mode === 'dev');

// Settings selection: the first camera that needs attention, else the first camera, none without cameras
const cA = {id: 'a', name: 'Baby', enabled: true, verified: true, status: 'ok'}, cB = {id: 'b', name: 'Kitchen', verified: false, status: 'not_verified'};
const cC = {id: 'c', name: 'Living', enabled: true, verified: true, status: 'ok'};
check('selection: the first camera that needs attention', F.pickSelection([cA, cB, cC]) === 'b');
check('order: a camera moves one place up or down', JSON.stringify(F.movedOrder(['a', 'b', 'c'], 'b', -1)) === '["b","a","c"]' && JSON.stringify(F.movedOrder(['a', 'b', 'c'], 'b', 1)) === '["a","c","b"]');
check('order: the ends and unknown ids stay as they are', JSON.stringify(F.movedOrder(['a', 'b'], 'a', -1)) === '["a","b"]' && JSON.stringify(F.movedOrder(['a', 'b'], 'b', 1)) === '["a","b"]' && JSON.stringify(F.movedOrder(['a', 'b'], 'x', 1)) === '["a","b"]');
check('selection: all ready means the first camera, none means empty', F.pickSelection([cA, cC]) === 'a' && F.pickSelection([]) === '');

// Verify page copy: title and lead, and the footer reason beside "Open timeline"
check('verify title: the first step, the counts, and the none-ready title after a try', F.verifyTitle([cB], false) === 'Verify your cameras'
  && F.verifyTitle([cA, cB, cC], true) === '2 of 3 cameras ready' && F.verifyTitle([cB], true) === 'No camera is ready yet'
  && F.verifyTitle([cA, cC], false) === "You're all set" && F.verifyTitle([], false) === 'Add a camera first');
check('verify lead: partial success names the camera that is not verified', /Kitchen isn't verified yet: it stays off until you verify it\./.test(F.verifyLead([cA, cB]))
  && /You can open the timeline now/.test(F.verifyLead([cA, cB])));
check('verify lead: all ready and none ready', F.verifyLead([cA, cC]) === 'Your cameras are ready. Open the timeline to browse the recordings.'
  && /lock for 30 minutes/.test(F.verifyLead([cB])));
check('open timeline: a reason only when no camera is playable, the busy camera named', F.openReason([cA], '') === '' && F.openReason([cB], '') === 'Verify at least one camera to continue.'
  && F.openReason([cB], 'Kitchen') === 'Verifying Kitchen…');

// Model fix: the device's model is applied silently after a Verify that found another model
check('model: names compared without case, other and unknown never flagged', !F.modelMismatch('C200', 'tapo c200 v1') && F.modelMismatch('C200', 'C210') && !F.modelMismatch('other', 'C100') && !F.modelMismatch('C100', ''));
check('model: seenModel finds the preset name only', F.seenModel('tapo c210 v1') === 'C210' && F.seenModel('C220') === '' && F.seenModel('') === '');
check('model: the silent apply body is the reported model with the preset applied, null for an unknown model', JSON.stringify(F.modelFixBody('C200 1.0')) === '{"model":"C200","apply_preset":true}' && F.modelFixBody('C220') === null);
check('model: the Settings body carries the apply flag', JSON.stringify(F.modelPatchBody('C100', false)) === '{"model":"C100","apply_preset":false}');

// Prerequisites (video tools, saved passwords) and how to fix them: plain words, no file names or install commands
const good = {ffmpeg: true, ffprobe: true, credential_store: {name: 'keyring', ok: true}};
check('prerequisites: all present', F.prereqRows(good).every(r => r.ok));
const missing = F.prereqRows({ffmpeg: false, ffprobe: true, credential_store: {name: 'keyring', ok: false, reason: 'no usable OS keyring'}});
check('prerequisites: missing FFmpeg is said in plain words, with a How to install link', missing[0].ok === false
  && missing[0].fix === 'This computer is missing FFmpeg, a free video tool the app needs.' && missing[0].link.text === 'How to install');
check('prerequisites: keyring not usable is not ok, file store is ok', missing[1].ok === false && F.prereqRows({ffmpeg: true, ffprobe: true, credential_store: {name: 'file', ok: false}})[1].ok === true);
check('prerequisites: two rows, labelled Video tools and Saved passwords', missing.length === 2 && missing[0].label === 'Video tools' && missing[1].label === 'Saved passwords');
check('prerequisites: no fix text names a file, a setting, an environment variable or an install command', !/KINORA_|settings\.json|brew|winget|apt|credential_store|keychain|keyring/i.test(JSON.stringify(missing)));

// Playable rule (the timeline lists only verified and enabled cameras)
check('playable: verified and enabled only', F.isPlayable({enabled: true, verified: true}) && !F.isPlayable({enabled: false, verified: true}) && !F.isPlayable({enabled: true, verified: false}) && !F.isPlayable(undefined));

// Theme label text in the Settings pane and the player header
check('theme: label, aria text and icon per choice', F.themeLabel('system') === 'Theme: Auto' && F.themeLabel('light') === 'Theme: Light' && F.themeLabel('dark') === 'Theme: Dark'
  && F.themeAria('system') === 'Colour theme: Auto, switch' && F.themeIconName('light') === 'sun' && F.themeIconName('dark') === 'moon' && F.themeIconName('system') === 'half');

// Add button label
check('add button: singular and plural', F.addButtonLabel(1) === 'Add camera' && F.addButtonLabel(3) === 'Add 3 cameras');

// Accounts step: the TP-Link account only (one email and password, both required)
const tpOk = F.accountPlan({email: ' me@example.com ', password: 'tp-secret'});
check('accounts: a complete TP-Link pair is sent trimmed as email and password only', JSON.stringify(tpOk.body) === '{"email":"me@example.com","password":"tp-secret"}');
check('accounts: both fields are required and nothing is sent while either is wrong', F.accountPlan({email: '', password: ''}).body === null
  && F.accountPlan({email: '', password: ''}).errors.email !== '' && F.accountPlan({email: 'me@example.com', password: ''}).errors.password !== ''
  && F.accountPlan({email: 'me@example.com', password: ''}).body === null);
check('accounts: a camera pair is not part of the TP-Link plan', !('camera_user' in F.accountPlan({email: 'me@example.com', password: 'tp-secret', camUser: 'rtspadmin', camPassword: 'cam'}).body));

// Add rows: the camera account (for Live view) is optional and goes with the row only when its password is typed
const okRow = {name: 'Kitchen', ip: '192.168.1.201', user: 'rtspadmin', password: 'cam-pw'};
check('add row: a row with name and address is ready, with or without a camera account', Object.keys(F.validateAddRow(okRow, {})).length === 0
  && Object.keys(F.validateAddRow({name: 'Kitchen', ip: '192.168.1.201', user: 'rtspadmin', password: ''}, {})).length === 0);
check('add row: a camera password needs a user name (never sent alone)', F.validateAddRow({...okRow, user: ' '}, {}).user !== undefined);
check('add row: a user name over 64 characters is refused', F.validateAddRow({...okRow, user: 'u'.repeat(65)}, {}).user !== undefined);
check('add row: a user name typed without a password is not a camera account', Object.keys(F.validateAddRow({...okRow, password: ''}, {})).length === 0
  && F.cameraAccountGiven({user: 'rtspadmin', password: ''}) === false && F.cameraAccountGiven(okRow) === true);
check('add row: name and address errors name the field', F.validateAddRow({...okRow, name: '', ip: 'nope'}, {}).name !== undefined
  && F.validateAddRow({...okRow, name: '', ip: 'nope'}, {}).ip !== undefined);
check('add body: name and address, and the camera account when one is given', JSON.stringify(F.addBatchBody(okRow))
  === '{"name":"Kitchen","ip":"192.168.1.201","camera_user":"rtspadmin","camera_password":"cam-pw"}');
check('add body: no model and no sign-in method are sent; no camera account when none is typed', JSON.stringify(F.addBatchBody({...okRow, password: ''}))
  === '{"name":"Kitchen","ip":"192.168.1.201"}');
check('add body: name, address and user name trimmed', (() => { const b = F.addBatchBody({...okRow, name: ' Kitchen ', ip: ' 192.168.1.201 ', user: ' admin2 '});
  return b.name === 'Kitchen' && b.ip === '192.168.1.201' && b.camera_user === 'admin2'; })());
check('add plan: rows already added are skipped', JSON.stringify(F.addBatchPlan([{added: true}, {}, {added: false}, {added: true}])) === '[1,2]');

// Stop-on-first-error: rows after a failure are not sent; rows before stay added and are not sent again
const sent = [];
const rowsA = [{added: true}, {}, {}, {}];
const fakeSend = async (i, body) => { sent.push(i); return i === 2 ? {ok: false, text: 'name in use'} : {ok: true, id: 'cam' + i}; };
const added = [];
F.runAddBatch(rowsA, fakeSend, (i, id) => added.push([i, id])).then(res => {
  check('batch: skips rows already added and sends the rest in order', JSON.stringify(sent) === '[1,2]');
  check('batch: stops at the first error and reports its row', res.stopped === 2 && res.text === 'name in use');
  check('batch: successes are reported once each', JSON.stringify(added) === '[[1,"cam1"]]');
  finish();
});

// Static checks of the page's CSS and markup (no browser here): the fixes that a pattern can prove.
const html = fs.readFileSync(__dirname + '/../kinora/web/index.html', 'utf8');
const appjs = fs.readFileSync(__dirname + '/../kinora/web/static/app.js', 'utf8');
const count = re => (html.match(re) || []).length;
check('tokens: light track and a quiet recording colour (events carry the contrast)', html.includes('--track:#EEEEF4;--rec:#9FA1B8'));
check('tokens: dark timeline uses the surface and a quiet recording colour, in both dark blocks', count(/--track:var\(--c-surface-2\);--rec:#5C5F75/g) === 2);
check('tokens: dark strong line and secondary text, in both dark blocks', count(/--c-line-strong:#6B6F86/g) === 2 && count(/--c-text-3:#8D90A2/g) === 2);
check('tokens: light strong line and text tokens (cool neutrals)', html.includes('--c-line-strong:#8B8CA0') && html.includes('--c-text-2:#565869') && html.includes('--c-text-3:#666879'));
check('timeline: rounded strip, hatched no-footage gaps and merged event ranges', /roundRect\(0,TT,W,H-TT,8\)/.test(appjs) && /mergeEvents\(events\.filter/.test(appjs) && /gapsOf\(clips,dayStart/.test(appjs));
check('specificity: the field rule is :where() (0,0,0 extra), so the number, invalid, hover and focus rules win', html.includes('#setup :where(input:not([type=radio],[type=checkbox]),select){')
  && !html.includes('#setup input:not([type=radio]):not([type=checkbox]),#setup select{width'));
check('specificity: the number row keeps its width and does not grow', html.includes('.su-tog-r .su-num-in{flex:none}'));
check('switch: a 44px hit area around the 36x20 switch (inset -12px -4px), thumb is --c-on-accent', /\.su-switch\{position:relative;/.test(html)
  && html.includes('.su-switch::before{content:"";position:absolute;inset:-12px -4px}') && html.includes('background:var(--c-on-accent);transition:transform'));
check('dialog: a darker backdrop in both themes', html.includes('.su-dlg::backdrop{background:rgba(0,0,0,.45)}'));
check('toast: has a 1px strong border', /\.su-toast\{[^}]*border:1px solid var\(--c-line-strong\)/.test(html));
check('toast: lifts the bottom offset only for the phone wizard footer', !/\n\s*\.su-toast\{bottom:96px\}/.test(html) && html.includes('#setup:has(#setupfoot:not(:empty)) .su-toast{bottom:96px}'));
check('selection: selected segments and sidebar rows have the agreed states', html.includes('.pb-seg-btn.on,.pb-seg-btn[aria-checked="true"]{background:var(--c-surface);border:1px solid var(--c-line-strong)')
  && html.includes('.su-seg-btn[aria-pressed="true"],.su-seg-btn[aria-checked="true"]{background:var(--c-surface);border:1px solid var(--c-line-strong)')
  && html.includes('.su-row[aria-current="page"]{background:var(--c-accent-soft);color:var(--c-accent-text);font-weight:600;box-shadow:inset 3px 0 0 var(--c-accent)}')
  && html.includes('--c-accent-soft:#E8E8F8'));
check('events header: the desktop card header is the surface colour', /#evhead\{position:sticky;top:0;z-index:2;background:var\(--c-surface\)/.test(html));
check('footer: the wizard footer has rounded bottom corners, square on phones', html.includes('border-radius:0 0 calc(var(--r-lg) - 1px) calc(var(--r-lg) - 1px)}') && /#setupfoot\{[^}]*border-radius:0\}/.test(html));
check('css: the setup block uses no hard-coded colour (the dialog backdrop is the one given value)', (() => {
  const a = html.indexOf('/* setup wizard and Settings'), b = html.indexOf('/* transport: the right group');
  const block = html.slice(a, b).replace('rgba(0,0,0,.45)', '');
  return a > 0 && b > a && !/#[0-9a-fA-F]{3,8}\b|rgba?\(/.test(block);
})());
const setupjs = fs.readFileSync(__dirname + '/../kinora/web/static/setup.js', 'utf8');
check('errors: field errors carry no role=alert (errAt and the number row)', !/function errAt[^\n]*role/.test(setupjs) && !/const err = h\('p', \{class: 'su-err', role/.test(setupjs));
check('theme: a radio group with arrow keys (role radiogroup and radio, one tab stop)', setupjs.includes("role: 'radiogroup'") && setupjs.includes("role: 'radio'") && setupjs.includes('function themeKeys'));
check('headings: the sidebar group labels are not headings', !/h3', \{class: 'su-grp-h'/.test(setupjs) && setupjs.includes("'aria-labelledby': id"));
check('phone header: the detail screen names the item, not Settings', setupjs.includes("settings ? (detail ? paneTitle() : 'Settings') : 'Camera setup'"));
check('no inline style attribute or inline handler is added to the page', !/\sstyle=|\son(click|change|input|keydown)=/.test(html + setupjs));

function finish() {
  console.log(ok ? 'ALL PASS' : 'SOME FAILED'); process.exit(ok ? 0 : 1);
}
