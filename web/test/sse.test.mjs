import assert from 'node:assert/strict';
import test from 'node:test';

const encoder = new TextEncoder();

function responseFromChunks(chunks) {
  return {
    ok: true,
    status: 200,
    body: new ReadableStream({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
        controller.close();
      },
    }),
  };
}

async function waitFor(condition, message) {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    if (condition()) return;
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  assert.fail(message);
}

test('authenticated SSE streams dispatch a record split inside its terminator for both GUI endpoints', async () => {
  const { openEventStream } = await import('../src/lib/sse.ts');
  const endpoints = [
    ['/api/v1/events', 'task_update'],
    ['/api/v1/events/cost', 'cost_tick'],
  ];

  for (const [url, type] of endpoints) {
    const received = [];
    let close;
    const stream = openEventStream(url, {
      fetchImpl: async (requestedUrl, init) => {
        assert.equal(requestedUrl, url);
        assert.equal(init.headers.Authorization, 'Bearer test-token');
        assert.equal(init.headers.Accept, 'text/event-stream');
        return responseFromChunks([
          `event: ${type}\ndata: {"source":"split"}\n`,
          '\n',
        ]);
      },
      getToken: () => 'test-token',
      onEvent: (event) => {
        received.push(event);
        close();
      },
      onError: () => assert.fail('a complete stream record must not be treated as an error'),
      backoffMs: [0],
    });
    close = stream.close;

    await waitFor(() => received.length === 1, `${url} did not dispatch its split event`);
    assert.deepEqual(received, [{ type, data: { source: 'split' } }]);
  }
});

test('mixed CRLF/LF record separators survive framing intact', async () => {
  const { openEventStream } = await import('../src/lib/sse.ts');
  const separators = ['\r\n\n', '\n\r\n', '\n\n', '\r\n\r\n'];
  const received = [];
  let close;
  const stream = openEventStream('/api/v1/events', {
    fetchImpl: async () => {
      let body = '';
      for (const sep of separators) {
        body += `event: evt_${separators.indexOf(sep)}\ndata: {"payload":"${separators.indexOf(sep)}"}${sep}`;
      }
      return responseFromChunks([body]);
    },
    onEvent: (event) => received.push(event),
    onError: () => assert.fail('mixed-separator records must not be treated as an error'),
  });
  close = stream.close;

  await waitFor(() => received.length === separators.length, 'mixed-separator records did not all dispatch');
  for (let i = 0; i < separators.length; i += 1) {
    assert.deepEqual(received[i], { type: `evt_${i}`, data: { payload: `${i}` } });
  }
});

test('a clean EOF reconnects', async () => {
  const { openEventStream } = await import('../src/lib/sse.ts');
  const received = [];
  let attempts = 0;
  let close;
  const stream = openEventStream('/api/v1/events', {
    fetchImpl: async () => {
      attempts += 1;
      return attempts === 1
        ? responseFromChunks([])
        : responseFromChunks(['event: message\ndata: {"reconnected":true}\n\n']);
    },
    onEvent: (event) => {
      received.push(event);
      close();
    },
    onError: () => assert.fail('a clean EOF must reconnect before reporting an error'),
    backoffMs: [0],
  });
  close = stream.close;

  await waitFor(() => received.length === 1, 'clean EOF did not reconnect');
  assert.equal(attempts, 2);
});

test('connect-time 401 clears the credential without retrying', async () => {
  const { openEventStream } = await import('../src/lib/sse.ts');
  const errors = [];
  let cleared = 0;
  let unauthorizedAttempts = 0;
  openEventStream('/api/v1/events', {
    fetchImpl: async () => {
      unauthorizedAttempts += 1;
      return { ok: false, status: 401, body: null };
    },
    clearToken: () => {
      cleared += 1;
    },
    onEvent: () => assert.fail('401 responses do not have stream events'),
    onError: (attemptCount) => errors.push(attemptCount),
    backoffMs: [0],
  });

  await waitFor(() => errors.length === 1, '401 did not report a terminal error');
  assert.deepEqual(errors, [0]);
  assert.equal(cleared, 1);
  assert.equal(unauthorizedAttempts, 1);
});

test('a reader failure after an accepted response reconnects instead of guessing it was a 401', async () => {
  const { openEventStream } = await import('../src/lib/sse.ts');
  const received = [];
  let attempts = 0;
  let close;
  const stream = openEventStream('/api/v1/events', {
    fetchImpl: async () => {
      attempts += 1;
      if (attempts === 1) {
        return {
          ok: true,
          status: 200,
          body: new ReadableStream({
            start(controller) {
              controller.error(new Error('connection ended after 200'));
            },
          }),
        };
      }
      return responseFromChunks(['event: message\ndata: {"reconnected":true}\n\n']);
    },
    onEvent: (event) => {
      received.push(event);
      close();
    },
    onError: () => assert.fail('a post-200 reader failure should reconnect'),
    backoffMs: [0],
  });
  close = stream.close;

  await waitFor(() => received.length === 1, 'reader failure did not reconnect');
  assert.equal(attempts, 2);
});

test('closing a pending authenticated stream cancels and releases its reader', async () => {
  const { openEventStream } = await import('../src/lib/sse.ts');
  let resolveRead;
  let cancelled = 0;
  let released = 0;
  const reader = {
    read: () => new Promise((resolve) => {
      resolveRead = resolve;
    }),
    cancel: async () => {
      cancelled += 1;
      resolveRead({ done: true });
    },
    releaseLock: () => {
      released += 1;
    },
  };
  let fetched = false;
  const stream = openEventStream('/api/v1/events', {
    fetchImpl: async () => {
      fetched = true;
      return { ok: true, status: 200, body: { getReader: () => reader } };
    },
    onEvent: () => assert.fail('the pending reader must not emit an event'),
    onError: () => assert.fail('closing a stream must not report an error'),
  });

  await waitFor(() => fetched, 'stream never started reading');
  stream.close();
  await waitFor(() => released === 1, 'reader lock was not released after close');
  assert.equal(cancelled, 1);
});
