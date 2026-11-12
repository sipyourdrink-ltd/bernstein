import { useMutation } from '@tanstack/react-query';
import { useState, type DragEvent, type FormEvent } from 'react';
import { api } from '@/lib/api';

interface ReceiptVerdict {
  v: number;
  ok: boolean;
  status: string;
  tier: 'integrity-only' | null;
  caveat: string | null;
  run_id: string;
  journal_events: number;
  spine_entries: number;
  divergent_step: number | null;
  errors: string[];
}

async function verifyReceipt(file: File): Promise<ReceiptVerdict> {
  const verdict = await api<ReceiptVerdict>('/governance/verify-receipt', {
    method: 'POST',
    body: file,
    headers: { 'Content-Type': 'application/octet-stream' },
    signal: AbortSignal.timeout(15_000),
  });
  if (
    verdict?.v !== 1 ||
    typeof verdict.ok !== 'boolean' ||
    !Array.isArray(verdict.errors) ||
    (verdict.ok && (verdict.tier !== 'integrity-only' || !verdict.caveat))
  ) {
    throw new Error('The server returned an unsupported receipt verdict.');
  }
  return verdict;
}

export function ReceiptVerification() {
  const [file, setFile] = useState<File | null>(null);
  const [selectionError, setSelectionError] = useState('');
  const verification = useMutation({
    mutationFn: verifyReceipt,
    networkMode: 'always',
    retry: false,
  });

  function selectFile(next: File | null) {
    verification.reset();
    setFile(next);
    setSelectionError(next ? '' : 'Choose one receipt file.');
  }

  function dropFile(event: DragEvent<HTMLLabelElement>) {
    event.preventDefault();
    if (verification.isPending) return;
    const { files } = event.dataTransfer;
    selectFile(files.length === 1 ? files[0] : null);
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (file && !verification.isPending) verification.mutate(file);
  }

  const verdict = verification.data;

  return (
    <section aria-labelledby="receipt-verification-heading" className="space-y-3 rounded-md border border-border bg-card p-4">
      <h2 id="receipt-verification-heading" className="text-h3 text-foreground">Verify a receipt</h2>
      <form onSubmit={submit} aria-busy={verification.isPending} className="flex flex-wrap items-end gap-3">
        <label
          className="block min-w-0 max-w-full flex-1 space-y-1 rounded-md border border-dashed border-meta-foreground bg-background px-3 py-2 text-body text-foreground focus-within:ring-2 focus-within:ring-ring"
          onDragOver={(event) => event.preventDefault()}
          onDrop={dropFile}
        >
          <span id="receipt-file-label" className="block text-body-md">Receipt file</span>
          <input
            type="file"
            className="sr-only"
            aria-labelledby="receipt-file-label"
            aria-describedby={file ? 'receipt-file-instructions receipt-file-selection' : 'receipt-file-instructions'}
            disabled={verification.isPending}
            onChange={(event) => {
              const selected = event.currentTarget.files?.[0];
              event.currentTarget.value = '';
              if (selected) selectFile(selected);
            }}
          />
          <span id="receipt-file-instructions" className="block text-muted-foreground">
            Drop a receipt here or choose a file.
          </span>
          <span id="receipt-file-selection" role="status" className="block break-all font-mono">
            {file && <><span className="sr-only">Selected file </span><span>{file.name}</span></>}
          </span>
        </label>
        <button
          type="submit"
          disabled={!file || verification.isPending}
          className="rounded-md border border-meta-foreground bg-card px-3 py-2 text-body-md text-foreground hover:bg-secondary focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:bg-secondary disabled:text-muted-foreground"
        >
          {verification.isError ? 'Retry verification' : 'Verify receipt'}
        </button>
      </form>
      {selectionError && <p role="alert" className="text-body text-destructive">{selectionError}</p>}
      {verification.isPending && <p role="status" className="text-body text-muted-foreground">Verifying receipt...</p>}
      {verification.isError && (
        <div role="alert" className="rounded-md border border-destructive p-3 text-body text-foreground">
          <p className="font-medium">Could not verify the receipt</p>
          <p>{verification.error.message}</p>
        </div>
      )}
      {verdict && (
        <div role="status" className="space-y-2 rounded-md border border-border bg-background p-3 text-body text-foreground">
          <p className="font-medium">{verdict.ok ? 'Receipt integrity verified' : 'Receipt did not verify'}</p>
          {verdict.ok && <p className="font-mono">{verdict.tier}</p>}
          {verdict.ok && (
            <p>
              The signed receipt contents were checked using the public key embedded in the receipt.
              To verify who signed it, use the operator's public key obtained separately with{' '}
              <code>bernstein verify receipt &lt;file&gt; --public-key &lt;pem&gt;</code>.
            </p>
          )}
          <dl className="flex flex-wrap gap-x-4 gap-y-1">
            <div><dt>Status</dt><dd className="font-mono">{verdict.status}</dd></div>
            {verdict.run_id && <div className="min-w-0"><dt>Run ID</dt><dd className="break-all font-mono">{verdict.run_id}</dd></div>}
            {verdict.ok && (
              <>
                <div><dt>Journal events</dt><dd className="font-mono">{verdict.journal_events}</dd></div>
                <div><dt>Spine entries</dt><dd className="font-mono">{verdict.spine_entries}</dd></div>
              </>
            )}
            {verdict.divergent_step !== null && (
              <div><dt>First divergent step</dt><dd className="font-mono">{verdict.divergent_step}</dd></div>
            )}
          </dl>
          {verdict.errors.length > 0 && (
            <ul className="list-disc space-y-1 break-words pl-5">
              {verdict.errors.map((error, index) => <li key={index}>{error}</li>)}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}
