// Offline test of the page's prefetch policy (static/app.js between the prefetch-policy markers) + a queue simulation. Run: node tests/test_prefetch.js
const fs = require('fs'), vm = require('vm');
const src = fs.readFileSync(__dirname + '/../kinora/web/static/app.js', 'utf8');
const block = src.split('/* prefetch-policy:begin */')[1].split('/* prefetch-policy:end */')[0];
const ffBlock = src.split('/* ff-policy:begin */')[1].split('/* ff-policy:end */')[0];
const ctx = {}; vm.createContext(ctx); vm.runInContext(ffBlock + block + ';this.pfDepth=pfDepth;this.pfLen=pfLen;this.pfLearn=pfLearn;this.ffScaleFor=ffScaleFor;this.ffPlayRate=ffPlayRate;this.ffViewT=ffViewT;this.ffSeekTo=ffSeekTo;this.ffFactor=ffFactor;', ctx);
const { pfDepth, pfLen, pfLearn, ffScaleFor, ffPlayRate, ffViewT, ffSeekTo, ffFactor } = ctx;
const FF_FACTOR = 16;
let ok = true; const check = (n, c) => { console.log((c ? 'PASS ' : 'FAIL ') + n); ok = ok && !!c; };

// --- unit tests
check('depth 1 at 1x', pfDepth(1, 0.07) === 1);
check('depth grows with speed', pfDepth(2, 0.07) === 2 && pfDepth(4, 0.07) === 2 && pfDepth(8, 0.07) === 2 && pfDepth(16, 0.07) === 3);
check('a slow camera gets more pieces ahead (capped at 3)', pfDepth(8, 0.2) === 3 && pfDepth(16, 1) === 3);
check('default speed used before anything is learned', pfDepth(16, undefined) === 3);
check('ramp 40, 60, 90, then the base, at >=4x only', [1, 2, 3, 4, 5].map(k => pfLen(16, k, 120)).join() === '40,60,90,120,120' && [1, 2, 3].map(k => pfLen(2, k, 120)).join() === '120,120,120' && pfLen(4, 1, 120) === 40);
check('cache hits (<0.6 s) are not learned', pfLearn(0.07, 0.2, 60) === 0.07);
check('first real fetch is learned', Math.abs(pfLearn(undefined, 6, 60) - 0.1) < 1e-9);
check('learning smooths', Math.abs(pfLearn(0.1, 6, 30) - (0.6 * 0.1 + 0.4 * 0.2)) < 1e-9);

// --- key-frame fast-forward mapping
check('direct cameras stream only 8x and 16x, P2P cameras every speed', ffScaleFor(false, 8) === '8/1' && ffScaleFor(false, 16) === '16/1' && ffScaleFor(false, 4) === null && ffScaleFor(false, 1) === null && [1, 2, 4, 8, 16].map(r => ffScaleFor(true, r)).join() === '1/1,2/1,4/1,8/1,16/1');
check('the factor is the scale numerator; streams play at normal rate', ffFactor('8/1') === 8 && ffFactor('16/1') === 16 && ffFactor('2/1') === 2 && ffFactor('1/1') === 1 && ffPlayRate() === 1);
check('stream time -> recording time scales by 16', ffViewT(1000, 0, 10, FF_FACTOR) === 1160 && ffViewT(1000, 2, 12, FF_FACTOR) === 1160 && ffViewT(1000, 5, 3, FF_FACTOR) === 1000);
check('recording time -> stream time is the inverse', Math.abs(ffSeekTo(1000, 2, 1160, FF_FACTOR) - 12) < 1e-9 && ffSeekTo(1000, 0, 1000, 1) === 0 && ffViewT(1000, 0, ffSeekTo(1000, 3, 1320, FF_FACTOR) - 3, FF_FACTOR) === 1320);
check('normal streams (factor 1) map 1:1', ffViewT(500, 0, 42, 1) === 542 && ffSeekTo(500, 0, 542, 1) === 42);

// --- simulation: one camera, downloads are serialised, fetch time = 0.5 s + len * spv
const SPV = 1 / 15, OVER = 0.5, TOTAL = 1200, QUICK = 20;
function sim(rate, policy) {
  const oldBase = rate >= 8 ? 300 : rate >= 4 ? 180 : 120;
  const lens = []; let covered = 0, k = 0;
  while (covered < TOTAL) { const len = k === 0 ? QUICK : (policy === 'new' ? pfLen(rate, k, 120) : oldBase); lens.push(len); covered += len; k++; }
  const dur = len => OVER + len * SPV, ready = [], queuedAt = []; let serverFree = 0;
  const enqueue = (i, t) => { if (queuedAt[i] !== undefined) return; queuedAt[i] = t; const s = Math.max(t, serverFree); ready[i] = s + dur(lens[i]); serverFree = ready[i]; };
  const depthOld = rate >= 8 ? 2 : 1, depthNew = pfDepth(rate, SPV);
  enqueue(0, 0); let playEnd = 0, stall = ready[0], longest = 0;
  for (let i = 0; i < lens.length; i++) {
    const start = Math.max(playEnd, ready[i]); if (i > 0) { const w = Math.max(0, ready[i] - playEnd); stall += w; longest = Math.max(longest, w); }
    // when does the page queue the next pieces?
    const t0 = ready[i];
    if (policy === 'new') { for (let j = 1; j <= (rate > 1 ? depthNew : 1); j++) if (i + j < lens.length) enqueue(i + j, rate > 1 ? t0 : t0 + (lens[i] / rate) / 2); }
    else { for (let j = 1; j <= depthOld; j++) if (i + j < lens.length) enqueue(i + j, t0 + 0.5 / rate); }
    if (i + 1 < lens.length && queuedAt[i + 1] === undefined) enqueue(i + 1, start + lens[i] / rate);   // 'ended' handler asks for it
    playEnd = start + lens[i] / rate;
  }
  return { stall, longest, wall: playEnd };
}
console.log('\nrate | total wait old -> new (s, 20 min of footage) | longest single freeze old -> new | wall time old -> new');
for (const r of [1, 2, 4, 8, 16]) {
  const a = sim(r, 'old'), b = sim(r, 'new');
  console.log(String(r).padStart(3) + 'x | ' + a.stall.toFixed(1).padStart(6) + ' -> ' + b.stall.toFixed(1).padStart(6) + ' | ' + a.longest.toFixed(1).padStart(6) + ' -> ' + b.longest.toFixed(1).padStart(6) + ' | ' + a.wall.toFixed(0).padStart(5) + ' -> ' + b.wall.toFixed(0).padStart(5));
  // at 16x the camera (~15x) is slower than playback, so the total wait is bandwidth-bound: allow the ramp's extra piece (~1 s), require shorter freezes
  check(`${r}x: total wait not meaningfully worse (+1.5 s tolerance)`, b.stall <= a.stall + 1.5);
  check(`${r}x: no single freeze longer than before`, b.longest <= a.longest + 0.01);
  if (r >= 4) check(`${r}x: longest freeze at most half as long`, b.longest <= a.longest * 0.5 || b.longest < 0.5);
  if (r >= 4) check(`${r}x: total wait clearly lower`, b.stall < a.stall * 0.95);
}
console.log(ok ? 'ALL PASS' : 'SOME FAILED'); process.exit(ok ? 0 : 1);
