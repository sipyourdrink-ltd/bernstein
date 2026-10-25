import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import postcss from 'postcss';
import tailwindcss from 'tailwindcss';
import autoprefixer from 'autoprefixer';
import postcssConfig from '../postcss.config.js';
import tailwindConfig from '../tailwind.config.js';
import { typeScale } from '../src/lib/type-scale.js';

test('application stylesheet compiles with its semantic theme and local assets intact', async () => {
  const cssFile = new URL('../src/index.css', import.meta.url);
  const css = await readFile(cssFile, 'utf8');
  const content = 'dark bg-primary border-border text-foreground animate-in dark:bg-primary';
  const plugins = { tailwindcss, autoprefixer };
  const configuredPlugins = Object.entries(postcssConfig.plugins).map(([name, options]) => {
    assert.ok(plugins[name], 'exercise the configured PostCSS plugin: ' + name);
    return plugins[name](name === 'tailwindcss'
      ? { ...tailwindConfig, content: [{ raw: content }] }
      : options);
  });
  const { root } = await postcss(configuredPlugins).process(css, { from: cssFile.pathname });

  function declarations(selector, property) {
    const values = [];
    root.walkRules(selector, (rule) => rule.walkDecls(property, ({ value }) => values.push(value)));
    return values;
  }

  assert.ok(declarations('*', 'border-color').some((value) => value.includes('var(--border)')));
  assert.ok(declarations('body', 'background-color').some((value) => value.includes('var(--background)')));
  assert.ok(declarations('body', 'color').some((value) => value.includes('var(--foreground)')));
  assert.ok(declarations('.bg-primary', 'background-color').some((value) => value.includes('var(--primary)')));
  for (const [name, [size]] of Object.entries(typeScale)) {
    assert.ok(declarations('.text-' + name, 'font-size').includes(size), 'retain the ' + name + ' type scale');
  }
  assert.ok(declarations('.animate-in', 'animation-name').includes('enter'));
  assert.ok(declarations('.dark', '--background').length > 0);
  const fontFaces = [];
  root.walkAtRules('font-face', (rule) => rule.walkDecls('src', ({ value }) => fontFaces.push(value)));
  assert.equal(fontFaces.length, 4);
  assert.ok(fontFaces.every((value) => value.startsWith("url('./fonts/")));
  let reducedMotion = false;
  root.walkAtRules('media', (rule) => {
    if (rule.params === '(prefers-reduced-motion: reduce)') {
      rule.walkDecls('animation-duration', ({ value }) => { reducedMotion ||= value === '0.001ms'; });
    }
  });
  assert.ok(reducedMotion, 'retain reduced-motion behavior');
  const unexpanded = [];
  root.walkAtRules((rule) => {
    if (['tailwind', 'apply'].includes(rule.name)) unexpanded.push(rule.name);
  });
  assert.deepEqual(unexpanded, [], 'emit usable CSS rather than unprocessed framework directives');
});
