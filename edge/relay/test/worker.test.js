import { describe, it, mock } from "node:test";
import assert from "node:assert/strict";
import worker from "../src/worker.js";
import { HashRing } from "../src/ring.js";

// Mock environment variables
import { createHash } from "node:crypto";
import { HashRing as RefRing } from "../src/ring.js";

function expectedShardUrl(name, url) {
  const id = createHash("sha256").update(`${name}|${url}`, "utf8").digest("hex").slice(0, 12);
  const shardId = new RefRing(["shard1", "shard2"], 100).lookup(id);
  return MOCK_SHARD_MAP.find((sh) => sh.id === shardId).url;
}

const MOCK_SHARD_MAP = [
  { id: "shard1", url: "http://shard1.example.com" },
  { id: "shard2", url: "http://shard2.example.com" },
];
const env = {
  SHARD_MAP: JSON.stringify(MOCK_SHARD_MAP),
};

// Mock fetch
const mockFetch = mock.fn(async (request) => {
  const url = new URL(request.url);
  const headers = Object.fromEntries(request.headers.entries());

  if (url.pathname.startsWith("/cluster/nodes/")) {
    const nodeId = url.pathname.split("/")[3]; // /cluster/nodes/{node_id}/...

    if (nodeId === "node1" || nodeId === "node3") {
      if (url.origin === MOCK_SHARD_MAP[0].url) {
        return new Response(`Routed to shard1 for ${nodeId}`, { status: 200, headers: { "X-Shard": "shard1" } });
      }
    } else if (nodeId === "node2") {
      if (url.origin === MOCK_SHARD_MAP[1].url) {
        return new Response(`Routed to shard2 for ${nodeId}`, { status: 200, headers: { "X-Shard": "shard2" } });
      }
    } else if (nodeId === "misdirected_node") {
      return new Response("Misdirected", { status: 421, headers: { "X-Shard": "some_other_shard" } });
    }
  } else if (url.pathname === "/cluster/nodes" && request.method === "POST") {
    const requestBody = JSON.parse(await request.clone().text());
    return new Response(JSON.stringify({ name: requestBody.name, status: "registered" }), { status: 201 });
  }

  return new Response("Not Found in Mock", { status: 404 });
});

global.fetch = mockFetch;

describe("worker.js fetch handler", () => {
  it("should route GET requests to the correct shard", async () => {
    mockFetch.mock.resetCalls(); // Reset calls before each test

    // Mock the hash ring lookup to return a specific shard for a node
    mock.method(HashRing.prototype, "lookup", (nodeId) => {
      if (nodeId === "node1") return "shard1";
      if (nodeId === "node2") return "shard2";
      return "shard1"; // Default for others
    });

    const request1 = new Request("http://example.com/cluster/nodes/node1/status", { method: "GET" });
    const response1 = await worker.fetch(request1, env);
    assert.strictEqual(response1.status, 200);
    assert.strictEqual(await response1.text(), "Routed to shard1 for node1");
    assert.strictEqual(mockFetch.mock.calls[0].arguments[0].url, "http://shard1.example.com/cluster/nodes/node1/status");

    const request2 = new Request("http://example.com/cluster/nodes/node2/health", { method: "GET" });
    const response2 = await worker.fetch(request2, env);
    assert.strictEqual(response2.status, 200);
    assert.strictEqual(await response2.text(), "Routed to shard2 for node2");
    assert.strictEqual(mockFetch.mock.calls[1].arguments[0].url, "http://shard2.example.com/cluster/nodes/node2/health");
  });

  it("should route POST /cluster/nodes requests for registration to the correct shard", async () => {
    mockFetch.mock.resetCalls(); // Reset calls before each test

    mock.method(HashRing.prototype, "lookup", (nodeId) => {
      if (nodeId === "node_to_register_on_shard1") return "shard1";
      if (nodeId === "node_to_register_on_shard2") return "shard2";
      return "shard1"; // Default
    });

    const request1 = new Request("http://example.com/cluster/nodes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: "test-node-1", url: "http://test1.example.com" }),
    });
    const response1 = await worker.fetch(request1, env);
    assert.strictEqual(response1.status, 201);
    const responseBody1 = await response1.json();
    assert.strictEqual(responseBody1.name, "test-node-1");
    assert.strictEqual(mockFetch.mock.calls[0].arguments[0].url, `${expectedShardUrl("test-node-1", "http://test1.example.com")}/cluster/nodes`);

    const request2 = new Request("http://example.com/cluster/nodes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: "test-node-2", url: "http://test2.example.com" }),
    });
    const response2 = await worker.fetch(request2, env);
    assert.strictEqual(response2.status, 201);
    const responseBody2 = await response2.json();
    assert.strictEqual(responseBody2.name, "test-node-2");
    assert.strictEqual(mockFetch.mock.calls[1].arguments[0].url, `${expectedShardUrl("test-node-2", "http://test2.example.com")}/cluster/nodes`);
  });

  it("should pass through 421 (Misdirected) responses", async () => {
    mockFetch.mock.resetCalls(); // Reset calls before each test

    mock.method(HashRing.prototype, "lookup", (nodeId) => {
      return "shard1"; // Always route to shard1 for this test
    });

    // Configure mockFetch to return 421 for a specific node_id
    mockFetch.mock.mockImplementationOnce(async (request) => {
      const url = new URL(request.url);
      if (url.pathname.includes("misdirected_node")) {
        return new Response("Misdirected to another shard", { status: 421, headers: { "X-Original-Shard": "some_other_shard" } });
      }
      return new Response("OK", { status: 200 });
    });

    const request = new Request("http://example.com/cluster/nodes/misdirected_node/status", { method: "GET" });
    const response = await worker.fetch(request, env);
    assert.strictEqual(response.status, 421);
    assert.strictEqual(await response.text(), "Misdirected to another shard");
    assert.strictEqual(response.headers.get("X-Original-Shard"), "some_other_shard");
  });

  it("should return 404 for unknown paths", async () => {
    mockFetch.mock.resetCalls(); // Reset calls before each test

    const request = new Request("http://example.com/unknown/path", { method: "GET" });
    const response = await worker.fetch(request, env);
    assert.strictEqual(response.status, 404);
    assert.strictEqual(await response.text(), "Not Found");
  });

  it("should forward Authorization and Content-Type headers", async () => {
    mockFetch.mock.resetCalls(); // Reset calls before each test

    mock.method(HashRing.prototype, "lookup", (nodeId) => "shard1");

    mockFetch.mock.mockImplementationOnce(async (request) => {
      assert.strictEqual(request.headers.get("Authorization"), "Bearer [REDACTED:auth_header]");
      assert.strictEqual(request.headers.get("Content-Type"), "application/json");
      return new Response("OK", { status: 200 });
    });

    const request = new Request("http://example.com/cluster/nodes/node1/action", {
      method: "POST",
      headers: {
        Authorization: "Bearer [REDACTED:auth_header]",
        "Content-Type": "application/json",
        "X-Custom-Header": "should-be-filtered",
      },
      body: JSON.stringify({ key: "value" }),
    });
    await worker.fetch(request, env);
  });

  it("should filter out unallowed headers", async () => {
    mockFetch.mock.resetCalls(); // Reset calls before each test

    mock.method(HashRing.prototype, "lookup", (nodeId) => "shard1");

    mockFetch.mock.mockImplementationOnce(async (request) => {
      assert.strictEqual(request.headers.get("X-Custom-Header"), null);
      return new Response("OK", { status: 200 });
    });

    const request = new Request("http://example.com/cluster/nodes/node1/action", {
      method: "POST",
      headers: {
        Authorization: "Bearer [REDACTED:auth_header]",
        "Content-Type": "application/json",
        "X-Custom-Header": "should-be-filtered",
      },
      body: JSON.stringify({ key: "value" }),
    });
    await worker.fetch(request, env);
  });
});
