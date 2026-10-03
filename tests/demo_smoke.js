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
    scrollIntoView() { this._scrolled = true; },
    getBoundingClientRect() {
      return this._rect || { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 };
    },
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
    _layers: new Set(), _zoom: 11, lastFly: null, _handlers: {},
    setView() { return this; }, fitBounds() {},
    on(type, fn) { (mapObj._handlers[type] = mapObj._handlers[type] || []).push(fn); },
    fire(type, evt) { (mapObj._handlers[type] || []).forEach(fn => fn(evt)); },
    getContainer: () => ({ getBoundingClientRect: () =>
      ({ left: 0, right: 1000, top: 0, bottom: 600 }) }),
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
    DomUtil: {
      create: () => stubElement(),
      getPosition: el => el._pos || { x: 0, y: 0, add: ([dx, dy]) => ({ x: (el._pos?.x || 0) + dx, y: (el._pos?.y || 0) + dy }) },
      setPosition: (el, pos) => { el._pos = pos; },
    },
    circleMarker(latlng, opts) {
      const m = {
        latlng, opts, tooltip: null, _handlers: {},
        bindTooltip(h) { this.tooltip = h; return this; },
        on(type, fn) { (m._handlers[type] = m._handlers[type] || []).push(fn); return this; },
        fire(type, evt) { (m._handlers[type] || []).forEach(fn => fn(evt)); return this; },
        setStyle(s) { this.opts = Object.assign({}, this.opts, s); return this; },
        openTooltip() { this.tooltipOpen = true; }, closeTooltip() { this.tooltipOpen = false; },
        addTo(map) { if (map && map.addLayer) map.addLayer(this); return this; },
        bringToFront() {},
      };
      return m;
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

  const win = {
    opened: [], scrolled: [], touch: false, narrow: false, pageYOffset: 0,
    open(url) { win.opened.push(url); },
    scrollTo(opts) { win.scrolled.push(opts); },
    matchMedia: q => ({
      matches: (win.touch && /hover:\s*none/.test(q))
            || (win.narrow && /max-width:\s*760px/.test(q)),
    }),
  };

  const sandbox = {
    document: {
      querySelector: get,
      querySelectorAll: sel => (sel === 'nav button' ? navButtons : []),
      activeElement: null,
    },
    L, console,
    window: win,
    setTimeout, clearTimeout, Promise, Map, Set, Number, String, Array, JSON, Object, Date,
    fetch: url => Promise.resolve({
      json: () => Promise.resolve(JSON.parse(fs.readFileSync(path.join(webDir, url), 'utf8'))),
    }),
  };
  sandbox.globalThis = sandbox;

  const ctx = vm.createContext(sandbox);
  vm.runInContext(js, ctx);
  return { get, map: mapObj, win, api: () => ctx.__api };
}

function withPanel(webDir, view) {
  const { get, map, win, api } = runPage(webDir);
  return new Promise(resolve => setTimeout(() => {
    const a = api();
    a.state.view = view;
    a.render();
    a.applyView();
    resolve({ get, map, win, a, html: get('#list').innerHTML });
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

  check('a tooltip wider than the map edge is nudged back inside', () => {
    const el = { getBoundingClientRect: () => ({ left: -50, right: 470, top: 0, bottom: 200 }) };
    all.map.fire('tooltipopen', { tooltip: { getElement: () => el } });
    assert(el._pos && el._pos.x > 0,
           `expected the tooltip to be pushed right, got ${JSON.stringify(el._pos)}`);
  });

  check('a tooltip that already fits is left alone', () => {
    const el = { getBoundingClientRect: () => ({ left: 300, right: 820, top: 0, bottom: 200 }) };
    all.map.fire('tooltipopen', { tooltip: { getElement: () => el } });
    assert(!el._pos, 'a tooltip that fits should not be repositioned');
  });

  check('hovering a pin reveals and scrolls to its sidebar entry', () => {
    const target = places.find(p => p.blurb && all.a.markers.has(p.id));
    const row = all.get(`.row[data-place="${target.id}"]`);
    const marker = all.a.markers.get(target.id);
    marker.fire('mouseover');
    assert(row.classList.contains('hot'), 'the sidebar row was not highlighted');
    assert(row._scrolled, 'the sidebar was not scrolled to the row');
    marker.fire('mouseout');
    assert(!row.classList.contains('hot'), 'the highlight was not cleared on mouseout');
  });

  check('closed places are excluded from the exported data', () => {
    let closed = {};
    try {
      closed = JSON.parse(fs.readFileSync(path.join(ROOT, 'closed.json'), 'utf8')).places || {};
    } catch { /* no closed list yet */ }
    const ids = new Set(places.map(p => p.id));
    const leaked = Object.keys(closed).filter(id => ids.has(id));
    assert(leaked.length === 0, `closed places still present: ${leaked.slice(0, 5).join(', ')}`);
  });

  check('sidebar offers an opening-hours filter', () => {
    const html = all.get('#mealFilters').innerHTML;
    assert(all.get('#mealFilters').hidden === false, 'the filter bar is hidden');
    assert(/data-meal="dinner"/.test(html), 'no dinner chip');
    assert(/data-meal=""/.test(html), 'no "any hours" reset chip');
  });

  check('choosing an hours filter narrows the list to those places', () => {
    all.a.state.meal = 'dinner';
    all.a.state.view = 'all';
    all.a.render();
    const got = countRows(all.get('#list').innerHTML);
    const want = places.filter(p => (p.meal_periods || []).includes('dinner')).length;
    assert(got === want, `expected ${want} dinner rows, got ${got}`);
    all.a.state.meal = '';
    all.a.render();
    assert(countRows(all.get('#list').innerHTML) === places.length, 'reset did not restore the list');
  });

  check('rows show real opening hours when they were fetched', () => {
    all.a.state.view = 'all';
    all.a.render();
    const html = all.get('#list').innerHTML;
    const withHours = places.filter(p => p.hours).length;
    assert(withHours > 0, 'no place has hours yet');
    assert(/(am|pm)/.test(html), 'no rendered hours found in the rows');
  });

  check('desktop: clicking a pin still opens the website', () => {
    const p = places.find(x => x.website && all.a.markers.has(x.id));
    all.win.touch = false;
    all.win.opened.length = 0;
    all.a.markers.get(p.id).fire('click');
    assert(all.win.opened.length === 1, `expected a page to open, got ${all.win.opened.length}`);
  });

  check('touch: tapping a pin reveals its entry instead of opening a page', () => {
    const p = places.find(x => x.website && all.a.markers.has(x.id));
    const row = all.get(`.row[data-place="${p.id}"]`);
    row.classList.remove('hot');
    all.win.touch = true;
    all.win.opened.length = 0;
    all.a.markers.get(p.id).fire('click');
    assert(all.win.opened.length === 0, 'a tap should not open a page');
    assert(row.classList.contains('hot'), 'the matching entry was not revealed');
    all.win.touch = false;
  });

  check('on mobile the scroll clears the sticky map', () => {
    const p = places.find(x => x.website && all.a.markers.has(x.id));
    const row = all.get(`.row[data-place="${p.id}"]`);
    const mapEl = all.get('#map');
    all.win.narrow = true;
    all.win.touch = true;
    all.win.pageYOffset = 1000;
    all.win.scrolled.length = 0;
    mapEl._rect = { top: 0, left: 0, right: 0, bottom: 300, width: 0, height: 300 };
    row._rect = { top: 500, left: 0, right: 0, bottom: 700, width: 0, height: 200 };
    all.a.markers.get(p.id).fire('click');
    const call = all.win.scrolled.at(-1);
    assert(call, 'window.scrollTo was not called');
    // row document top (1000 + 500) minus map height (300) and the 12px gap
    assert(call.top === 1188, `expected the row to clear the map at 1188, got ${call.top}`);
    all.win.narrow = false;
    all.win.touch = false;
  });

  check('the mobile layout puts the map above the list', () => {
    const css = fs.readFileSync(path.join(ROOT, 'web/index.html'), 'utf8');
    const block = css.match(/@media \(max-width: 760px\)\s*\{[\s\S]*?\n  \}/);
    assert(block, 'no mobile media query found');
    assert(/order:\s*-1/.test(block[0]),
           'without order:-1 the panel stays first and the map sits below every row');
    assert(/position:\s*sticky/.test(block[0]), 'the map should stay visible while scrolling');
  });

  check('the footer notice is gone', () => {
    const html = fs.readFileSync(path.join(ROOT, 'web/index.html'), 'utf8');
    assert(!/<footer/i.test(html), 'the footer notice is still present');
    assert(!/Wayback Machine captures of ny\.eater/.test(html), 'the notice text is still present');
  });

  check('the search box matches the description text, not just names', () => {
    let target = null, word = null;
    for (const p of places.filter(x => x.blurb)) {
      const shown = `${p.name} ${p.address || ''}`.toLowerCase();
      const w = (p.blurb.toLowerCase().match(/\b[a-z]{9,}\b/g) || []).find(w => !shown.includes(w));
      if (w) { target = p; word = w; break; }
    }
    assert(target, 'no distinctive blurb word found to test with');
    all.a.state.view = 'all';
    all.a.state.meal = '';
    all.a.state.q = word;
    all.a.render();
    const html = all.get('#list').innerHTML;
    assert(html.includes(`data-place="${target.id}"`),
           `searching "${word}" did not surface ${target.name}, whose blurb contains it`);
    const rows = countRows(html);
    assert(rows < places.length, 'the description search matched everything');
    all.a.state.q = '';
    all.a.render();
  });

  check('the search box still matches names', () => {
    const target = places.find(p => p.name.length > 6);
    all.a.state.q = target.name.toLowerCase().slice(0, 6);
    all.a.render();
    assert(all.get('#list').innerHTML.includes(`data-place="${target.id}"`),
           `searching by name did not find ${target.name}`);
    all.a.state.q = '';
    all.a.render();
  });

  check('markers exist for mapped places', () => {
    const mapped = places.filter(p => p.lat !== null && p.lng !== null).length;
    assert(all.a.markers.size === mapped, `expected ${mapped} markers, got ${all.a.markers.size}`);
  });
  const escHtml = t => String(t).replace(/[&<>"']/g, c =>
    ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[c]));

  check('sidebar rows carry the Eater blurb', () => {
    const want = places.filter(p => p.blurb && all.a.markers.has(p.id)).length;
    const got = (all.html.match(/class="blurb"/g) || []).length;
    assert(got > 200, `expected most rows to show a blurb, got ${got} of ${want}`);
    const p = places.find(x => x.blurb && all.a.markers.has(x.id));
    const fragment = escHtml(p.blurb.split(' ').slice(0, 4).join(' '));
    assert(all.html.includes(fragment), `blurb text missing for ${p.name}`);
  });

  // Not observable through the DOM stub -- this is a CSS layout invariant that
  // only a real engine would catch, so it is asserted against the stylesheet.
  check('the tooltip card is sized to its text, not to its longest word', () => {
    const css = fs.readFileSync(path.join(ROOT, 'web/index.html'), 'utf8');
    const rule = css.match(/\.leaflet-tooltip\.e38\s*\{[^}]*\}/s);
    assert(rule, 'tooltip style rule not found');
    assert(/white-space:\s*normal/.test(rule[0]), 'tooltip should wrap');
    assert(/width:\s*max-content/.test(rule[0]),
           'without width:max-content the zero-width tooltip pane collapses the card to min-content');
    assert(/max-width:/.test(rule[0]), 'tooltip needs a max-width cap');
  });

  check('the hover tooltip is name and address only', () => {
    const sample = places.filter(p => all.a.markers.has(p.id)).slice(0, 40);
    sample.forEach(p => {
      const html = String(all.a.markers.get(p.id).tooltip || '');
      assert(!/<a[\s>]/i.test(html), `${p.name}'s tooltip contains a link`);
      assert(!/no website on file/.test(html), `${p.name}'s tooltip has a website placeholder`);
      if (p.address) assert(html.includes('k">'), `${p.name}'s tooltip lost its address`);
    });
  });

  check('the hover tooltip carries no date ranges', () => {
    const sample = places.filter(p => all.a.markers.has(p.id)).slice(0, 30);
    sample.forEach(p => {
      const html = String(all.a.markers.get(p.id).tooltip || '');
      assert(!/\d{4}-\d{2}-\d{2}/.test(html),
             `${p.name}'s tooltip still shows a date range`);
    });
  });

  check('the hover tooltip stays short (no blurb in it)', () => {
    const withBlurb = places.filter(p => p.blurb && all.a.markers.has(p.id));
    withBlurb.slice(0, 25).forEach(p => {
      const html = String(all.a.markers.get(p.id).tooltip || '');
      const fragment = escHtml(p.blurb.split(' ').slice(0, 4).join(' '));
      assert(!html.includes(fragment), `blurb leaked back into ${p.name}'s tooltip`);
    });
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
