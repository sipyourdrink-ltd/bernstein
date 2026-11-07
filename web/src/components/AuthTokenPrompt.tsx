// Shell-level prompt for the API access token.
//
// When the server refuses the stored token (or none is stored), every screen's
// query fails with the same 401 and api.ts drops the token, so a screen's own
// Retry cannot succeed. api.ts signals UNAUTHORIZED_EVENT on that 401; this
// prompt is the one place that reacts to it, and it closes on AUTHORIZED_EVENT,
// when a request carrying the stored token succeeds.

import { useEffect, useId, useState, type FormEvent } from 'react';
import { Link } from 'react-router';
import { useQueryClient } from '@tanstack/react-query';
import { AUTHORIZED_EVENT, UNAUTHORIZED_EVENT, setAuthToken } from '@/lib/api';

// hidden: no refusal since the last accepted request.
// missing: a 401 arrived; ask for a token.
// saved: a token was saved here and requests are being retried with it.
// rejected: the token saved here was refused as well.
type PromptState = 'hidden' | 'missing' | 'saved' | 'rejected';

const DETAIL: Record<Exclude<PromptState, 'hidden'>, string> = {
  missing:
    "The server refused this browser's token, so no data can load. Paste the BERNSTEIN_AUTH_TOKEN the server was started with.",
  saved: 'Token saved. Retrying requests…',
  rejected:
    'The server rejected that token too. Paste the BERNSTEIN_AUTH_TOKEN the server was started with.',
};

export function AuthTokenPrompt() {
  const queryClient = useQueryClient();
  const inputId = useId();
  const [state, setState] = useState<PromptState>('hidden');
  const [value, setValue] = useState('');

  useEffect(() => {
    const onUnauthorized = () =>
      setState((s) => (s === 'saved' ? 'rejected' : s === 'hidden' ? 'missing' : s));
    const onAuthorized = () => setState('hidden');
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    window.addEventListener(AUTHORIZED_EVENT, onAuthorized);
    return () => {
      window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
      window.removeEventListener(AUTHORIZED_EVENT, onAuthorized);
    };
  }, []);

  if (state === 'hidden') return null;

  const onSubmit = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const token = value.trim();
    if (!token) return;
    setAuthToken(token);
    setValue('');
    setState('saved');
    // Every query that failed under the refused token runs again with this
    // one; the first success closes the prompt via AUTHORIZED_EVENT.
    void queryClient.invalidateQueries();
  };

  return (
    <div role="alert" className="shrink-0 border-b border-warning/40 bg-warning/10 px-5 py-3">
      <form onSubmit={onSubmit} className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <div className="min-w-0 flex-1">
          <div className="text-[13px] font-medium text-foreground">
            Access token missing or rejected
          </div>
          <p className="text-[12px] text-muted-foreground">{DETAIL[state]}</p>
        </div>
        <label htmlFor={inputId} className="sr-only">
          Access token
        </label>
        <input
          id={inputId}
          type="password"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="paste token…"
          spellCheck={false}
          autoComplete="off"
          className="w-64 rounded-md border border-border bg-background px-3 py-1.5 font-mono text-[12.5px] text-foreground outline-none focus:border-foreground"
        />
        <button
          type="submit"
          disabled={!value.trim()}
          className="rounded-md border border-foreground bg-foreground px-3 py-1.5 text-[12.5px] font-medium text-background transition-colors hover:bg-foreground/90 disabled:opacity-50"
        >
          Save
        </button>
        <Link
          to="/settings"
          className="text-[12.5px] text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
        >
          Settings
        </Link>
      </form>
    </div>
  );
}
