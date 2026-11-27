import { useQuery } from '@tanstack/react-query';
import { apiGet } from '@/lib/api';

export type GuiMeta = {
  version: string;
  commit: string;
  build_time: string;
  runner_active?: boolean;
};

export function useGuiMeta(enabled = true) {
  return useQuery({
    queryKey: ['gui-meta'],
    queryFn: () => apiGet<GuiMeta>('/gui-meta'),
    enabled,
    retry: 3,
    retryDelay: (attempt) => Math.min(1000 * 2 ** attempt, 30_000),
    staleTime: 15_000,
    refetchInterval: 30_000,
  });
}

export function formatGuiMetaLabel(query: {
  data?: GuiMeta;
  isFetching: boolean;
  isFetched: boolean;
  isError: boolean;
}): string {
  if (query.data) {
    const commit = query.data.commit ? ` · ${query.data.commit.slice(0, 7)}` : '';
    return `v${query.data.version}${commit}`;
  }
  // While the very first fetch is still in flight show "connecting…"; once
  // every retry is exhausted, fall back to "version unknown" so the chip
  // doesn't pretend to still be working.
  if (query.isFetching && !query.isFetched) return 'connecting…';
  if (query.isError) return 'version unknown';
  return 'connecting…';
}
