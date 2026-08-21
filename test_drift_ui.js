// jsdom render check for the market-drift UI. Verifies the panels actually
// produce DOM (jsdom validates syntax and structure, NOT computed layout --
// a file can pass this and still be visibly broken in a browser).
const fs = require('fs');
const { JSDOM } = require('jsdom');

const html = fs.readFileSync('index.html', 'utf8');
const dom = new JSDOM(html, { runScripts: 'dangerously', pretendToBeVisual: true });
const win = dom.window;
win.fetch = () => Promise.reject(new Error('no network in test'));

setTimeout(() => {
  const POS = { QB: 20, RB: 40, WR: 45, TE: 18 };
  const players = [];
  let rank = 1;
  for (const [pos, n] of Object.entries(POS)) {
    for (let i = 1; i <= n; i++) {
      const tru = Math.round((Math.sin(i / 4) * 14 + (pos === 'QB' ? 9 : pos === 'RB' ? -8 : 0)) * 10) / 10;
      const price = Math.round((tru * 0.7) * 10) / 10;
      const talent = Math.round(((tru - price) / 0.5) * 10) / 10;
      const band = tru > 9 ? 'strong-pos' : tru > 5 ? 'pos' : tru < -9 ? 'strong-neg' : tru < -5 ? 'neg' : 'neutral';
      players.push({
        id: `${pos}${i}`, name: `Player ${pos}${i}`, pos, team: 'KC', tier: `${pos}1 - x`,
        avgRank: rank++, seanRankOverall: i * 3, seanPosRank: i, byeWeek: 7,
        projectedWar: 50 - i, warByLeague: { kepners: 50 - i },
        platform: {}, notes: '',
        marketDrift: { kepners: { slot: `${pos}${i}`, price, talent, tru, band, sample: 'ok' } },
      });
    }
  }
  const DATA_OBJ = {
    meta: {
      season: 2026,
      marketDriftConfig: { smoothWindow: 2, talentSmoothWindow: 4, neutralDeadband: 6, talentWeight: 0.5 },
      marketDriftBands: { kepners: { adpSource2026: 'yahoo', adpSource2025: 'average', likeForLike: false, leanRemoved: 3.8, bands: { strongPos: 9, pos: 5, neg: -5, strongNeg: -9 } } },
    },
    players,
    leagues: { kepners: { platform: 'yahoo', teams: Array.from({ length: 12 }, (_, i) => ({ slot: i + 1, team: `T${i}`, manager: `M${i}` })), rosterSlots: ['QB', 'RB', 'RB', 'WR', 'WR', 'WR', 'TE', 'K', 'DEF'], keepers: [], rules: { teams: 12, format: 'snake' } } },
  };
  // top-level `let` bindings aren't properties of window, so assign via eval
  win.eval(`DATA = ${JSON.stringify(DATA_OBJ)}; activeLeague = 'kepners';`);

  let fail = 0;
  const ok = (c, m) => { console.log((c ? 'PASS: ' : 'FAIL: ') + m); if (!c) fail++; };

  // --- panel renders -------------------------------------------------------
  const mount = win.document.createElement('div');
  win.document.body.appendChild(mount);
  win.mountDriftPanel(mount);

  ok(mount.querySelectorAll('svg').length === 2, 'renders both curve and value map SVGs');
  ok(mount.querySelectorAll('.drift-tab').length === 5, 'renders ALL/QB/RB/WR/TE tabs');
  const curvePaths = mount.querySelector('#driftCurveMount').querySelectorAll('path').length;
  ok(curvePaths === 4, `ALL view draws one TruRank line per position (got ${curvePaths})`);
  ok(mount.querySelectorAll('circle').length === players.length, 'value map plots every slot');
  ok(/TruRank = 0/.test(mount.innerHTML), 'value map labels the break-even diagonal');
  ok(/YAHOO/.test(mount.innerHTML) && /lean has been removed/.test(mount.innerHTML),
    'reports the ADP source and the mixed-source lean correction');

  // --- decomposition view --------------------------------------------------
  mount.querySelector('.drift-tab[data-pos="RB"]').dispatchEvent(new win.Event('click'));
  const decomp = mount.querySelector('#driftCurveMount').querySelectorAll('path').length;
  ok(decomp === 4, `single-position view draws price + talent + TruRank + shaded gap (got ${decomp})`);
  ok(/WAR correction/.test(mount.innerHTML), 'explains the shaded gap as the WAR correction');
  ok(mount.querySelector('.drift-tab[data-pos="RB"]').classList.contains('on'), 'active tab is marked');

  // --- table column --------------------------------------------------------
  const tbl = win.document.createElement('div');
  win.document.body.appendChild(tbl);
  win.renderPlayerTable(tbl, {});
  const heads = [...tbl.querySelectorAll('th')].map(t => t.textContent.trim().split(' ')[0]);
  ok(heads.includes('MKT'), `MKT column present (${heads.join('|')})`);
  ok(tbl.querySelectorAll('.pt-mkt').length > 0, 'MKT cells render');
  ok(tbl.querySelectorAll('.mkt-strong-pos, .mkt-pos').length > 0, 'green bands applied');
  ok(tbl.querySelectorAll('.mkt-strong-neg, .mkt-neg').length > 0, 'red bands applied');

  // --- column disappears with no drift data --------------------------------
  win.eval('DATA.players.forEach(p => { p.marketDrift = {}; });');
  const tbl2 = win.document.createElement('div');
  win.document.body.appendChild(tbl2);
  win.renderPlayerTable(tbl2, {});
  ok(![...tbl2.querySelectorAll('th')].some(t => t.textContent.includes('MKT')),
    'MKT column hidden when no 2025 board was available');
  const m2 = win.document.createElement('div');
  win.mountDriftPanel(m2);
  ok(m2.innerHTML.trim() === '', 'drift panel renders nothing without drift data');

  console.log(fail ? `\n${fail} CHECK(S) FAILED` : '\nALL RENDER CHECKS PASSED');
  process.exit(fail ? 1 : 0);
}, 400);
