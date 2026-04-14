/**
 * Bernstein MCP server on Cloudflare Workers.
 *
 * Uses the Cloudflare Agents SDK with McpAgent base class to expose
 * Bernstein orchestration tools over streamable HTTP transport.
 */
import { McpAgent } from "agents/mcp";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";

type Env = {
  MCP_AGENT: DurableObjectNamespace;
  BERNSTEIN_SERVER_URL: string;
  BEARER_TOKEN?: string;
};

/** Proxy a GET request to the Bernstein task server. */
async function proxyGet(
  serverUrl: string,
  path: string,
  params?: Record<string, string>
): Promise<unknown> {
  const url = new URL(path, serverUrl);
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      url.searchParams.set(k, v);
    }
  }
  const resp = await fetch(url.toString());
  return resp.json();
}

/** Proxy a POST request to the Bernstein task server. */
async function proxyPost(
  serverUrl: string,
  path: string,
  body: unknown
): Promise<unknown> {
  const resp = await fetch(new URL(path, serverUrl).toString(), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return resp.json();
}

export class BernsteinMcpAgent extends McpAgent<Env, {}, {}> {
  server = new McpServer({
    name: "bernstein",
    version: "1.0.0",
  });

  async init() {
    const serverUrl =
      this.env.BERNSTEIN_SERVER_URL || "http://127.0.0.1:8052";

    this.server.tool("bernstein_health", "Liveness check", {}, async () => ({
      content: [{ type: "text", text: JSON.stringify({ status: "ok" }) }],
    }));

    this.server.tool(
      "bernstein_run",
      "Start an orchestration run by posting a task",
      {
        goal: z.string(),
        role: z.string().default("backend"),
        priority: z.number().default(2),
        scope: z.string().default("medium"),
        complexity: z.string().default("medium"),
        estimated_minutes: z.number().default(30),
      },
      async ({ goal, role, priority, scope, complexity, estimated_minutes }) => {
        const data = await proxyPost(serverUrl, "/tasks", {
          title: goal.slice(0, 120),
          description: goal,
          role,
          priority,
          scope,
          complexity,
          estimated_minutes,
        });
        return { content: [{ type: "text" as const, text: JSON.stringify(data) }] };
      }
    );

    this.server.tool(
      "bernstein_status",
      "Return task counts summary",
      {},
      async () => {
        const data = await proxyGet(serverUrl, "/status");
        return { content: [{ type: "text" as const, text: JSON.stringify(data) }] };
      }
    );

    this.server.tool(
      "bernstein_tasks",
      "List tasks with optional status filter",
      { status: z.string().optional() },
      async ({ status }) => {
        const params = status ? { status } : undefined;
        const data = await proxyGet(serverUrl, "/tasks", params);
        return { content: [{ type: "text" as const, text: JSON.stringify(data) }] };
      }
    );

    this.server.tool(
      "bernstein_cost",
      "Return cost summary",
      {},
      async () => {
        const data = (await proxyGet(serverUrl, "/status")) as Record<
          string,
          unknown
        >;
        const perRole = (data.per_role as Array<Record<string, unknown>>) || [];
        const summary = {
          total_cost_usd: data.total_cost_usd || 0,
          per_role: perRole.map((r) => ({
            role: r.role,
            cost_usd: r.cost_usd || 0,
          })),
        };
        return {
          content: [{ type: "text" as const, text: JSON.stringify(summary) }],
        };
      }
    );

    this.server.tool(
      "bernstein_approve",
      "Approve a pending/blocked task",
      {
        task_id: z.string(),
        note: z.string().default("Approved via MCP"),
      },
      async ({ task_id, note }) => {
        const data = await proxyPost(serverUrl, `/tasks/${task_id}/complete`, {
          result_summary: note,
        });
        return { content: [{ type: "text" as const, text: JSON.stringify(data) }] };
      }
    );

    this.server.tool(
      "bernstein_create_subtask",
      "Create a subtask linked to a parent task",
      {
        parent_task_id: z.string(),
        goal: z.string(),
        role: z.string().default("auto"),
        priority: z.number().default(2),
        scope: z.string().default("medium"),
        complexity: z.string().default("medium"),
        estimated_minutes: z.number().optional(),
      },
      async (args) => {
        const payload: Record<string, unknown> = {
          parent_task_id: args.parent_task_id,
          title: args.goal.slice(0, 120),
          description: args.goal,
          role: args.role,
          priority: args.priority,
          scope: args.scope,
          complexity: args.complexity,
        };
        if (args.estimated_minutes !== undefined) {
          payload.estimated_minutes = args.estimated_minutes;
        }
        const data = await proxyPost(serverUrl, "/tasks/self-create", payload);
        return { content: [{ type: "text" as const, text: JSON.stringify(data) }] };
      }
    );
  }
}

export default {
  fetch(request: Request, env: Env, ctx: ExecutionContext) {
    const url = new URL(request.url);

    // Optional bearer token auth.
    if (env.BEARER_TOKEN) {
      const auth = request.headers.get("Authorization") || "";
      if (auth !== `Bearer ${env.BEARER_TOKEN}`) {
        return new Response("Unauthorized", { status: 401 });
      }
    }

    if (url.pathname === "/mcp" || url.pathname === "/mcp/") {
      // Route to the Durable Object.
      const id = env.MCP_AGENT.idFromName("default");
      const stub = env.MCP_AGENT.get(id);
      return stub.fetch(request);
    }

    return new Response("Not Found", { status: 404 });
  },
} satisfies ExportedHandler<Env>;
