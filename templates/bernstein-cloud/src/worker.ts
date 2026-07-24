/**
 * Minimal Bernstein agent worker.
 *
 * Free-tier compatible: a single fetch handler with no paid bindings. This file
 * is the `main` entry point named in wrangler.toml, so `wrangler deploy`
 * resolves without an "entry-point not found" error. Replace the body with your
 * agent logic and re-enable bindings in wrangler.toml as needed.
 */

interface Env {
  BERNSTEIN_ENV: string;
  MAX_AGENTS: string;
  DEFAULT_MODEL: string;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const body = {
      service: "bernstein-agent",
      env: env.BERNSTEIN_ENV ?? "development",
      max_agents: env.MAX_AGENTS ?? "3",
      default_model: env.DEFAULT_MODEL ?? "auto",
      message: "Bernstein agent worker is running.",
    };
    return new Response(JSON.stringify(body), {
      headers: { "content-type": "application/json" },
    });
  },
};
