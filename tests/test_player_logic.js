// Offline test of the player's pure logic (static/app.js between the player-logic markers): status line, mode control,
// camera switcher. Run: node tests/test_player_logic.js
const fs = require('fs'), vm = require('vm');
const src = fs.readFileSync(__dirname + '/../kinora/web/static/app.js', 'utf8');
const block = src.split('/* player-logic:begin */')[1].split('/* player-logic:end */')[0];
const names = ['gapsOf', 'mergeEvents', 'clockShort', 'clockFull', 'statusLine', 'modeAction', 'modeCaption', 'tabTarget', 'rovingTabIndex', 'countText', 'countChip', 'exportLabel', 'eventsPanelState', 'filtersVisible', 'newestFirst', 'rovingEventIndex', 'timelineKeyStep', 'thumbOutcome', 'thumbPick', 'thumbWakeMs', 'thumbMayRun', 'thumbSlotClass', 'THUMB_BACKOFF_MS', 'THUMB_MAX_TRIES'];
const ctx = {}; vm.createContext(ctx);
vm.runInContext(block + ';' + names.map(n => `this.${n}=${n};`).join(''), ctx);
const F = ctx;
let ok = true;
const check = (n, c) => { console.log((c ? 'PASS ' : 'FAIL ') + n); ok = ok && !!c; };

// The logic must not touch the page: no DOM, no network, no storage.
check('logic block has no DOM, network or storage access', !/document\.|window\.|fetch\(|localStorage|sessionStorage|XMLHttpRequest|\$\(/.test(block));

// Clock text (local time, the same wording as the status copy)
const at = (h, m, s = 0) => new Date(2026, 10, 23, h, m, s).getTime() / 1000;
check('short clock: 2:41 pm, 12:05 am, 12:00 pm', F.clockShort(at(14, 41)) === '2:41 pm' && F.clockShort(at(0, 5)) === '12:05 am' && F.clockShort(at(12, 0)) === '12:00 pm');
check('full clock: 2:41:08 pm and 9:03:07 am', F.clockFull(at(14, 41, 8)) === '2:41:08 pm' && F.clockFull(at(9, 3, 7)) === '9:03:07 am');

// Status line: only problems and actionable states have text; healthy states (connecting, live, recorded, idle) are empty
const line = st => F.statusLine(st);
const empty = st => line(st).text === '';
check('healthy: connecting, live (muted or not), recorded and idle give no text', empty({kind: 'connecting', name: 'Living Room'}) && empty({kind: 'live', muted: true}) && empty({kind: 'live', muted: false}) && empty({kind: 'recorded', at: at(14, 41), rate: 4}) && empty({kind: 'idle'}));
check('sound state is not in the status line', !/Mute|Sound|unmute/i.test(JSON.stringify([line({kind: 'live', muted: true}), line({kind: 'live', muted: false})])));
check('unknown kinds: empty text, mut', JSON.stringify(line({kind: 'nope'})) === JSON.stringify({text: '', tone: 'mut'}));
check('busy with a retry count, warn', JSON.stringify(line({kind: 'busy', secs: 60})) === JSON.stringify({text: 'Camera busy: another app is watching. Retrying in 60 s.', tone: 'warn'}));
check('busy without retry: no countdown text', line({kind: 'busy', secs: 0}).text === 'Camera busy: another app is watching.');
check('paused: warn, minutes', JSON.stringify(line({kind: 'paused', mins: 12})) === JSON.stringify({text: 'Sign-ins paused 12 min to protect the camera.', tone: 'warn'}));
check('offline: bad, names the camera', JSON.stringify(line({kind: 'offline', name: 'Kitchen'})) === JSON.stringify({text: "Kitchen isn't answering on the network.", tone: 'bad'}));
check('disabled: bad, names the camera and the reason', JSON.stringify(line({kind: 'disabled', name: 'Kitchen', reason: 'off in Settings'})) === JSON.stringify({text: 'Kitchen is disabled: off in Settings', tone: 'bad'}));
check('not set up: mut', JSON.stringify(line({kind: 'notset'})) === JSON.stringify({text: 'Not set up yet.', tone: 'mut'}));
check('recording in progress: accent, try again', JSON.stringify(line({kind: 'recording'})) === JSON.stringify({text: 'Still recording. Try again in a minute.', tone: 'accent'}));
check('server note (playback ended or paused): its text and tone, warn by default', JSON.stringify(line({kind: 'note', text: 'Stream ended.'})) === JSON.stringify({text: 'Stream ended.', tone: 'warn'}) && line({kind: 'note', text: 'x', tone: 'bad'}).tone === 'bad');
const problems = [{kind: 'busy', secs: 60}, {kind: 'paused', mins: 3}, {kind: 'offline', name: 'X'}, {kind: 'disabled', name: 'X', reason: 'r'}, {kind: 'notset'}, {kind: 'recording'}, {kind: 'note', text: 'n'}].map(line);
check('every problem state has text', problems.every(l => l.text.length > 0));
const healthy = [{kind: 'connecting', name: 'X'}, {kind: 'live', muted: true}, {kind: 'recorded', at: at(1, 2), rate: 16}].map(line);
const every = [...healthy, ...problems];
check('no developer text in any status line (no app.js, no resolution)', every.every(l => !/app\.js|\d+x\d+|v\d+/.test(l.text)));
check('every tone is one of the dot classes', every.every(l => ['ok', 'warn', 'bad', 'accent', 'mut'].includes(l.tone)));

// Mode control: what a click does
check('Live while live: nothing', F.modeAction(true, 'live', true) === 'none' && F.modeAction(true, 'live', false) === 'none');
check('Live on today: start', F.modeAction(false, 'live', true) === 'start');
check('Live on a past day: jump to today', F.modeAction(false, 'live', false) === 'jump');
check('mode tooltips: Live and Recordings explanations (no visible caption)', F.modeCaption(true) === 'Watching live. This is what the camera sees right now.' && F.modeCaption(false) === 'Playing back what the camera saved to its SD card. Pick a time on the timeline or an event.');
check('Recorded while live: stop; while recorded: nothing', F.modeAction(true, 'recorded', true) === 'stop' && F.modeAction(false, 'recorded', true) === 'none');

// Camera switcher: arrow, Home and End targets, and the one tab stop
check('arrows wrap around', F.tabTarget('ArrowRight', 2, 3) === 0 && F.tabTarget('ArrowLeft', 0, 3) === 2 && F.tabTarget('ArrowRight', 0, 3) === 1 && F.tabTarget('ArrowLeft', 1, 3) === 0);
check('Home and End go to the ends', F.tabTarget('Home', 2, 3) === 0 && F.tabTarget('End', 0, 3) === 2);
check('focus outside the list: right goes to the first', F.tabTarget('ArrowRight', -1, 3) === 0);
check('other keys and empty lists are not handled', F.tabTarget('Enter', 1, 3) === null && F.tabTarget('ArrowDown', 1, 3) === null && F.tabTarget('ArrowRight', 0, 0) === null);
check('roving: the selected camera is the only tab stop', [0, 1, 2].map(i => F.rovingTabIndex(1, i)).join(',') === '-1,0,-1');
check('roving: with no camera selected, the first is the tab stop', [0, 1].map(i => F.rovingTabIndex(-1, i)).join(',') === '0,-1');

// Events list: counts, export label, body state, flat newest-first order, roving keys, timeline keys
check('counts: 1 event, 5 events; chip shows filtered as "2 of 5"', F.countText(1) === '1 event' && F.countText(5) === '5 events' && F.countChip(5, 2, true) === '2 of 5' && F.countChip(5, 5, false) === '5');
check('export label names the time', F.exportLabel(at(14, 41, 8)) === 'Export event at 2:41 pm');
// Events panel state machine: every way a load or refresh can end maps to exactly one state
const P = (loading, refreshing, error, total, shown) => F.eventsPanelState({loading, refreshing, error, total, shown});
check('loading: skeletons while a load runs, with or without rows (a first load has none)', P(true, false, '', 0, 0) === 'loading' && P(true, false, '', 3, 3) === 'loading' && P(true, false, 'x', 0, 0) === 'loading');
check('success with 0 events: empty (not loading, no skeleton)', P(false, false, '', 0, 0) === 'empty');
check('success with N events, all shown: ready', P(false, false, '', 5, 5) === 'ready');
check('success with N events, filter matches none: filtered-empty', P(false, false, '', 5, 0) === 'filtered-empty');
check('error (request failed or timed out): error, with or without rows from before', P(false, false, 'boom', 0, 0) === 'error' && P(false, false, 'boom', 4, 4) === 'error');
check('refresh keeps the rows: a refresh is not loading, the list stays ready', P(true, true, '', 5, 5) === 'ready' && P(true, true, '', 0, 0) === 'empty');
check('camera switch or day change starts with loading (skeletons), then one final state', P(true, false, '', 2, 1) === 'loading' && P(false, false, '', 2, 1) === 'ready');
check('error wins over empty and filtered (the alert is the state)', P(false, false, 'x', 0, 0) !== 'empty' && P(false, false, 'x', 3, 0) === 'error');
check('filter row: only with two or more event types (zero or one: no row)', F.filtersVisible(0) === false && F.filtersVisible(1) === false && F.filtersVisible(2) === true && F.filtersVisible(4) === true);
const ev = (h, m, s = 0) => ({start: at(h, m, s), end: at(h, m, s) + 5, type: 2});
const input = [ev(9, 3), ev(14, 41, 8), ev(14, 2), ev(14, 59), ev(9, 40)];
const flat = F.newestFirst(input);
check('flat list: newest start first, every row kept, input not reordered', flat.length === 5 && flat.map(e => e.start).join() === [at(14, 59), at(14, 41, 8), at(14, 2), at(9, 40), at(9, 3)].join() && input[0].start === at(9, 3));
check('flat list: empty list gives an empty array', F.newestFirst([]).length === 0);
check('roving: Down/Up stop at the ends (no wrap), Home/End jump, other keys null', F.rovingEventIndex('ArrowDown', 2, 3) === 2 && F.rovingEventIndex('ArrowDown', 1, 3) === 2 && F.rovingEventIndex('ArrowUp', 0, 3) === 0 && F.rovingEventIndex('End', 0, 3) === 2 && F.rovingEventIndex('Home', 2, 3) === 0 && F.rovingEventIndex('Enter', 1, 3) === null);
check('roving: focus outside the list goes to the first row; empty list is null', F.rovingEventIndex('ArrowDown', -1, 3) === 0 && F.rovingEventIndex('ArrowDown', 0, 0) === null);
check('timeline keys: Left/Right 10 s, Shift 60 s, other keys null', F.timelineKeyStep('ArrowLeft', false) === -10 && F.timelineKeyStep('ArrowRight', false) === 10 && F.timelineKeyStep('ArrowRight', true) === 60 && F.timelineKeyStep('ArrowLeft', true) === -60 && F.timelineKeyStep('ArrowUp', false) === null);

// Static rules for the page: no style attributes (CSP), no inline handlers in the markup or the templates
const html = fs.readFileSync(__dirname + '/../kinora/web/index.html', 'utf8');
check('no style= attribute in app.js or index.html markup', !/\sstyle\s*=/.test(src) && !/\sstyle\s*=/.test(html.replace(/<style>[\s\S]*?<\/style>/g, '')));
check('no inline event handlers (on*=) in index.html or app.js templates', !/\son[a-z]+\s*=\s*["']/.test(html) && !/<[^>]*\son[a-z]+=/.test(src));

// Player status surfaces: no visible mode caption, no hour subheads, tooltips instead of native titles on the mode and speed controls
check('no mode caption element and no hour subheads left in the page', !/id="modecap"/.test(html) && !/ev-hr|hourGroups|hourLabel/.test(src + html));
const tagOf = id => (html.match(new RegExp('<[^>]*\\sid="' + id + '"[^>]*>')) || [''])[0];
check('Live, Recordings and speed carry data-tip (no native title, so no double tooltip)', ['live', 'recbtn'].every(id => /data-tip="[^"]+"/.test(tagOf(id)) && !/\stitle=/.test(tagOf(id))) && /\stitle=/.test(tagOf('speed')) === false);
check('status line element is present and hidden by default', /id="livestat"[^>]*\shidden/.test(html) && /id="camstat"/.test(html));
check('speaker toggle: labelled, pressed state, tooltip, hidden outside Live', /<button[^>]*id="sound"[^>]*aria-label="Sound"[^>]*aria-pressed="[^"]+"[^>]*data-tip="[^"]+"[^>]*\shidden>/.test(html));

check('events request has a 20 s client timeout that moves loading to the error state', /function armEventsGuard\(\)/.test(src) && /,20000\);/.test(src) && /EV_TIMEOUT/.test(src));

// Event thumbnails: retry rules (202 busy = backoff 5, 10, 20, 40 s, 5 attempts; other failures retried once; 404 never)
const sh = (o) => JSON.stringify(o);
check('thumb 200 shows the picture', sh(F.thumbOutcome(200, {tries: 1, hard: 0})) === sh({do: 'show'}));
check('thumb 202 backs off 5, 10, 20, 40 s', [1, 2, 3, 4].map(t => F.thumbOutcome(202, {tries: t, hard: 0}).ms).join() === '5000,10000,20000,40000');
check('thumb 202 gives up after 5 attempts (busy, no retry)', sh(F.thumbOutcome(202, {tries: 5, hard: 0})) === sh({do: 'none', why: 'busy'}));
check('thumb 202 honours a longer Retry-After', F.thumbOutcome(202, {tries: 1, hard: 0}, 12).ms === 12000 && F.thumbOutcome(202, {tries: 1, hard: 0}, 5).ms === 5000);
check('thumb 409 (still recording) waits at least 10 s, follows Retry-After, is not a hard failure', F.thumbOutcome(409, {tries: 1, hard: 0}).ms === 10000 && F.thumbOutcome(409, {tries: 1, hard: 0}, 45).ms === 45000 && F.thumbOutcome(409, {tries: 11, hard: 0}).do === 'wait');
check('thumb 409 gives up after 12 attempts', sh(F.thumbOutcome(409, {tries: 12, hard: 0})) === sh({do: 'none', why: 'failed'}));
check('gapsOf: the complement of the clips inside the range, ignoring tiny gaps', (() => {
  const g = F.gapsOf([{start: 100, end: 200}, {start: 205, end: 300}, {start: 500, end: 600}], 0, 700, 10);
  return JSON.stringify(g) === JSON.stringify([{start: 0, end: 100}, {start: 300, end: 500}, {start: 600, end: 700}]);
})());
check('gapsOf: overlapping clips, clips past the range, and no clips', (() => {
  const a = F.gapsOf([{start: 0, end: 500}, {start: 100, end: 200}], 0, 400, 10);
  const b = F.gapsOf([], 0, 100, 10);
  return a.length === 0 && b.length === 1 && b[0].start === 0 && b[0].end === 100;
})());
check('mergeEvents joins same-type events within the gap and keeps other types apart', (() => {
  const m = F.mergeEvents([{type: 6, start: 0, end: 10}, {type: 6, start: 14, end: 20}, {type: 7, start: 12, end: 13}, {type: 6, start: 100, end: 105}], 5);
  return m.length === 3 && m[0].type === 6 && m[0].start === 0 && m[0].end === 20 && m[1].type === 7 && m[2].start === 100;
})());
check('mergeEvents does not change its input and handles an empty list', (() => { const i = [{type: 6, start: 0, end: 1}, {type: 6, start: 2, end: 3}]; F.mergeEvents(i, 5); return i[0].end === 1 && F.mergeEvents([], 5).length === 0; })());
check('thumb 404 is final (thumbnails off), no retry', sh(F.thumbOutcome(404, {tries: 1, hard: 0})) === sh({do: 'none', why: 'off'}));
check('thumb hard failure: retried once, then given up', sh(F.thumbOutcome(502, {tries: 1, hard: 1})) === sh({do: 'wait', ms: 5000}) && sh(F.thumbOutcome(502, {tries: 2, hard: 2})) === sh({do: 'none', why: 'failed'}));
check('thumb network error (status 0) counts as a hard failure', F.thumbOutcome(0, {tries: 1, hard: 2}).why === 'failed');
check('backoff constants', sh(F.THUMB_BACKOFF_MS) === '[5000,10000,20000,40000]' && F.THUMB_MAX_TRIES === 5);

check('remembered thumbnails are painted when the row is rebuilt (cache hit in thumbAttach)', /it\.state==='show'&&it\.url\) thumbPaint\(it\)/.test(src));

// Queue: the first due item in list order; wake-up time; never run while Live is open
const q = [{ts: 1, due: 500}, {ts: 2, due: 0}, {ts: 3, due: 0}];
check('pick: first due item in list order, -1 when none is due', F.thumbPick(q, 100) === 1 && F.thumbPick([{due: 900}], 100) === -1 && F.thumbPick([], 100) === -1);
check('wake: 0 when something is due, ms to the earliest retry otherwise, null when empty', F.thumbWakeMs(q, 100) === 0 && F.thumbWakeMs([{due: 900}, {due: 400}], 100) === 300 && F.thumbWakeMs([], 100) === null);
check('queue runs beside Live and stops only when nothing is queued', F.thumbMayRun(true, 5) === true && F.thumbMayRun(false, 5) === true && F.thumbMayRun(false, 0) === false);

// Placeholders: same box in every state; the picture only when shown; no state shows text
check('slot classes: show, off, failed, everything else is the quiet wait placeholder', F.thumbSlotClass('show') === 'th-ok' && F.thumbSlotClass('off') === 'th-off' && F.thumbSlotClass('failed') === 'th-fail' && ['idle', 'queued', 'gaveup'].every(s => F.thumbSlotClass(s) === 'th-wait') && F.thumbSlotClass('flight') === 'th-load');

// Thumbnail loader: visible rows only, no inline styles, no lazy <img> per row, one request path (fetch to a blob URL)
const thumbBlock = src.slice(src.indexOf('function thumbReset'), src.indexOf('function evRow'));
check('loader: no broken-image <img> template in rows, fetch to a blob URL, Retry-After read', !/<img loading="lazy"/.test(src) && /URL\.createObjectURL/.test(thumbBlock) && /Retry-After/.test(thumbBlock));
check('loader: fetches only visible rows (IntersectionObserver) and one at a time (inflight guard)', /new IntersectionObserver/.test(thumbBlock) && /TH\.inflight/.test(thumbBlock));
check('loader: resets on camera or day change (load) and pauses via liveOn', /thumbReset\(\); const seq=\+\+loadSeq/.test(src) && /thumbMayRun\(liveOn/.test(thumbBlock));
// Player layout (the design notes): static checks; the measured checks are done in a browser
const at2 = s => html.indexOf(s);
check('camstat sits after the transport bar and after the video (so showing it never moves the video)', at2('id="camstat"') > at2('id="vwrap"') && at2('id="camstat"') > at2('class="bar"') && at2('id="camstat"') < at2('id="livestat"'));
check('no leftover 346px height math or 290px chrome in index.html', !/346px|290px/.test(html));
check('#app has no max-width cap (app bar and content span the page)', /#app\{max-width:none/.test(html) && !/#app\{max-width:640px/.test(html));
const rootBlock = html.match(/:root\{--gut:[^}]*\}/);
check('root spacing variables exist with the spec values (base)', !!rootBlock && /--gut:16px/.test(rootBlock[0]) && /--pt:var\(--s-3\)/.test(rootBlock[0]) && /--bar-h:52px/.test(rootBlock[0]) && /--max:1760px/.test(rootBlock[0]) && /--side:clamp\(320px,26vw,440px\)/.test(rootBlock[0]) && /--chrome:328px/.test(rootBlock[0]));
check('root spacing variables: 600px, 960px and 960-1279px overrides', /@media\(min-width:600px\)\{:root\{--gut:var\(--s-6\);--pt:var\(--s-4\);--bar-h:56px\}\}/.test(html) && /@media\(min-width:960px\)\{:root\{--gut:var\(--s-7\);--pt:var\(--s-6\)\}\}/.test(html) && /@media\(min-width:960px\) and \(max-width:1279px\)\{:root\{--chrome:376px\}\}/.test(html));
const ptzRule = (html.match(/#vwrap #ptz\{[^}]*\}/) || [''])[0];
check('#vwrap #ptz sits in the top-right corner of the video (top and right = --s-4)', /top:var\(--s-4\)/.test(ptzRule) && /right:var\(--s-4\)/.test(ptzRule) && !/translateY/.test(ptzRule));
check('no style= attribute in the new layout markup', !/\sstyle\s*=/.test(html.replace(/<style>[\s\S]*?<\/style>/g, '')));

console.log(ok ? 'ALL PASS' : 'SOME FAILED');
process.exit(ok ? 0 : 1);
