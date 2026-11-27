import assert from 'node:assert/strict';
import test from 'node:test';

function installWindow(initialHref) {
  const storage = new Map();
  const replaced = [];
  let href = initialHref;
  const parsed = new URL(initialHref);
  globalThis.window = {
    localStorage: {
      getItem: (k) => (storage.has(k) ? storage.get(k) : null),
      setItem: (k, v) => {
        storage.set(k, String(v));
      },
      removeItem: (k) => {
        storage.delete(k);
      },
    },
    location: {
      get href() {
        return href;
      },
      get hash() {
        return parsed.hash;
      },
    },
    history: {
      replaceState: (_state, _title, nextUrl) => {
        replaced.push(nextUrl);
        href = nextUrl;
        parsed.href = nextUrl;
      },
    },
  };
  return { storage, replaced };
}

test('captureAuthTokenFromFragment stores #t= via setAuthToken and scrubs URL fragment', async (t) => {
  const prevWindow = globalThis.window;
  t.after(() => {
    globalThis.window = prevWindow;
  });
  const { replaced } = installWindow('http://127.0.0.1:8052/ui/#t=loopback-bearer-123');
  const { captureAuthTokenFromFragment } = await import('../src/lib/pwa.ts');
  const { getAuthToken } = await import('../src/lib/api.ts');

  const captured = captureAuthTokenFromFragment();
  assert.equal(captured, 'loopback-bearer-123');
  assert.equal(getAuthToken(), 'loopback-bearer-123');
  assert.deepEqual(replaced, ['http://127.0.0.1:8052/ui/']);
  assert.equal(globalThis.window.location.hash, '');
});

test('captureAuthTokenFromFragment returns null when #t= is absent or empty', async (t) => {
  const prevWindow = globalThis.window;
  t.after(() => {
    globalThis.window = prevWindow;
  });
  const { captureAuthTokenFromFragment } = await import('../src/lib/pwa.ts');

  for (const url of ['http://127.0.0.1:8052/ui/', 'http://127.0.0.1:8052/ui/#t=']) {
    const { replaced } = installWindow(url);
    assert.equal(captureAuthTokenFromFragment(), null);
    assert.deepEqual(replaced, []);
  }
});
