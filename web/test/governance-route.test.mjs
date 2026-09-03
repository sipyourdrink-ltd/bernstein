// The governance coverage panel is the one screen that must never flatter the
// installation it describes. These tests hold the three properties that make
// that true: an unmeasured metric is absent rather than zero, every number
// carries the denominator it was taken over, and no bar is coloured by its own
// value.

import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';

const webRoot = fileURLToPath(new URL('..', import.meta.url));

async function loadPanel(t) {
  const vite = await createServer({
    root: webRoot,
    configFile: fileURLToPath(new URL('../vite.config.ts', import.meta.url)),
    server: { middlewareMode: true },
    appType: 'custom',
    logLevel: 'silent',
  });
  t.after(() => vite.close());
  return vite.ssrLoadModule('/src/routes/Governance.tsx');
}

function report(metrics) {
  return { run_id: 'run-under-test', captured_at: '2026-09-01T18:22:00Z', metrics };
}

const MEASURED = {
  id: 'attributable_actions',
  label: 'Actions attributable to a principal',
  denominator_label: 'agent actions recorded in this run',
  covered: 34,
  total: 100,
};

const UNMEASURED = {
  id: 'delegation_hops',
  label: 'Delegation hops with a grant chain',
  denominator_label: 'delegation hops recorded in this run',
  covered: null,
  total: null,
  unmeasured_reason: 'no grant chain was written',
};

function rowFor(html, id) {
  const start = html.indexOf(`data-metric="${id}"`);
  assert.notEqual(start, -1, `metric ${id} is absent from the panel`);
  const next = html.indexOf('data-metric="', start + 1);
  return html.slice(start, next === -1 ? undefined : next);
}

// 1
test('a metric with no data renders as not measured rather than as zero', async (t) => {
  const { GovernancePanel } = await loadPanel(t);
  const html = renderToStaticMarkup(
    createElement(GovernancePanel, { coverage: report([UNMEASURED, { ...MEASURED, covered: 0, total: 0 }]) }),
  );

  for (const id of ['delegation_hops', 'attributable_actions']) {
    const row = rowFor(html, id);
    assert.ok(row.includes('data-metric-state="not-measured"'), `${id} claims to be measured`);
    assert.ok(row.includes('not measured'), `${id} does not say it is not measured`);
    assert.ok(!row.includes('data-metric-bar'), `${id} draws a bar for a number it does not have`);
    assert.ok(!/\b0%/.test(row), `${id} renders an unmeasured metric as 0%`);
    assert.ok(!/NaN/.test(row), `${id} rendered NaN`);
  }
  assert.ok(rowFor(html, 'delegation_hops').includes('no grant chain was written'));
});

// 2
test('a measured zero renders as a zero fraction and is not called not measured', async (t) => {
  const { GovernancePanel } = await loadPanel(t);
  const html = renderToStaticMarkup(
    createElement(GovernancePanel, { coverage: report([{ ...MEASURED, covered: 0, total: 18 }]) }),
  );

  const row = rowFor(html, 'attributable_actions');
  assert.ok(row.includes('data-metric-state="measured"'), 'a measured zero was reported as unmeasured');
  assert.ok(!row.includes('not measured'), 'a measured zero is labelled not measured');
  assert.ok(row.includes('0%'), 'a measured zero does not render 0%');
  assert.ok(row.includes('0 / 18'), 'a measured zero does not render its fraction');
  assert.ok(row.includes('data-metric-bar'), 'a measured zero draws no bar track');
});

// 3
test('every measured metric names the denominator its fraction was taken over', async (t) => {
  const { GovernancePanel } = await loadPanel(t);
  const metrics = [
    MEASURED,
    { ...MEASURED, id: 'recomputable_decisions', label: 'Decisions recomputable from inputs', denominator_label: 'decisions recorded in this run', covered: 12, total: 12 },
  ];
  const html = renderToStaticMarkup(createElement(GovernancePanel, { coverage: report(metrics) }));

  for (const metric of metrics) {
    const row = rowFor(html, metric.id);
    assert.ok(row.includes(metric.denominator_label), `${metric.id} renders a number with no named denominator`);
    assert.ok(row.includes(`${metric.covered} / ${metric.total}`), `${metric.id} renders no fraction`);
  }
});

// 4
test('a partial fraction never rounds up to 100% or down to 0%', async (t) => {
  const { GovernancePanel } = await loadPanel(t);
  const metrics = [
    { ...MEASURED, id: 'nearly_all', covered: 999, total: 1000 },
    { ...MEASURED, id: 'nearly_none', covered: 1, total: 1000 },
    { ...MEASURED, id: 'exactly_all', covered: 7, total: 7 },
  ];
  const html = renderToStaticMarkup(createElement(GovernancePanel, { coverage: report(metrics) }));

  assert.ok(!rowFor(html, 'nearly_all').includes('100%'), '999/1000 is presented as complete coverage');
  assert.ok(!/\b0%/.test(rowFor(html, 'nearly_none')), '1/1000 is presented as no coverage');
  assert.ok(rowFor(html, 'exactly_all').includes('100%'), '7/7 is not presented as complete coverage');
});

// 5
test('no bar is coloured by its value and the panel renders no aggregate score', async (t) => {
  const { GovernancePanel } = await loadPanel(t);
  const metrics = [
    { ...MEASURED, id: 'low', covered: 3, total: 100 },
    { ...MEASURED, id: 'mid', covered: 50, total: 100 },
    { ...MEASURED, id: 'high', covered: 100, total: 100 },
  ];
  const html = renderToStaticMarkup(createElement(GovernancePanel, { coverage: report(metrics) }));

  const fills = ['low', 'mid', 'high'].map((id) => {
    const match = rowFor(html, id).match(/data-metric-bar="fill"[^>]*class="([^"]*)"/);
    assert.ok(match, `${id} renders no bar fill`);
    return match[1];
  });
  assert.equal(new Set(fills).size, 1, 'bar colour varies with the value, which is a traffic light');

  for (const word of ['score', 'grade', 'overall', 'rating']) {
    assert.ok(!html.toLowerCase().includes(word), `the panel renders an aggregate "${word}"`);
  }
});

// 6
test('the governance route is in the sidebar, routed, and titled', async (t) => {
  const { NAV } = await (async () => {
    const vite = await createServer({
      root: webRoot,
      configFile: fileURLToPath(new URL('../vite.config.ts', import.meta.url)),
      server: { middlewareMode: true },
      appType: 'custom',
      logLevel: 'silent',
    });
    t.after(() => vite.close());
    return vite.ssrLoadModule('/src/components/AppShell.tsx');
  })();

  assert.ok(
    NAV.some((entry) => entry.to === '/governance'),
    '/governance is not in the AppShell NAV array',
  );

  const app = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
  assert.match(app, /path="\/governance" element=\{<Governance \/>\}/, '/governance is not routed');
  assert.match(app, /'\/governance': 'Governance'/, '/governance has no document title');
});

// 7
test('the committed fixture exercises both the measured and the not-measured state', async (t) => {
  const { GovernancePanel, coverageFixture } = await loadPanel(t);
  const html = renderToStaticMarkup(createElement(GovernancePanel, { coverage: coverageFixture }));

  assert.ok(html.includes('data-metric-state="measured"'), 'the fixture has no measured metric');
  assert.ok(html.includes('data-metric-state="not-measured"'), 'the fixture has no unmeasured metric');
  for (const metric of coverageFixture.metrics) {
    assert.ok(html.includes(`data-metric="${metric.id}"`), `${metric.id} is not rendered`);
    assert.ok(html.includes(metric.denominator_label), `${metric.id} renders without its denominator`);
  }
});
