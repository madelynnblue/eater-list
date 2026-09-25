/**
 * Headless smoke test for web/index.html.
 *
 * Runs the page's actual script against a stub DOM and stub Leaflet, using the
 * real exported JSON, then asserts each tab renders. Catches the class of bug
 * where a payload shape change makes a render function throw and the panel
 * silently keeps showing the previous tab.
 *
 *   node tests/demo_smoke.js [project-root]
 */
const fs = require('fs');
const vm = require('vm');
const path = require('path');
const os = require('os');

const ROOT = process.argv[2] || path.join(__dirname, '..');

function stubElement() {
  const classes = new Set();
  return {
    innerHTML: '', textContent: '', hidden: false, dataset: {},
    classList: {
      add: c => classes.add(c), remove: c => classes.delete(c),
      toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)),
      contains: c => classes.has(c),
    },
    addEventListener() {}, contains() { return false; }, closest() { return null; },
  };
}

function runPage(webDir) {
  const html = fs.readFileSync(path.join(webDir, 'index.html'), 'utf8');
  let js = html.match(/<script>([\s\S]*?)<\/script>/)[1];
  js += '\nglobalThis.__api = { state, render, applyView, markers, showUpdate };\n';

  const nodes = new Map();
  const get = sel => {
    if (!nodes.has(sel)) nodes.set(sel, stubElement());
    return nodes.get(sel);
  };
  const navButtons = ['current', 'updates', 'all'].map(view => {
    const b = stubElement(); b.dataset.view = view; return b;
  });

  const mapObj = {
    _layers: new Set(),
    setView() { return this; }, fitBounds() {}, on() {},
    hasLayer: l => mapObj._layers.has(l),
    addLayer: l => mapObj._layers.add(l),
    removeLayer: l => mapObj._layers.delete(l),
  };

  const L = {
    map: () => mapObj,
    tileLayer: () => ({ addTo() { return this; } }),
    control: () => { const c = { onAdd: null, _div: null, addTo() { c._div = c.onAdd(); return c; } }; return c; },
    DomUtil: { create: () => stubElement() },
    circleMarker(latlng, opts) {
      return {
        latlng, opts, tooltip: null,
        bindTooltip(h) { this.tooltip = h; return this; },
        on() { return this; }, setStyle(s) { this.opts = s; return this; },
        openTooltip() {}, closeTooltip() {}, addTo() { return this; }, bringToFront() {},
      };
    },
    geoJSON(data, opts) {
      return {
        addData(geo) {
          geo.features.forEach(f => opts.onEachFeature(f, opts.pointToLayer(f, { lat: 0, lng: 0 })));
        },
        addTo() { return this; },
      };
    },
    featureGroup: () => ({ getBounds: () => ({}) }),
  };

  const sandbox = {
    document: {
      querySelector: get,
      querySelectorAll: sel => (sel === 'nav button' ? navButtons : []),
    },
    L, console,
    window: { open() {} },
    setTimeout, Promise, Map, Set, Number, String, Array, JSON, Object, Date,
    fetch: url => Promise.resolve({
      json: () => Promise.resolve(JSON.parse(fs.readFileSync(path.join(webDir, url), 'utf8'))),
    }),
  };
  sandbox.globalThis = sandbox;

  const ctx = vm.createContext(sandbox);
  vm.runInContext(js, ctx);
  return { get, api: () => ctx.__api, navButtons };
}

function withPanel(webDir, view) {
  const { get, api } = runPage(webDir);
  return new Promise(resolve => setTimeout(() => {
    const a = api();
    a.state.view = view;
    a.render();
    resolve({ get, a, html: get('#list').innerHTML });
  }, 80));
}

const results = [];
function check(name, fn) {
  try { fn(); results.push(['ok', name]); }
  catch (err) { results.push(['FAIL', name, err.message]); }
}
function assert(cond, msg) { if (!cond) throw new Error(msg); }

(async () => {
  const reads = name => JSON.parse(fs.readFileSync(path.join(ROOT, 'web/data', name), 'utf8'));
  const places = reads('places.json');
  const current = reads('current.json');
  const updates = reads('updates.json');

  const cur = await withPanel(path.join(ROOT, 'web'), 'current');
  check('current tab renders one row per current entry', () => {
    const rows = (cur.html.match(/class="row/g) || []).length;
    assert(rows === current.count, `expected ${current.count} rows, got ${rows}`);
    assert(rows > 0, 'no rows rendered');
  });

  const all = await withPanel(path.join(ROOT, 'web'), 'all');
  check('all tab renders every place', () => {
    const rows = (all.html.match(/class="row/g) || []).length;
    assert(rows === places.length, `expected ${places.length} rows, got ${rows}`);
  });

  const upd = await withPanel(path.join(ROOT, 'web'), 'updates');
  check('updates tab renders a dated block per update', () => {
    const blocks = (upd.html.match(/class="upd"/g) || []).length;
    const dates = (upd.html.match(/class="when"/g) || []).length;
    assert(blocks === updates.length, `expected ${updates.length} blocks, got ${blocks}`);
    assert(dates === updates.length, `expected ${updates.length} date headers, got ${dates}`);
  });
  check('updates tab shows the diff, not just dates', () => {
    const adds = (upd.html.match(/class="add"/g) || []).length;
    const dels = (upd.html.match(/class="del"/g) || []).length;
    assert(adds > 20 && dels > 20, `diff lines look wrong: ${adds} adds, ${dels} dels`);
  });

  check('update ids all resolve to known places', () => {
    const ids = new Set(places.map(p => p.id));
    const bad = [];
    updates.forEach(u => [...(u.added_ids || []), ...(u.removed_ids || [])]
      .forEach(id => { if (!ids.has(id)) bad.push(id); }));
    assert(bad.length === 0, `unresolved ids: ${bad.slice(0, 5).join(', ')}`);
  });

  check('markers exist for mapped places', () => {
    const mapped = places.filter(p => p.lat !== null && p.lng !== null).length;
    assert(all.a.markers.size === mapped, `expected ${mapped} markers, got ${all.a.markers.size}`);
  });

  check('tooltip shows no click hint', () => {
    const html = String(upd.a.markers.values().next().value.tooltip || '');
    assert(!/click to open website|click for the Eater entry/.test(html),
           'tooltip still contains a click hint');
  });

  // Resilience: a payload from an older pipeline (no *_ids) must not break the tab.
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'e38-stale-'));
  fs.mkdirSync(path.join(tmp, 'data'), { recursive: true });
  ['places.json', 'current.json', 'meta.json', 'places.geojson'].forEach(f =>
    fs.copyFileSync(path.join(ROOT, 'web/data', f), path.join(tmp, 'data', f)));
  fs.writeFileSync(path.join(tmp, 'data', 'updates.json'),
    JSON.stringify(updates.map(u => ({ ...u, added_ids: undefined, removed_ids: undefined }))));
  fs.copyFileSync(path.join(ROOT, 'web/index.html'), path.join(tmp, 'index.html'));

  const stale = await withPanel(tmp, 'updates');
  check('updates tab survives a legacy payload without ids', () => {
    const blocks = (stale.html.match(/class="upd"/g) || []).length;
    assert(blocks === updates.length, `expected ${updates.length} blocks, got ${blocks}`);
  });

  results.forEach(([status, name, msg]) => {
    console.log(`${status === 'ok' ? '  ok  ' : '  FAIL'}  ${name}${msg ? '  -> ' + msg : ''}`);
  });
  const failed = results.filter(r => r[0] === 'FAIL').length;
  console.log(`\n${results.length - failed}/${results.length} demo checks passed`);
  process.exit(failed ? 1 : 0);
})();
