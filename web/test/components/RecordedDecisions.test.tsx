// @vitest-environment happy-dom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { RecordedDecisions, type RecordedDecision } from '@/components/governance/RecordedDecisions';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); localStorage.clear(); });

const decision: RecordedDecision = {
  decision_id: 'record-1', run_id: 'run & 1', subject: 'alice', action: 'budget', verdict: 'refuse',
  inputs_hash: 'sha256:recorded-input', timestamp: 1000, context: { cap_usd: 10, next_cost_usd: 2 },
  journal_entry_hash: 'sha256:recorded-anchor', policy_version: 'this decision names no policy version',
  budget: {
    cap_usd: { value: 10, state: 'verified', provenance: 'recorded context.cap_usd (USD)', reason: 'Matched budget inputs.' },
    prior_spend_usd: { value: 9, state: 'verified', provenance: 'ledger via existing verifier', reason: 'Matched budget inputs.' },
    verification_scope: 'Budget input consistency only; journal anchor and signer are not verified by this read.',
  },
};

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(<QueryClientProvider client={client}><RecordedDecisions /></QueryClientProvider>);
}

function load(runId: string) {
  fireEvent.change(screen.getByLabelText('Run ID'), { target: { value: runId } });
  fireEvent.click(screen.getByRole('button', { name: 'Load decisions' }));
}

it('loads authenticated live rows, expands recorded inputs and retains the recorded verdict', async () => {
  let resolve!: (response: Response) => void;
  const fetch = vi.fn(() => new Promise<Response>((done) => { resolve = done; }));
  vi.stubGlobal('fetch', fetch);
  localStorage.setItem('bernstein_token', 'synthetic-test-token');
  mount();
  expect(fetch).not.toHaveBeenCalled();
  expect(screen.queryByText(/alice/)).toBeNull();
  load('run & 1');
  await screen.findByText('Loading recorded decisions...');
  expect(fetch).toHaveBeenCalledWith('/api/v1/governance/decisions?run_id=run%20%26%201', expect.objectContaining({
    method: 'GET', headers: expect.objectContaining({ Authorization: 'Bearer synthetic-test-token' }),
  }));
  resolve(Response.json({ run_id: 'run & 1', decisions: [decision] }));
  const summary = await screen.findByText('alice · budget · refuse');
  const details = summary.closest('details')!;
  expect(details.open).toBe(false);
  fireEvent.click(summary);
  expect(details.open).toBe(true);
  expect(screen.getByText('this decision names no policy version')).toBeTruthy();
  expect(screen.getByText('sha256:recorded-input')).toBeTruthy();
  expect(screen.getByText('sha256:recorded-anchor')).toBeTruthy();
  expect(screen.getByText(/record is meaningless without its journal anchor/)).toBeTruthy();
  expect(screen.getByText(/"next_cost_usd": 2/)).toBeTruthy();
  expect(screen.getByText('9 USD · verified')).toBeTruthy();
  expect(screen.getByText('10 USD · verified')).toBeTruthy();
  expect(screen.getByText('ledger via existing verifier')).toBeTruthy();
  expect(fetch).toHaveBeenCalledTimes(1);
});

it('shows empty and failed requests explicitly and removes previous run data', async () => {
  const fetch = vi.fn()
    .mockResolvedValueOnce(Response.json({ run_id: 'first', decisions: [decision] }))
    .mockResolvedValueOnce(Response.json({ run_id: 'empty', decisions: [] }))
    .mockResolvedValueOnce(Response.json({ detail: 'unavailable' }, { status: 503 }));
  vi.stubGlobal('fetch', fetch);
  mount();
  load('first');
  await screen.findByText('alice · budget · refuse');
  load('empty');
  await screen.findByText('No recorded decisions found for this run.');
  expect(screen.queryByText(/alice/)).toBeNull();
  load('unavailable');
  expect((await screen.findByRole('alert')).textContent).toContain('Could not load recorded decisions.');
  expect(screen.queryByText('No recorded decisions found for this run.')).toBeNull();
  expect(screen.queryByText(/alice/)).toBeNull();
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3));
});

it.each(['unavailable', 'mismatch'] as const)('renders %s evidence without inventing prior spend or anchor', async (state) => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json({ run_id: 'run', decisions: [{
    ...decision, journal_entry_hash: '', budget: {
      ...decision.budget,
      cap_usd: { ...decision.budget!.cap_usd, state },
      prior_spend_usd: { ...decision.budget!.prior_spend_usd, state, value: null, reason: 'No matching decision-time evidence.' },
    },
  }] })));
  mount();
  load('run');
  await screen.findByText(`unavailable · ${state}`);
  expect(screen.getByText(`10 USD · ${state}`)).toBeTruthy();
  expect(screen.queryByText(/9 USD/)).toBeNull();
  expect(screen.getByText('missing')).toBeTruthy();
  expect(screen.getByText('No matching decision-time evidence.')).toBeTruthy();
});
