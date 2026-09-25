/**
 * Headless smoke test for web/index.html.
 *
 * Runs the page's real script against a stub DOM and stub Leaflet, using the
 * real exported JSON, then asserts what each tab renders. Catches the class of
 * bug where a payload change makes a render function throw and the panel
 * silently keeps showing the previous tab.
 *
 *   node tests/demo_smoke.js [project-root]
 */
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const ROOT = process.argv[2] || path.join(__dirname, '..');

function stubElement() {
  const classes = new Set();
  const handlers = {};
  return {
    innerHTML: '', textContent: '', hidden: false, dataset: {}, style: {},
    classList: {
      add: c => classes.add(c), remove: c => classes.delete(c),
      toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)),
      contains: c => classes.has(c),
    },
    addEventListener(type, fn) { (handlers[type] = handlers[type] || []).push(fn); },
    fire(type, evt) { (handlers[type] || []).forEach(fn => fn(evt)); },
    contains() { return false; }, closest() { return null; },
  };
}

function runPage(webDir) {
  const html = fs.readFileSync(path.join(webDir, 'index.html'), 'utf8');
  let js = html.match(/<script>([\s\S]*?)<\/script>/)[1];
  js += '\nglobalThis.__api = { state, render, applyView, markers, inWindow, focusPlace };\n';

  const nodes = new Map();
  const get = sel => {
    if (!nodes.has(sel)) nodes.set(sel, stubElement());
    return nodes.get(sel);
  };
  const navButtons = ['current', 'range', 'all'].map(view => {
    const b = stubElement(); b.dataset.view = view; return b;
  });

  const mapObj = {
    _layers: new Set(), _zoom: 11, lastFly: null,
    setView() { return this; }, fitBounds() {}, on() {},
    getZoom() { return mapObj._zoom; },
    flyTo(latlng, zoom) { mapObj.lastFly = { latlng, zoom }; },
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
        on() { return this; }, setStyle(s) { this.opts = Object.assign({}, this.opts, s); return this; },
        openTooltip() { this.tooltipOpen = true; }, closeTooltip() { this.tooltipOpen = false; },
        addTo(m) { if (m && m.addLayer) m.addLayer(this); return this; },
        bringToFront() {},
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
      activeElement: null,
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
  return { get, map: mapObj, api: () => ctx.__api };
}

function withPanel(webDir, view) {
  const { get, map, api } = runPage(webDir);
  return new Promise(resolve => setTimeout(() => {
    const a = api();
    a.state.view = view;
    a.render();
    a.applyView();
    resolve({ get, map, a, html: get('#list').innerHTML });
  }, 80));
}

const DAY = 86400000;
const toUTC = iso => { const [y, m, d] = iso.split('-').map(Number); return Date.UTC(y, m - 1, d); };
const isoAt = (base, i) => new Date(toUTC(base) + i * DAY).toISOString().slice(0, 10);

const results = [];
function check(name, fn) {
  try { fn(); results.push(['ok', name]); }
  catch (err) { results.push(['FAIL', name, err.message]); }
}
function assert(cond, msg) { if (!cond) throw new Error(msg); }
const countRows = html => (html.match(/class="row/g) || []).length;

(async () => {
  const reads = n => JSON.parse(fs.readFileSync(path.join(ROOT, 'web/data', n), 'utf8'));
  const places = reads('places.json');
  const current = reads('current.json');
  const updates = reads('updates.json');
  const meta = reads('meta.json');

  // Independent re-implementation of the window filter, for cross-checking.
  const expectedInWindow = (from, to) => places.filter(p =>
    p.periods.some(x => x.from_date <= to && x.to_date >= from));

  const cur = await withPanel(path.join(ROOT, 'web'), 'current');
  check('current tab renders one row per current entry', () => {
    const rows = countRows(cur.html);
    assert(rows === current.count, `expected ${current.count} rows, got ${rows}`);
  });

  const all = await withPanel(path.join(ROOT, 'web'), 'all');
  check('all tab renders every place', () => {
    const rows = countRows(all.html);
    assert(rows === places.length, `expected ${places.length} rows, got ${rows}`);
  });

  const rng = await withPanel(path.join(ROOT, 'web'), 'range');
  check('range slider spans the whole archive', () => {
    const span = Math.round((toUTC(meta.coverage.last_observation) -
                             toUTC(meta.coverage.first_observation)) / DAY);
    [['#rangeA', 'start'], ['#rangeB', 'end']].forEach(([sel, which]) => {
      assert(Number(rng.get(sel).max) === span,
             `${which} thumb max should be ${span} days, got ${rng.get(sel).max}`);
      assert(Number(rng.get(sel).min) === 0, `${which} thumb min should be 0`);
    });
  });
  check('range tab defaults to the previous year', () => {
    const to = meta.coverage.last_observation;
    const from = isoAt(meta.coverage.first_observation,
      Math.max(0, Math.round((toUTC(to) - toUTC(meta.coverage.first_observation)) / DAY) - 365));
    assert(rng.a.state.to === to, `expected to=${to}, got ${rng.a.state.to}`);
    assert(rng.a.state.from === from, `expected from=${from}, got ${rng.a.state.from}`);
    assert(rng.get('#rangeFrom').textContent === from, 'From label not updated');
    assert(rng.get('#rangeTo').textContent === to, 'To label not updated');
  });
  check('range tab lists exactly the places on the list in that window', () => {
    const want = expectedInWindow(rng.a.state.from, rng.a.state.to);
    const rows = countRows(rng.html);
    assert(rows === want.length, `expected ${want.length} rows, got ${rows}`);
    assert(rows > 0 && rows < places.length, `window should be a strict subset (${rows})`);
  });

  const windows = [
    ['2017-08-05', '2017-08-05'],
    ['2019-01-01', '2019-12-31'],
    ['2022-06-01', '2022-08-31'],
    ['2025-01-01', '2026-09-21'],
  ];
  check('range filter agrees with an independent computation for 4 windows', () => {
    for (const [from, to] of windows) {
      rng.a.state.from = from; rng.a.state.to = to;
      rng.a.render();
      const got = countRows(rng.get('#list').innerHTML);
      const want = expectedInWindow(from, to).length;
      assert(got === want, `${from}..${to}: expected ${want} rows, got ${got}`);
    }
  });
  check('a place that left the list in 2018 is out of the default window', () => {
    rng.a.state.from = isoAt(meta.coverage.first_observation,
      Math.round((toUTC(meta.coverage.last_observation) - toUTC(meta.coverage.first_observation)) / DAY) - 365);
    rng.a.state.to = meta.coverage.last_observation;
    const gone = places.find(p => p.periods.every(x => x.to_date < '2019-01-01'));
    assert(gone, 'expected at least one long-gone place in the data');
    assert(!rng.a.inWindow(gone), `${gone.name} should not be in the last-year window`);
    rng.a.state.from = meta.coverage.first_observation;
    assert(rng.a.inWindow(gone), `${gone.name} should be in the full-archive window`);
  });

  check('clicking a list row centres the map on it and zooms in', () => {
    const mapped = places.filter(p => p.lat !== null && p.lng !== null);
    const target = mapped.find(p => all.a.markers.has(p.id));
    all.map.lastFly = null;
    const row = { dataset: { place: target.id }, closest: sel => (sel === '.row' ? row : null) };
    all.get('#list').fire('click', { target: row });
    assert(all.map.lastFly, 'map was not moved by the click');
    assert(Math.abs(all.map.lastFly.latlng[0] - target.lat) < 1e-9 &&
           Math.abs(all.map.lastFly.latlng[1] - target.lng) < 1e-9,
           `flew to ${all.map.lastFly.latlng}, expected [${target.lat}, ${target.lng}]`);
    assert(all.map.lastFly.zoom >= 16, `expected street-level zoom, got ${all.map.lastFly.zoom}`);
  });

  check('clicking the website link inside a row does not move the map', () => {
    const mapped = places.filter(p => p.lat !== null && p.lng !== null);
    const target = mapped.find(p => all.a.markers.has(p.id));
    all.map.lastFly = null;
    const row = { dataset: { place: target.id }, closest: sel => (sel === '.row' ? row : null) };
    const link = { closest: sel => (sel === 'a' ? link : (sel === '.row' ? row : null)) };
    all.get('#list').fire('click', { target: link });
    assert(all.map.lastFly === null, 'clicking the link should not re-centre the map');
  });

  check('clicking a row for a place with no coordinates is a no-op', () => {
    const unmapped = places.find(p => p.lat === null || p.lng === null);
    if (!unmapped) return;                       // every place is mapped
    all.map.lastFly = null;
    assert(all.a.focusPlace(unmapped.id) === false, 'focusPlace should report failure');
    assert(all.map.lastFly === null, 'map should not move for an unmapped place');
  });

  check('on-list and rotated-out pins are visually distinct', () => {
    const mapped = places.filter(p => p.lat !== null && p.lng !== null && all.a.markers.has(p.id));
    const here = mapped.find(p => p.on_list_now);
    const gone = mapped.find(p => !p.on_list_now);
    assert(here && gone, 'need both a present and a rotated-out mapped place');
    all.a.state.view = 'all';
    all.a.render();
    all.a.applyView();
    const fill = p => all.a.markers.get(p.id).opts.fillColor;
    assert(fill(here) && fill(gone), 'pins have no fill colour after painting');
    assert(fill(here) !== fill(gone),
           `states look identical (${fill(here)} vs ${fill(gone)})`);
    assert(all.a.markers.get(here.id).opts.fillOpacity === 1
        && all.a.markers.get(here.id).opts.weight >= 2,
           'pins should be solid with a visible casing');
  });

  check('markers exist for mapped places', () => {
    const mapped = places.filter(p => p.lat !== null && p.lng !== null).length;
    assert(all.a.markers.size === mapped, `expected ${mapped} markers, got ${all.a.markers.size}`);
  });
  check('tooltip shows no click hint', () => {
    const html = String(all.a.markers.values().next().value.tooltip || '');
    assert(!/click to open website|click for the Eater entry/.test(html),
           'tooltip still contains a click hint');
  });
  check('every update id resolves to a known place', () => {
    const ids = new Set(places.map(p => p.id));
    const bad = [];
    updates.forEach(u => [...(u.added_ids || []), ...(u.removed_ids || [])]
      .forEach(id => { if (!ids.has(id)) bad.push(id); }));
    assert(bad.length === 0, `unresolved ids: ${bad.slice(0, 5).join(', ')}`);
  });

  results.forEach(([status, name, msg]) => {
    console.log(`${status === 'ok' ? '  ok  ' : '  FAIL'}  ${name}${msg ? '  -> ' + msg : ''}`);
  });
  const failed = results.filter(r => r[0] === 'FAIL').length;
  console.log(`\n${results.length - failed}/${results.length} demo checks passed`);
  process.exit(failed ? 1 : 0);
})();
