/**
 * GitHub Actions adapter — create Bernstein tasks from CI failures.
 *
 * @example
 * ```ts
 * // In a GitHub Actions workflow step (if: failure()):
 * import { CITaskFactory } from '@bernstein/sdk/adapters/github-actions';
 * import { BernsteinClient } from '@bernstein/sdk';
 *
 * const factory = CITaskFactory.fromEnv();
 * const client = new BernsteinClient({ baseUrl: process.env.BERNSTEIN_URL });
 * const task = await client.createTask(factory.taskFromEnv());
 * console.log('Created task', task.id);
 * ```
 */

import type { TaskCreate } from '../models.js';

export interface CIRunInfo {
  workflowName: string;
  runId: string;
  repository: string;
  branch: string;
  commitSha: string;
  conclusion: string;
  runUrl: string;
}

const CONCLUSION_PRIORITY: Record<string, 1 | 2 | 3> = {
  failure: 1,
  action_required: 1,
  timed_out: 2,
  cancelled: 3,
};

export class CITaskFactory {
  private readonly defaultRole: string;
  private readonly conclusionPriority: Record<string, 1 | 2 | 3>;

  constructor(options: {
    defaultRole?: string;
    conclusionPriority?: Record<string, 1 | 2 | 3>;
  } = {}) {
    this.defaultRole = options.defaultRole ?? 'qa';
    this.conclusionPriority = options.conclusionPriority ?? CONCLUSION_PRIORITY;
  }

  /** Build a factory from GitHub Actions environment variables. */
  static fromEnv(options?: { defaultRole?: string }): CITaskFactory {
    return new CITaskFactory(options);
  }

  /**
   * Parse a `workflow_run` webhook payload and return a {@link TaskCreate},
   * or `null` if the run did not fail.
   */
  taskFromWorkflowWebhook(payload: Record<string, unknown>): TaskCreate | null {
    const run = payload['workflow_run'] as Record<string, unknown> | undefined;
    if (!run) return null;
    const conclusion = String(run['conclusion'] ?? '');
    if (!Object.keys(this.conclusionPriority).includes(conclusion)) return null;

    const repo = (payload['repository'] as Record<string, unknown> | undefined) ?? {};
    const runInfo: CIRunInfo = {
      workflowName: String(run['name'] ?? 'CI'),
      runId: String(run['id'] ?? ''),
      repository: String(repo['full_name'] ?? ''),
      branch: String(run['head_branch'] ?? ''),
      commitSha: String(run['head_sha'] ?? ''),
      conclusion,
      runUrl: String(run['html_url'] ?? ''),
    };
    return this.taskFromRun(runInfo);
  }

  /**
   * Parse a `check_run` webhook payload and return a {@link TaskCreate},
   * or `null` if the check did not fail.
   */
  taskFromCheckRunWebhook(payload: Record<string, unknown>): TaskCreate | null {
    const checkRun = payload['check_run'] as Record<string, unknown> | undefined;
    if (!checkRun) return null;
    const conclusion = String(checkRun['conclusion'] ?? '');
    if (!Object.keys(this.conclusionPriority).includes(conclusion)) return null;

    const repo = (payload['repository'] as Record<string, unknown> | undefined) ?? {};
    const suite = (checkRun['check_suite'] as Record<string, unknown> | undefined) ?? {};
    const runInfo: CIRunInfo = {
      workflowName: String(checkRun['name'] ?? 'check'),
      runId: String(checkRun['id'] ?? ''),
      repository: String(repo['full_name'] ?? ''),
      branch: String(suite['head_branch'] ?? ''),
      commitSha: String(checkRun['head_sha'] ?? ''),
      conclusion,
      runUrl: String(checkRun['html_url'] ?? ''),
    };
    return this.taskFromRun(runInfo);
  }

  /**
   * Create a {@link TaskCreate} from the current GitHub Actions environment.
   *
   * Call from a step with `if: failure()`.
   */
  taskFromEnv(): TaskCreate {
    const repo = process.env['GITHUB_REPOSITORY'] ?? '';
    const runId = process.env['GITHUB_RUN_ID'] ?? '';
    const serverUrl = process.env['GITHUB_SERVER_URL'] ?? 'https://github.com';
    const runUrl = repo && runId ? `${serverUrl}/${repo}/actions/runs/${runId}` : '';
    const branch = (process.env['GITHUB_REF'] ?? '').replace(/^refs\/heads\//, '');

    return this.taskFromRun({
      workflowName: process.env['GITHUB_WORKFLOW'] ?? 'CI',
      runId,
      repository: repo,
      branch,
      commitSha: process.env['GITHUB_SHA'] ?? '',
      conclusion: 'failure',
      runUrl,
    });
  }

  /** Convert a {@link CIRunInfo} to a {@link TaskCreate}. */
  taskFromRun(run: CIRunInfo): TaskCreate {
    const priority: 1 | 2 | 3 = this.conclusionPriority[run.conclusion] ?? 1;
    const conclusionLabel = run.conclusion.replace(/_/g, ' ');
    const shortSha = run.commitSha.slice(0, 7);
    const branchName = run.branch.replace(/^refs\/heads\//, '');

    const descParts = [
      `Workflow **${run.workflowName}** ${conclusionLabel} on branch \`${branchName}\`.`,
      `Commit: \`${run.commitSha}\``,
      `Repository: \`${run.repository}\``,
    ];
    if (run.runUrl) descParts.push(`Run: ${run.runUrl}`);

    return {
      title: `Fix CI ${conclusionLabel}: ${run.workflowName} on ${branchName} (${shortSha})`,
      role: this.defaultRole,
      description: descParts.join('\n'),
      priority,
      scope: 'small',
      complexity: 'medium',
      external_ref: `github_actions:${run.repository}/${run.runId}`,
      metadata: {
        ci_provider: 'github_actions',
        workflow: run.workflowName,
        run_id: run.runId,
        repository: run.repository,
        branch: branchName,
        commit: run.commitSha,
        conclusion: run.conclusion,
        run_url: run.runUrl,
      },
    };
  }
}
