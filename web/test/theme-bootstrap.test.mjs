import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const indexHtml = await readFile(new URL('../index.html', import.meta.url), 'utf8');
const themeProviderSource = await readFile(new URL('../src/components/ThemeProvider.tsx', import.meta.url), 'utf8');
const bootstrapMatch = indexHtml.match(/<script id="theme-bootstrap">([\s\S]*?)<\/script>/);

function runBootstrap({
  storedTheme,
  systemIsDark,
  storageThrows = false,
  matchMediaThrows = false,
  matchMediaUnavailable = false,
  documentElementThrows = false,
}) {
  const classes = new Set();
  const documentElement = {
    classList: {
      add: (theme) => classes.add(theme),
      remove: (...themes) => themes.forEach((theme) => classes.delete(theme)),
    },
  };
  const storage = {
    getItem: () => {
      if (storageThrows) throw new Error('storage blocked');
      return storedTheme;
    },
  };
  const context = {
    document: {
      get documentElement() {
        if (documentElementThrows) throw new Error('document unavailable');
        return documentElement;
      },
    },
    localStorage: storage,
    window: {
      localStorage: storage,
      matchMedia: matchMediaUnavailable
        ? undefined
        : () => {
            if (matchMediaThrows) throw new Error('media query unavailable');
            return { matches: systemIsDark };
          },
    },
  };

  vm.runInNewContext(bootstrapMatch[1], context);
  return [...classes];
}

test('theme bootstrap resolves preferences the same way as ThemeProvider before hydration', async () => {
  assert.ok(bootstrapMatch, 'index.html must include the pre-hydration theme bootstrap script');

  const { DEFAULT_THEME_STORAGE_KEY, THEMES, isTheme, resolveTheme } = await import('../src/components/theme.ts');
  const bootstrap = bootstrapMatch[1];

  assert.match(
    bootstrap,
    new RegExp(`localStorage\\.getItem\\('${DEFAULT_THEME_STORAGE_KEY}'\\)`),
    'the bootstrap must read the ThemeProvider default storage key',
  );
  assert.match(
    themeProviderSource,
    /storageKey = DEFAULT_THEME_STORAGE_KEY/,
    'ThemeProvider must use the same exported default storage key',
  );
  for (const theme of THEMES) {
    assert.match(bootstrap, new RegExp(`storedTheme === '${theme}'`));
  }

  const cases = [
    ['dark', false],
    ['light', true],
    ['system', true],
    ['system', false],
    [null, true],
    [null, false],
    ['blue', true],
    ['blue', false],
  ];

  for (const [storedTheme, systemIsDark] of cases) {
    const providerTheme = isTheme(storedTheme) ? storedTheme : null;
    assert.deepEqual(
      runBootstrap({ storedTheme, systemIsDark }),
      [resolveTheme(providerTheme, systemIsDark ? 'dark' : 'light')],
      `stored theme ${storedTheme} with ${systemIsDark ? 'dark' : 'light'} system preference`,
    );
  }
});

test('theme bootstrap falls back to the system preference when storage is blocked', () => {
  assert.ok(bootstrapMatch, 'index.html must include the pre-hydration theme bootstrap script');
  assert.deepEqual(runBootstrap({ systemIsDark: true, storageThrows: true }), ['dark']);
  assert.deepEqual(runBootstrap({ systemIsDark: false, storageThrows: true }), ['light']);
});

test('theme bootstrap degrades safely when browser APIs are unavailable or throw', () => {
  assert.ok(bootstrapMatch, 'index.html must include the pre-hydration theme bootstrap script');

  assert.deepEqual(runBootstrap({ matchMediaUnavailable: true }), ['dark']);
  assert.deepEqual(runBootstrap({ matchMediaThrows: true }), ['dark']);
  assert.doesNotThrow(() => runBootstrap({ documentElementThrows: true }));
});

test('ThemeProvider storage reader is safe outside a browser', async () => {
  const { readStoredTheme } = await import('../src/components/theme.ts');
  assert.equal(readStoredTheme('bernstein-theme'), null);
});
