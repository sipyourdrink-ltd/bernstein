// A single posted artifact version (#2553).
//
// Reports render as markdown text, tables as a grid, links as a plain anchor
// with a kind badge (never embedded content). A version whose stored blob fails
// its hash check renders as *tampered* with the content withheld - the surface
// must never present unverifiable bytes as content. The header always shows the
// key, version, journal position, and a short content hash so the record is
// bound to the exact run position that produced it.

import { AlertTriangle, ExternalLink, ShieldCheck } from 'lucide-react';

import { cn } from '@/lib/utils';

import type { ArtifactView, LinkContent, ReportContent, TableContent } from './types';

export interface ArtifactCardProps {
  artifact: ArtifactView;
}

function VerifyBadge({ verified }: { verified: boolean }) {
  if (verified) {
    return (
      <span className="inline-flex items-center gap-1 rounded bg-secondary/60 px-1.5 py-0.5 text-[11px] font-medium text-foreground">
        <ShieldCheck className="h-3 w-3" aria-hidden />
        verified
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 rounded bg-destructive/15 px-1.5 py-0.5 text-[11px] font-medium text-destructive">
      <AlertTriangle className="h-3 w-3" aria-hidden />
      tampered
    </span>
  );
}

function ReportBody({ content }: { content: ReportContent }) {
  return (
    <pre className="mt-2 whitespace-pre-wrap break-words rounded bg-secondary/40 p-2.5 font-mono text-[12px] text-foreground">
      {content.body}
    </pre>
  );
}

function TableBody({ content }: { content: TableContent }) {
  return (
    <div className="mt-2 overflow-x-auto">
      <table className="w-full border-collapse text-[12px]">
        <thead>
          <tr>
            {content.columns.map((col) => (
              <th
                key={col}
                className="border border-border-subtle bg-secondary/40 px-2 py-1 text-left font-medium text-foreground"
              >
                {col}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {content.rows.map((row, ri) => (
            <tr key={ri}>
              {row.map((cell, ci) => (
                <td key={ci} className="border border-border-subtle px-2 py-1 text-foreground">
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function LinkBody({ content }: { content: LinkContent }) {
  return (
    <div className="mt-2 flex items-center gap-2">
      <span className="rounded bg-secondary/60 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-[0.1em] text-meta-foreground">
        {content.kind}
      </span>
      <a
        href={content.url}
        target="_blank"
        rel="noopener noreferrer"
        className="inline-flex items-center gap-1 break-all text-[12.5px] text-primary underline-offset-2 hover:underline"
      >
        {content.url}
        <ExternalLink className="h-3 w-3 shrink-0" aria-hidden />
      </a>
    </div>
  );
}

function ArtifactBody({ artifact }: { artifact: ArtifactView }) {
  if (!artifact.verified || artifact.content === null) {
    return (
      <div className="mt-2 rounded border border-destructive/40 bg-destructive/10 px-2.5 py-2 text-[12px] text-destructive">
        Content withheld: the stored blob does not recompute from the seal.
        {artifact.verify_reason ? <div className="mt-1 font-mono text-[11px] opacity-80">{artifact.verify_reason}</div> : null}
      </div>
    );
  }
  if (artifact.content.type === 'report') return <ReportBody content={artifact.content} />;
  if (artifact.content.type === 'table') return <TableBody content={artifact.content} />;
  return <LinkBody content={artifact.content} />;
}

export function ArtifactCard({ artifact }: ArtifactCardProps) {
  return (
    <div
      className={cn(
        'rounded-md border bg-card px-3 py-2.5',
        artifact.verified ? 'border-border' : 'border-destructive/50',
      )}
    >
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="font-medium text-[13px] text-foreground">{artifact.key}</span>
        <span className="rounded bg-secondary/50 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-[0.1em] text-meta-foreground">
          {artifact.artifact_type}
        </span>
        <span className="font-mono text-[11px] text-meta-foreground">v{artifact.version}</span>
        <VerifyBadge verified={artifact.verified} />
        <span className="ml-auto font-mono text-[10px] text-meta-foreground">
          idx {artifact.journal_index} · {artifact.content_hash.replace(/^sha256:/, '').slice(0, 12)}
        </span>
      </div>
      <ArtifactBody artifact={artifact} />
    </div>
  );
}
