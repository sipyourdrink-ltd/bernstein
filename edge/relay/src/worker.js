import { createHash } from "node:crypto";
import { HashRing } from "./ring.js";

const SHARD_VIRTUAL_NODES = 100;

export default {
  async fetch(request, env) {
    let shardMap;
    try {
      shardMap = JSON.parse(env.SHARD_MAP || "[]");
    } catch (e) {
      console.error("Failed to parse SHARD_MAP:", e);
      shardMap = [];
    }

    const shardIds = shardMap.map((shard) => shard.id);
    const hashRing = new HashRing(shardIds, SHARD_VIRTUAL_NODES);

    const url = new URL(request.url);

    // Route POST /cluster/nodes (registration)
    if (url.pathname === "/cluster/nodes" && request.method === "POST") {
      try {
        const requestBody = await request.json(); // Read body once
        if (!requestBody.name || !requestBody.url) {
          return new Response("Bad Request: name and url required", { status: 400 });
        }
        const nodeId = createHash("sha256")
          .update(`${requestBody.name}|${requestBody.url}`, "utf8")
          .digest("hex")
          .slice(0, 12);
        const targetShardId = hashRing.lookup(nodeId);
        const targetShard = shardMap.find((shard) => shard.id === targetShardId);

        if (!targetShard) {
          return new Response("Internal Server Error: Target shard not found", { status: 500 });
        }

        const targetUrl = new URL(url.pathname, targetShard.url);
        const newRequest = new Request(targetUrl, {
          method: request.method,
          headers: request.headers,
          body: JSON.stringify(requestBody), // Re-serialize the body
          duplex: "half", // Required when body is a ReadableStream
        });

        const response = await fetch(newRequest);
        if (response.status === 421) {
          return response;
        }
        return response;
      } catch (e) {
        console.error("Error routing registration request:", e);
        return new Response("Internal Server Error", { status: 500 });
      }
    }

    // Route GET/POST /cluster/nodes/{node_id}/*
    const match = url.pathname.match(/^\/cluster\/nodes\/([^/]+)(.*)$/);
    if (match) {
      const nodeId = match[1];
      const restOfPath = match[2];
      const targetShardId = hashRing.lookup(nodeId);
      const targetShard = shardMap.find((shard) => shard.id === targetShardId);

      if (!targetShard) {
        return new Response("Internal Server Error: Target shard not found", { status: 500 });
      }

      const targetUrl = new URL(`/cluster/nodes/${nodeId}${restOfPath}`, targetShard.url);
      
      const newHeaders = new Headers();
      // Forward Authorization and Content-Type headers
      const allowedHeaders = ["authorization", "content-type"];
      for (const [key, value] of request.headers.entries()) {
        if (allowedHeaders.includes(key.toLowerCase())) {
          newHeaders.append(key, value);
        }
      }

      const newRequest = new Request(targetUrl, {
        method: request.method,
        headers: newHeaders,
        body: request.body,
        duplex: "half",  // Required when body is a ReadableStream
      });

      const response = await fetch(newRequest);
      if (response.status === 421) {
        return response;
      }
      return response;
    }

    // Return 404 for unknown paths
    return new Response("Not Found", { status: 404 });
  },
};
