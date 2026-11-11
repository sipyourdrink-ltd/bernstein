// A refused token must leave the operator one obvious way forward. These tests
// hold the contract between the two halves of that: api.ts drops the refused
// token and signals the shell, and the shell's prompt takes a new token,
// retries every query with it, and closes on the first accepted request.

import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import test, { after } from 'node:test';
import { Window } from 'happy-dom';

// React DOM and TanStack Query decide at import time whether they run in a
// browser, so the window is installed before either is loaded.
const window = new Window({ url: 'http://127.0.0.1:8052/ui/tasks' });
Object.assign(globalThis, {
  window,
  document: window.document,
  Event: window.Event,
  IS_REACT_ACT_ENVIRONMENT: true,
});

const { act, createElement: h } = await import('react');
const { createRoot } = await import('react-dom/client');
const { QueryClient, QueryClientProvider, useQuery } = await import('@tanstack/react-query');
const { MemoryRouter } = await import('react-router');
const { createServer } = await import('vite');

const vite = await createServer({
  root: fileURLToPath(new URL('..', import.meta.url)),
  configFile: fileURLToPath(new URL('../vite.config.ts', import.meta.url)),
  server: { middlewareMode: true, hmr: false },
  appType: 'custom',
  logLevel: 'silent',
});
const api = await vite.ssrLoadModule('/src/lib/api.ts');
const { AuthTokenPrompt } = await vite.ssrLoadModule('/src/components/AuthTokenPrompt.tsx');

after(async () => {
  await vite.close();
  await window.happyDOM.close();
});

function json(status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

function stubFetch(t, handler) {
  const original = globalThis.fetch;
  globalThis.fetch = handler;
  t.after(() => {
    globalThis.fetch = original;
  });
}

function listen(t, type) {
  const seen = [];
  const listener = (event) => seen.push(event);
  window.addEventListener(type, listener);
  t.after(() => window.removeEventListener(type, listener));
  return seen;
}

async function waitFor(condition, message) {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    if (condition()) return;
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
  }
  assert.fail(message);
}

// React tracks an input's value on the element itself, so the value is set
// through the prototype setter for the input event to register as a change.
async function type(input, text) {
  const setValue = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  await act(async () => {
    setValue.call(input, text);
    input.dispatchEvent(new window.Event('input', { bubbles: true }));
  });
}

async function clickSave(prompt) {
  await act(async () => {
    prompt.querySelector('button[type="submit"]').click();
  });
}

test('a 401 drops the refused token, signals the shell once and says what is missing', async (t) => {
  window.localStorage.clear();
  api.setAuthToken('stale-token');
  const signals = listen(t, api.UNAUTHORIZED_EVENT);
  let sent;
  stubFetch(t, async (_url, init) => {
    sent = init.headers.Authorization;
    return json(401, { detail: 'Invalid token' });
  });

  await assert.rejects(api.apiGet('/agents'), (err) => {
    assert.equal(err.status, 401);
    assert.equal(err.message, 'Access token missing or rejected');
    assert.deepEqual(err.body, { detail: 'Invalid token' });
    return true;
  });
  assert.equal(sent, 'Bearer stale-token');
  assert.equal(api.getAuthToken(), null, 'the refused token is still stored');
  assert.equal(signals.length, 1);
});

test('a 401 for a request sent before a new token was saved leaves that token alone', async (t) => {
  window.localStorage.clear();
  api.setAuthToken('old-token');
  const signals = listen(t, api.UNAUTHORIZED_EVENT);
  let answer;
  stubFetch(t, () => new Promise((resolve) => {
    answer = resolve;
  }));

  const inFlight = api.apiGet('/agents');
  api.setAuthToken('new-token');
  answer(json(401, { detail: 'Invalid token' }));

  await assert.rejects(inFlight, { status: 401 });
  assert.equal(api.getAuthToken(), 'new-token', 'a stale 401 wiped the token saved after it was sent');
  assert.equal(signals.length, 0);
});

test('the shell prompt appears on a 401, stores the pasted token, retries and closes once a request succeeds', async (t) => {
  window.localStorage.clear();
  stubFetch(t, async (_url, init) =>
    init.headers.Authorization === 'Bearer operator-token'
      ? json(200, { agents: 3 })
      : json(401, { detail: 'Invalid token' }),
  );

  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  // Stands in for any screen: one query through the shared fetch wrapper.
  function Screen() {
    const q = useQuery({ queryKey: ['agents'], queryFn: () => api.apiGet('/agents') });
    return h('output', null, q.data ? `agents: ${q.data.agents}` : q.isError ? 'failed' : 'loading');
  }
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  t.after(async () => {
    await act(async () => root.unmount());
    client.clear();
    container.remove();
  });
  await act(async () => {
    root.render(
      h(QueryClientProvider, { client }, h(MemoryRouter, null, h(AuthTokenPrompt), h(Screen))),
    );
  });

  const prompt = () => container.querySelector('[role="alert"]');
  await waitFor(() => prompt() !== null, 'no prompt after the first 401');
  assert.match(prompt().textContent, /Access token missing or rejected/);
  assert.equal(prompt().querySelector('input').type, 'password');
  assert.equal(prompt().querySelector('a').getAttribute('href'), '/settings');

  // A token the server refuses as well keeps the prompt up and says so.
  await type(prompt().querySelector('input'), 'wrong-token');
  await clickSave(prompt());
  await waitFor(
    () => /rejected that token/.test(prompt()?.textContent ?? ''),
    'a refused token was not reported',
  );
  assert.equal(api.getAuthToken(), null);

  // The right token is stored, the failed query retries with it, and that
  // success closes the prompt.
  await type(prompt().querySelector('input'), '  operator-token  ');
  await clickSave(prompt());
  assert.equal(api.getAuthToken(), 'operator-token');
  await waitFor(() => prompt() === null, 'the prompt stayed up after a request succeeded');
  assert.equal(container.querySelector('output').textContent, 'agents: 3');
});
