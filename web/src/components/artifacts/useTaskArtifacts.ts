// Polling fetch for ``/tasks/{id}/artifacts`` + ``/tasks/{id}/progress`` backed
// by react-query, refreshed live by the ``task.artifact`` / ``task.progress``
// SSE events so a posted artifact appears without a manual reload (#2553 AC1).

import { useQueryClient, useQuery } from '@tanstack/react-query';
import { useCallback } from 'react';

import { apiGet, ApiError } from '@/lib/api';
import { useEventStream } from '@/lib/sse';

import type { ArtifactView, ProgressVector } from './types';

const ACTIVE_POLL_MS = 8_000;

export interface UseTaskArtifactsOptions {
  taskId: string;
  /** When false the queries are disabled (e.g. the tab isn't visible). */
  enabled?: boolean;
}

export interface UseTaskArtifactsResult {
  artifacts: ArtifactView[];
  progress: ProgressVector | null;
  initialLoading: boolean;
  isRefetching: boolean;
  error: Error | null;
  refetch: () => Promise<unknown>;
}

export function useTaskArtifacts({ taskId, enabled = true }: UseTaskArtifactsOptions): UseTaskArtifactsResult {
  const client = useQueryClient();
  const on = taskId.length > 0 && enabled;

  const artifactsQuery = useQuery<ArtifactView[], Error>({
    queryKey: ['task-artifacts', taskId],
    enabled: on,
    queryFn: async () => {
      try {
        return await apiGet<ArtifactView[]>(`/tasks/${encodeURIComponent(taskId)}/artifacts`);
      } catch (err) {
        if (err instanceof ApiError && err.status === 404) return [];
        throw err;
      }
    },
    refetchInterval: ACTIVE_POLL_MS,
    staleTime: 2_000,
    placeholderData: (prev) => prev,
  });

  const progressQuery = useQuery<ProgressVector | null, Error>({
    queryKey: ['task-progress', taskId],
    enabled: on,
    queryFn: async () => {
      try {
        return await apiGet<ProgressVector>(`/tasks/${encodeURIComponent(taskId)}/progress`);
      } catch (err) {
        if (err instanceof ApiError && err.status === 404) return null;
        throw err;
      }
    },
    refetchInterval: ACTIVE_POLL_MS,
    staleTime: 2_000,
    placeholderData: (prev) => prev,
  });

  // Live refresh: a matching SSE event invalidates the queries so the panel
  // reflects the newly posted artifact / advanced progress at once.
  const refreshOnEvent = useCallback(() => {
    void client.invalidateQueries({ queryKey: ['task-artifacts', taskId] });
    void client.invalidateQueries({ queryKey: ['task-progress', taskId] });
  }, [client, taskId]);

  useEventStream('/api/v1/events', {
    enabled: on,
    on: {
      'task.artifact': refreshOnEvent,
      'task.progress': refreshOnEvent,
    },
  });

  return {
    artifacts: artifactsQuery.data ?? [],
    progress: progressQuery.data ?? null,
    initialLoading: artifactsQuery.isLoading && !artifactsQuery.data,
    isRefetching: artifactsQuery.isFetching && !artifactsQuery.isLoading,
    error: artifactsQuery.error ?? progressQuery.error,
    refetch: artifactsQuery.refetch,
  };
}
