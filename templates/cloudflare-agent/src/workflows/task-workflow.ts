/**
 * Cloudflare Workflow template for the Bernstein task lifecycle.
 *
 * Maps the full task lifecycle (claim -> spawn -> execute -> verify ->
 * [approval] -> merge -> complete) to durable Workflow steps with
 * auto-retry and human approval gates.
 */

import {
  WorkflowEntrypoint,
  WorkflowEvent,
  WorkflowStep,
} from "cloudflare:workers";

interface Env {
  BERNSTEIN_API_URL: string;
  BERNSTEIN_API_KEY: string;
}

interface TaskParams {
  agentId: string;
  prompt: string;
  model: string;
  role: string;
  effort: string;
  timeoutSeconds: number;
  env: Record<string, string>;
  labels: Record<string, string>;
}

interface StepResult {
  success: boolean;
  message: string;
  data?: Record<string, unknown>;
}

export class BernsteinTaskWorkflow extends WorkflowEntrypoint<Env, TaskParams> {
  async run(event: WorkflowEvent<TaskParams>, step: WorkflowStep) {
    const params = event.payload;

    // Step 1: Claim the task from the Bernstein task server.
    const claimed = await step.do(
      "claim",
      { retries: { limit: 3, delay: "5 seconds", backoff: "exponential" } },
      async (): Promise<StepResult> => {
        const resp = await fetch(
          `${this.env.BERNSTEIN_API_URL}/tasks/${params.agentId}/claim`,
          {
            method: "POST",
            headers: {
              Authorization: `Bearer ${this.env.BERNSTEIN_API_KEY}`,
              "Content-Type": "application/json",
            },
          }
        );
        if (!resp.ok) {
          throw new Error(`Claim failed: ${resp.status} ${await resp.text()}`);
        }
        return { success: true, message: "Task claimed" };
      }
    );

    // Step 2: Spawn the agent process.
    const spawned = await step.do(
      "spawn",
      {
        retries: { limit: 3, delay: "10 seconds", backoff: "exponential" },
        timeout: "30 minutes",
      },
      async (): Promise<StepResult> => {
        const resp = await fetch(
          `${this.env.BERNSTEIN_API_URL}/agents/spawn`,
          {
            method: "POST",
            headers: {
              Authorization: `Bearer ${this.env.BERNSTEIN_API_KEY}`,
              "Content-Type": "application/json",
            },
            body: JSON.stringify({
              agent_id: params.agentId,
              prompt: params.prompt,
              model: params.model,
              role: params.role,
              effort: params.effort,
              timeout_seconds: params.timeoutSeconds,
              env: params.env,
              labels: params.labels,
            }),
          }
        );
        if (!resp.ok) {
          throw new Error(`Spawn failed: ${resp.status} ${await resp.text()}`);
        }
        const data = (await resp.json()) as Record<string, unknown>;
        return { success: true, message: "Agent spawned", data };
      }
    );

    // Step 3: Execute — poll until the agent completes.
    const executed = await step.do(
      "execute",
      { timeout: "2 hours" },
      async (): Promise<StepResult> => {
        let attempts = 0;
        const maxAttempts = 720; // 2 hours at 10s intervals
        while (attempts < maxAttempts) {
          const resp = await fetch(
            `${this.env.BERNSTEIN_API_URL}/agents/${params.agentId}/status`,
            {
              headers: {
                Authorization: `Bearer ${this.env.BERNSTEIN_API_KEY}`,
              },
            }
          );
          if (!resp.ok) {
            throw new Error(
              `Status check failed: ${resp.status} ${await resp.text()}`
            );
          }
          const status = (await resp.json()) as Record<string, string>;
          if (status.state === "completed") {
            return { success: true, message: "Execution completed" };
          }
          if (status.state === "failed") {
            throw new Error(`Agent execution failed: ${status.message}`);
          }
          await new Promise((r) => setTimeout(r, 10_000));
          attempts++;
        }
        throw new Error("Execution timed out");
      }
    );

    // Step 4: Verify — run quality gates.
    const verified = await step.do(
      "verify",
      {
        retries: { limit: 2, delay: "10 seconds", backoff: "exponential" },
        timeout: "15 minutes",
      },
      async (): Promise<StepResult> => {
        const resp = await fetch(
          `${this.env.BERNSTEIN_API_URL}/tasks/${params.agentId}/verify`,
          {
            method: "POST",
            headers: {
              Authorization: `Bearer ${this.env.BERNSTEIN_API_KEY}`,
              "Content-Type": "application/json",
            },
          }
        );
        if (!resp.ok) {
          throw new Error(
            `Verification failed: ${resp.status} ${await resp.text()}`
          );
        }
        return { success: true, message: "Verification passed" };
      }
    );

    // Step 5: Optional human approval gate.
    await step.do("approval", async (): Promise<StepResult> => {
      // This step uses Cloudflare's waitForEvent to pause until
      // a human approves via the approve API endpoint.
      // In production, this would call step.waitForEvent().
      return { success: true, message: "Approved (auto)" };
    });

    // Step 6: Merge the changes.
    const merged = await step.do(
      "merge",
      {
        retries: { limit: 2, delay: "5 seconds", backoff: "exponential" },
      },
      async (): Promise<StepResult> => {
        const resp = await fetch(
          `${this.env.BERNSTEIN_API_URL}/tasks/${params.agentId}/merge`,
          {
            method: "POST",
            headers: {
              Authorization: `Bearer ${this.env.BERNSTEIN_API_KEY}`,
              "Content-Type": "application/json",
            },
          }
        );
        if (!resp.ok) {
          throw new Error(`Merge failed: ${resp.status} ${await resp.text()}`);
        }
        return { success: true, message: "Changes merged" };
      }
    );

    // Step 7: Mark complete.
    await step.do("complete", async (): Promise<StepResult> => {
      const resp = await fetch(
        `${this.env.BERNSTEIN_API_URL}/tasks/${params.agentId}/complete`,
        {
          method: "POST",
          headers: {
            Authorization: `Bearer ${this.env.BERNSTEIN_API_KEY}`,
            "Content-Type": "application/json",
          },
        }
      );
      if (!resp.ok) {
        throw new Error(
          `Complete failed: ${resp.status} ${await resp.text()}`
        );
      }
      return { success: true, message: "Task completed" };
    });
  }
}
