import { useQuery } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { apiGet } from '@/lib/api';

type BudgetValue = {
  value: number | null;
  state: 'verified' | 'unavailable' | 'mismatch';
  provenance: string;
  reason: string;
};

export type RecordedDecision = {
  decision_id: string;
  run_id: string;
  subject: string;
  action: string;
  verdict: string;
  inputs_hash: string;
  timestamp: number;
  context: Record<string, unknown>;
  journal_entry_hash: string;
  policy_version: string;
  budget: {
    cap_usd: BudgetValue;
    prior_spend_usd: BudgetValue;
    verification_scope: string;
  } | null;
};

type DecisionReport = { run_id: string; decisions: RecordedDecision[] };

function BudgetInput({ label, evidence }: { label: string; evidence: BudgetValue }) {
  return (
    <div className="space-y-1">
      <dt>{label}</dt>
      <dd>
        <p className="font-mono">{evidence.value === null ? 'unavailable' : `${evidence.value} USD`} · {evidence.state}</p>
        <p>{evidence.provenance}</p>
        <p>{evidence.reason}</p>
      </dd>
    </div>
  );
}

export function RecordedDecisionRows({ decisions }: { decisions: RecordedDecision[] }) {
  if (!decisions.length) return <p>No recorded decisions found for this run.</p>;
  return (
    <div className="space-y-2">
      {decisions.map((decision) => (
        <details key={decision.decision_id} className="rounded-md border border-border bg-background p-3">
          <summary className="cursor-pointer break-words text-body-md text-foreground">
            {decision.subject} · {decision.action} · {decision.verdict}
          </summary>
          <dl className="mt-3 space-y-3 break-words text-body text-foreground">
            <div><dt>Subject</dt><dd>{decision.subject}</dd></div>
            <div><dt>Action</dt><dd>{decision.action}</dd></div>
            <div><dt>Recorded verdict</dt><dd>{decision.verdict}</dd></div>
            <div><dt>Recorded timestamp</dt><dd>{decision.timestamp}</dd></div>
            <div><dt>Input hash</dt><dd className="break-all font-mono">{decision.inputs_hash}</dd></div>
            <div><dt>Policy version</dt><dd>{decision.policy_version}</dd></div>
            <div>
              <dt>Recorded inputs (context)</dt>
              <dd><pre className="overflow-auto whitespace-pre-wrap break-all font-mono">{JSON.stringify(decision.context, null, 2)}</pre></dd>
            </div>
            <div>
              <dt>Journal anchor (journal_entry_hash)</dt>
              <dd className="break-all font-mono">{decision.journal_entry_hash || 'missing'}</dd>
              <dd>The record is meaningless without its journal anchor. This read does not verify the anchor.</dd>
            </div>
            {decision.budget && (
              <>
                <BudgetInput label="Recorded cap" evidence={decision.budget.cap_usd} />
                <BudgetInput label="Projected prior spend" evidence={decision.budget.prior_spend_usd} />
                <div><dt>Budget verification scope</dt><dd>{decision.budget.verification_scope}</dd></div>
              </>
            )}
          </dl>
        </details>
      ))}
    </div>
  );
}

export function RecordedDecisions() {
  const [input, setInput] = useState('');
  const [runId, setRunId] = useState('');
  const query = useQuery({
    queryKey: ['governance-decisions', runId],
    queryFn: ({ signal }) => apiGet<DecisionReport>(`/governance/decisions?run_id=${encodeURIComponent(runId)}`, { signal }),
    enabled: !!runId,
    retry: false,
    refetchOnWindowFocus: false,
  });

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const next = input.trim();
    if (!next) return;
    if (next === runId) void query.refetch();
    else setRunId(next);
  }

  return (
    <section aria-labelledby="recorded-decisions-heading" className="space-y-3 rounded-md border border-border bg-card p-4">
      <h2 id="recorded-decisions-heading" className="text-h3 text-foreground">Recorded decisions</h2>
      <p className="text-body text-muted-foreground">This read cannot verify the journal anchor or signer. Expand a decision to inspect its recorded verdict and inputs.</p>
      <form onSubmit={submit} className="flex flex-wrap items-end gap-3">
        <label className="space-y-1 text-body text-foreground">
          <span className="block">Run ID</span>
          <input className="rounded-md border border-border bg-background px-3 py-2" value={input} onChange={(event) => setInput(event.target.value)} />
        </label>
        <button type="submit" disabled={!input.trim() || query.isFetching} className="rounded-md border border-border px-3 py-2 text-body-md text-foreground disabled:opacity-50">Load decisions</button>
      </form>
      {query.isFetching && <p role="status">Loading recorded decisions...</p>}
      {query.isError && <p role="alert">Could not load recorded decisions. {query.error.message}</p>}
      {!query.isFetching && !query.isError && query.data && (
        <div className="space-y-2">
          <p className="break-all font-mono">Run {query.data.run_id}</p>
          <RecordedDecisionRows decisions={query.data.decisions} />
        </div>
      )}
    </section>
  );
}
