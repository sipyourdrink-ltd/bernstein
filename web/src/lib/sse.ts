// useEventStream - SSE wrapper with auto-reconnect and per-event-type listeners.
import { useEffect, useRef } from 'react';
import { clearAuthToken, getAuthToken } from './api.ts';

type EventHandler = (data: unknown) => void;

export interface StreamEvent {
  type: string;
  data: unknown;
}

export interface OpenEventStreamOptions {
  onEvent: (event: StreamEvent) => void;
  onError?: (attempts: number) => void;
  backoffMs?: readonly number[];
  maxRetries?: number;
  /** Test seam; production uses the browser's fetch. */
  fetchImpl?: typeof fetch;
  /** Test seam; production reads the GUI bearer from localStorage. */
  getToken?: () => string | null;
  /** Test seam; production clears the GUI bearer after a refused connection. */
  clearToken?: () => void;
}

export interface EventStreamHandle {
  close: () => void;
}

const DEFAULT_BACKOFF_MS: readonly number[] = [1000, 2000, 5000, 15000];
/**
 * Hard cap on consecutive reconnect attempts before giving up. Prevents
 * infinite spin against a 404/500 endpoint. Surfaced via `onError`.
 */
const DEFAULT_MAX_RETRIES = 8;

function parseSseRecord(record: string): StreamEvent | null {
  let type = 'message';
  const data: string[] = [];
  for (const line of record.split(/\r?\n/)) {
    if (!line || line.startsWith(':')) continue;
    const separator = line.indexOf(':');
    const field = separator === -1 ? line : line.slice(0, separator);
    const value = separator === -1 ? '' : line.slice(separator + 1).replace(/^ /, '');
    if (field === 'event') type = value;
    if (field === 'data') data.push(value);
  }
  if (data.length === 0) return null;
  const text = data.join('\n');
  try {
    return { type, data: JSON.parse(text) };
  } catch {
    return { type, data: text };
  }
}

/**
 * Opens an authenticated SSE connection and returns a synchronous cleanup
 * handle suitable for React effect teardown. The stream is intentionally
 * fetch-based: browser EventSource cannot send the GUI bearer header.
 */
export function openEventStream(url: string, options: OpenEventStreamOptions): EventStreamHandle {
  const fetchImpl = options.fetchImpl ?? fetch;
  const getToken = options.getToken ?? getAuthToken;
  const clearToken = options.clearToken ?? clearAuthToken;
  const backoff = options.backoffMs?.length ? options.backoffMs : DEFAULT_BACKOFF_MS;
  const maxRetries = options.maxRetries ?? DEFAULT_MAX_RETRIES;
  const controller = new AbortController();
  let reader: ReadableStreamDefaultReader<Uint8Array> | null = null;
  let retry = 0;
  let cancelled = false;
  let timer: ReturnType<typeof setTimeout> | null = null;

  const scheduleReconnect = () => {
    if (cancelled) return;
    if (retry >= maxRetries) {
      queueMicrotask(() => options.onError?.(retry));
      return;
    }
    const wait = backoff[Math.min(retry, backoff.length - 1)];
    retry += 1;
    timer = setTimeout(() => {
      timer = null;
      void connect();
    }, wait);
  };

  const consume = async (body: ReadableStream<Uint8Array>) => {
    reader = body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    try {
      while (!cancelled) {
        const part = await reader.read();
        if (part.done) break;
        buffer += decoder.decode(part.value, { stream: true });
        let boundary = buffer.search(/\r?\n\r?\n/);
        while (boundary >= 0) {
          const record = buffer.slice(0, boundary);
          const terminatorLength = buffer.startsWith('\r\n', boundary) ? 4 : 2;
          buffer = buffer.slice(boundary + terminatorLength);
          const event = parseSseRecord(record);
          if (event) {
            retry = 0;
            options.onEvent(event);
          }
          if (cancelled) return;
          boundary = buffer.search(/\r?\n\r?\n/);
        }
      }
    } finally {
      reader.releaseLock();
      reader = null;
    }
  };

  const connect = async () => {
    try {
      const token = getToken();
      const response = await fetchImpl(url, {
        headers: {
          Accept: 'text/event-stream',
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        signal: controller.signal,
      });
      if (cancelled) return;
      if (response.status === 401) {
        clearToken();
        options.onError?.(0);
        return;
      }
      if (!response.ok || !response.body) throw new Error(`SSE connection failed with ${response.status}`);
      await consume(response.body);
      // Once a 200 response has started, HTTP cannot change to a 401 mid-stream.
      // A later reader failure is therefore treated as a transient disconnect.
      if (!cancelled) scheduleReconnect();
    } catch (error) {
      if (cancelled || (error instanceof DOMException && error.name === 'AbortError')) return;
      scheduleReconnect();
    }
  };

  void connect();
  return {
    close: () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
      controller.abort();
      if (reader) void reader.cancel().catch(() => undefined);
    },
  };
}

export interface UseEventStreamOptions {
  /** Map of event-type → handler. Use 'message' for unnamed default events. */
  on: Record<string, EventHandler>;
  /** Disable when false (e.g. waiting on auth). Default: true. */
  enabled?: boolean;
  /** Backoff schedule in ms; cycles. Defaults to [1s, 2s, 5s, 15s]. */
  backoffMs?: readonly number[];
  /** Max consecutive reconnect attempts before giving up. Default: 8. */
  maxRetries?: number;
  /** Invoked once retries are exhausted; receives the count of attempts made. */
  onError?: (attempts: number) => void;
}

/**
 * Subscribes to an SSE endpoint with auto-reconnect, capped retries, and
 * per-event-type dispatch. Handlers map and onError callback are read via
 * refs, so callers may pass inline objects without forcing reconnects.
 *
 * The lower-level stream seam owns framing, reconnection, and reader cleanup;
 * this hook only keeps React callback identity out of the reconnect boundary.
 */
export function useEventStream(url: string, opts: UseEventStreamOptions): void {
  const enabled = opts.enabled !== false;
  // Stash callable parts in refs so prop identity changes (inline objects /
  // arrow functions) don't tear down the connection.
  const handlersRef = useRef(opts.on);
  handlersRef.current = opts.on;
  const onErrorRef = useRef(opts.onError);
  onErrorRef.current = opts.onError;
  const backoffRef = useRef<readonly number[]>(opts.backoffMs ?? DEFAULT_BACKOFF_MS);
  backoffRef.current = opts.backoffMs ?? DEFAULT_BACKOFF_MS;
  const maxRetriesRef = useRef<number>(opts.maxRetries ?? DEFAULT_MAX_RETRIES);
  maxRetriesRef.current = opts.maxRetries ?? DEFAULT_MAX_RETRIES;

  useEffect(() => {
    if (!enabled || !url) return;
    const stream = openEventStream(url, {
      onEvent: ({ type, data }) => handlersRef.current[type]?.(data),
      onError: (attempts) => onErrorRef.current?.(attempts),
      backoffMs: backoffRef.current,
      maxRetries: maxRetriesRef.current,
    });
    return () => {
      stream.close();
    };
    // Intentionally only depend on the two values that should force a fresh
    // connection. backoff/maxRetries/onError/handlers flow through refs above.
  }, [url, enabled]);
}
