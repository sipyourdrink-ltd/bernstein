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
    const heartbeatsToFlush = [...this.buffer];
    this.buffer = []; // Clear buffer immediately
    
    try {
      const response = await fetch(`${this.env.SHARD_URL}/cluster/nodes/heartbeats`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ heartbeats: heartbeatsToFlush }),
      });

      if (response.ok) {
        this.backoff_delay = 1000; // Reset backoff on success
        const result = await response.json();
        if (result.unknown && result.unknown.length > 0) {
          result.unknown.forEach((id) => this.unknown_ids.add(id));
        }
      } else {
        console.error("Failed to flush heartbeats:", response.status, response.statusText);
        // Re-add to buffer and retry with exponential backoff
        this.buffer.unshift(...heartbeatsToFlush); // Add back to front of buffer
        this.scheduleRetry();
      }
    } catch (error) {
      console.error("Error flushing heartbeats:", error);
      // Re-add to buffer and retry with exponential backoff
      this.buffer.unshift(...heartbeatsToFlush); // Add back to front of buffer
      this.scheduleRetry();
    } finally {
      this.flushing = false;
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
