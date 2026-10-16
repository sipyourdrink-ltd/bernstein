import { test, describe, beforeEach, mock } from "node:test";
import assert from "node:assert";
import { HeartbeatCell } from "../src/heartbeat_cell.js";

// Mock for DurableObjectState
class MockDurableObjectState {
  constructor() {
    this.storage = new Map();
  }

  // Simulate waitUntil for compatibility, but don't block
  async waitUntil(promise) {
    await promise;
  }
}

describe("HeartbeatCell", () => {
  let state;
  let env;
  let cell;

  beforeEach(() => {
    state = new MockDurableObjectState();
    env = {
      SHARD_URL: "http://mock-shard-url.com",
      FLUSH_MS: 100, // Shorter flush interval for testing
    };
    cell = new HeartbeatCell(state, env);

    // Mock fetch
    global.fetch = mock.fn(async (url, options) => {
      if (url === `${env.SHARD_URL}/cluster/nodes/heartbeats` && options.method === "POST") {
        const body = JSON.parse(options.body);
        return new Response(
          JSON.stringify({
            status: "ok",
            unknown: body.heartbeats.filter((h) => h.node_id === "unknown").map((h) => h.node_id),
          }),
          { status: 200 }
        );
      }
      return new Response("Not Found", { status: 404 });
    });
  });

  test("should buffer heartbeats and flush on size", async () => {
    // Fill the buffer almost to the limit
    for (let i = 0; i < 199; i++) {
      await cell.handleBuffer(new Request("http://localhost/buffer", { method: "POST", body: JSON.stringify({ node_id: `node-${i}` }) }));
    }
    assert.strictEqual(cell.buffer.length, 199);
    assert.strictEqual(global.fetch.mock.calls.length, 0);

    // Add one more heartbeat to trigger flush
    await cell.handleBuffer(new Request("http://localhost/buffer", { method: "POST", body: JSON.stringify({ node_id: "node-199" }) }));

    // Wait for the flush to complete
    await new Promise((resolve) => setTimeout(resolve, 10)); // Give event loop a moment

    assert.strictEqual(cell.buffer.length, 0);
    assert.strictEqual(global.fetch.mock.calls.length, 1);
    const call = global.fetch.mock.calls[0].arguments[1];
    const body = JSON.parse(call.body);
    assert.strictEqual(body.heartbeats.length, 200);
  });

  test("should buffer heartbeats and flush on timer", async () => {
    await cell.handleBuffer(new Request("http://localhost/buffer", { method: "POST", body: JSON.stringify({ node_id: "node-A" }) }));
    await cell.handleBuffer(new Request("http://localhost/buffer", { method: "POST", body: JSON.stringify({ node_id: "node-B" }) }));

    assert.strictEqual(cell.buffer.length, 2);
    assert.strictEqual(global.fetch.mock.calls.length, 0);

    // Wait for timer to flush
    await new Promise((resolve) => setTimeout(resolve, env.FLUSH_MS + 50));

    assert.strictEqual(cell.buffer.length, 0);
    assert.strictEqual(global.fetch.mock.calls.length, 1);
    const call = global.fetch.mock.calls[0].arguments[1];
    const body = JSON.parse(call.body);
    assert.strictEqual(body.heartbeats.length, 2);
  });

  test("should handle unknown node IDs", async () => {
    // Simulate initial flush with an unknown ID
    global.fetch.mock.mockImplementationOnce(async () => {
      return new Response(JSON.stringify({ status: "ok", unknown: ["unknown-node"] }), { status: 200 });
    });

    await cell.handleBuffer(new Request("http://localhost/buffer", { method: "POST", body: JSON.stringify({ node_id: "test-node-1" }) }));
    await new Promise((resolve) => setTimeout(resolve, env.FLUSH_MS + 50)); // Trigger flush

    assert.ok(cell.unknown_ids.has("unknown-node"));

    // Send a heartbeat for the unknown node, it should be rejected
    const response = await cell.handleBuffer(new Request("http://localhost/buffer", { method: "POST", body: JSON.stringify({ node_id: "unknown-node" }) }));
    assert.strictEqual(response.status, 404);
    assert.strictEqual(cell.unknown_ids.has("unknown-node"), false, "Unknown ID should be removed after rejection");
  });

  test("should retry on flush failure with exponential backoff", async () => {
    // Make fetch fail once
    global.fetch.mock.mockImplementationOnce(async () => {
      return new Response("Internal Server Error", { status: 500 });
    });

    const initialBackoff = cell.backoff_delay;

    await cell.handleBuffer(new Request("http://localhost/buffer", { method: "POST", body: JSON.stringify({ node_id: "node-1" }) }));
    await new Promise((resolve) => setTimeout(resolve, env.FLUSH_MS + 50)); // First flush attempt

    assert.strictEqual(global.fetch.mock.calls.length, 1);
    assert.strictEqual(cell.buffer.length, 1); // Heartbeat should be re-added
    assert.strictEqual(cell.backoff_delay, initialBackoff * 2);

    // Wait for retry
    await new Promise((resolve) => setTimeout(resolve, initialBackoff * 2 + 50));

    assert.strictEqual(global.fetch.mock.calls.length, 2);
    assert.strictEqual(cell.buffer.length, 0); // Should have flushed successfully this time
    assert.strictEqual(cell.backoff_delay, 1000); // Backoff reset
  });
});

// Mock global objects needed for HeartbeatCell to run in Node.js
if (typeof Request === "undefined") {
  global.Request = class MockRequest {
    constructor(url, options) {
      this.url = url;
      this.method = options?.method || "GET";
      this._body = options?.body;
    }
    async json() {
      return JSON.parse(this._body);
    }
  };
}

if (typeof Response === "undefined") {
  global.Response = class MockResponse {
    constructor(body, options) {
      this.body = body;
      this.status = options?.status || 200;
      this.statusText = options?.statusText || "OK";
      this.headers = options?.headers || {};
    }
    ok = this.status >= 200 && this.status < 300;
    async json() {
      return JSON.parse(this.body);
    }
  };
}

if (typeof URL === "undefined") {
  global.URL = class MockURL extends URL {};
}
