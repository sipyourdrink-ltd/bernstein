// React Query hook that loads the per-task diff. The cache key is bound to
// the task id; the query is disabled until the Diff tab actually mounts to
// avoid wasting bandwidth on every drawer open.

import { useQuery } from '@tanstack/react-query';

import { apiGet } from '@/lib/api';

import type { TaskDiffResponse } from './types';

export interface UseTaskDiffOptions {
  taskId: string;
  enabled: boolean;
}

export function useTaskDiff({ taskId, enabled }: UseTaskDiffOptions) {
  return useQuery<TaskDiffResponse>({
    queryKey: ['task-diff', taskId],
    queryFn: () =>
      apiGet<TaskDiffResponse>(
        `/tasks/${encodeURIComponent(taskId)}/diff`,
      ),
    enabled: enabled && taskId !== '',
    // The diff doesn't update on its own - refetching is operator-driven via
    // the Refresh button. Stale immediately so a refetch always hits the
    // server.
    staleTime: 0,
    refetchOnWindowFocus: false,
    refetchOnMount: 'always',
    retry: 1,
  });
}
