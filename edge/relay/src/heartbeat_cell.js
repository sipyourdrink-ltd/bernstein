export class HeartbeatCell {
  constructor(state, env) {
    this.state = state;
    this.env = env;
    this.buffer = [];
    this.timer = null;
    this.unknown_ids = new Set();
    this.flushing = false; 
    this.backoff_delay = 1000; // Initial backoff delay for retries
    this.max_backoff_delay = 60000; // Max backoff delay
    this.max_batch = 500;
    this.max_buffer = 10000;
  }

  async fetch(request) {
    const url = new URL(request.url);

    switch (url.pathname) {
      case "/buffer":
        if (request.method !== "POST") {
          return new Response("Method Not Allowed", { status: 405 });
        }
        return this.handleBuffer(request);
      case "/status":
        if (request.method !== "GET") {
          return new Response("Method Not Allowed", { status: 405 });
        }
        return this.handleStatus();
      default:
        return new Response("Not Found", { status: 404 });
    }
  }

  async handleBuffer(request) {
    const heartbeat = await request.json();
    if (this.unknown_ids.has(heartbeat.node_id)) {
      this.unknown_ids.delete(heartbeat.node_id);
      return new Response("Node ID unknown", { status: 404 });
    }
    this.buffer.push(heartbeat);

    if (this.buffer.length >= 200) {
      this.state.waitUntil(this.flush());
    } else if (!this.timer) {
      this.timer = setTimeout(() => {
        this.timer = null;
        this.flush();
      }, this.env.FLUSH_MS || 2000);
    }

    return new Response("Accepted", { status: 202 });
  }

  async handleStatus() {
    return new Response(JSON.stringify({ buffered: this.buffer.length }), {
      headers: { "Content-Type": "application/json" },
    });
  }

  async flush() {
    if (this.flushing || this.buffer.length === 0) {
      return;
    }

    this.flushing = true;
    const heartbeatsToFlush = this.buffer.splice(0, this.max_batch);
    const headers = { "Content-Type": "application/json" };
    if (this.env.CLUSTER_TOKEN) {
      headers.Authorization = `Bearer ${this.env.CLUSTER_TOKEN}`;
    }

    let retry = false;
    try {
      const response = await fetch(`${this.env.SHARD_URL}/cluster/nodes/heartbeats`, {
        method: "POST",
        headers,
        body: JSON.stringify({ heartbeats: heartbeatsToFlush }),
      });

      if (response.ok) {
        this.backoff_delay = 1000;
        const result = await response.json();
        if (result.unknown && result.unknown.length > 0) {
          result.unknown.forEach((id) => this.unknown_ids.add(id));
        }
      } else if (response.status >= 500 || response.status === 429 || response.status === 408) {
        console.error("Failed to flush heartbeats:", response.status, response.statusText);
        retry = true;
      } else {
        console.error("Dropping heartbeat batch, central rejected it:", response.status);
      }
    } catch (error) {
      console.error("Error flushing heartbeats:", error);
      retry = true;
    } finally {
      this.flushing = false;
    }

    if (retry) {
      this.buffer.unshift(...heartbeatsToFlush);
      if (this.buffer.length > this.max_buffer) {
        this.buffer.splice(0, this.buffer.length - this.max_buffer);
      }
      this.scheduleRetry();
    } else if (this.buffer.length > 0) {
      await this.flush();
    }
  }

  scheduleRetry() {
    if (this.timer) {
        clearTimeout(this.timer);
    }
    this.timer = setTimeout(async () => {
        this.timer = null;
        await this.flush();
    }, this.backoff_delay);
    this.backoff_delay = Math.min(this.backoff_delay * 2, this.max_backoff_delay);
  }
}
