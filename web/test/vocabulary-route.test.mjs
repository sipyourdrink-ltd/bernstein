import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';

const webRoot = fileURLToPath(new URL('..', import.meta.url));

function declaredCustomProperties(css) {
  const withoutComments = css.replace(/\/\*[\s\S]*?\*\//g, '');
  return [...new Set([...withoutComments.matchAll(/(--[a-z0-9-]+)\s*:/gi)].map((match) => match[1]))]
    .sort();
}

function fakeDeclaration(properties) {
  return {
    length: properties.length,
    item: (index) => properties[index] ?? '',
  };
}

function fakeRule(selectorText, properties) {
  return { selectorText, style: fakeDeclaration(properties) };
}

test('developer vocabulary route renders every custom property and Tailwind type step', async (t) => {
  const css = await readFile(new URL('../src/index.css', import.meta.url), 'utf8');
  const expectedTokens = declaredCustomProperties(css);

  const vite = await createServer({
    root: webRoot,
    configFile: fileURLToPath(new URL('../vite.config.ts', import.meta.url)),
    server: { middlewareMode: true },
    appType: 'custom',
    logLevel: 'silent',
  });
  t.after(() => vite.close());

  const [{ collectCssTokens, VocabularyContent }, tailwindModule] = await Promise.all([
    vite.ssrLoadModule('/src/routes/Vocabulary.tsx'),
    vite.ssrLoadModule('/tailwind.config.js'),
  ]);
  const tokens = collectCssTokens(
    [{
      cssRules: [
        fakeRule(':root', expectedTokens),
        fakeRule(':root', ['--lightningcss-light']),
        fakeRule('*', ['--tw-ring-color']),
      ],
    }],
    (name) => `resolved ${name}`,
  );

  assert.deepEqual(tokens.map((token) => token.name), expectedTokens);

  const html = renderToStaticMarkup(createElement(VocabularyContent, { tokens }));
  for (const token of expectedTokens) {
    assert.ok(html.includes(`data-token="${token}"`), `${token} is absent from the rendered route`);
  }
  assert.ok(!html.includes('data-token="--tw-ring-color"'), 'Tailwind internals leak into the route');
  assert.ok(!html.includes('data-token="--lightningcss-light"'), 'LightningCSS internals leak into the route');
  assert.ok(!html.includes('hsl(var(--radius))'), '--radius is rendered as a color swatch');

  const typeSteps = Object.keys(tailwindModule.default.theme.extend.fontSize);
  for (const step of typeSteps) {
    assert.ok(html.includes(`data-type-step="${step}"`), `${step} is absent from the rendered type scale`);
    assert.ok(
      tailwindModule.default.safelist.includes(`text-${step}`),
      `${step} is not emitted for the runtime-rendered type scale`,
    );
  }
});
