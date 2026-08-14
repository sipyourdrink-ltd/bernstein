import { useMemo } from 'react';
import {
  EmptyState,
  ErrorState,
  LoadingState,
  Pill,
  SectionLabel,
  StatusDot,
} from '@/lib/states';
import { typeScaleNames } from '@/lib/type-scale.js';

type StyleDeclaration = {
  length: number;
  item(index: number): string;
};

type StyleRule = {
  cssRules?: ArrayLike<StyleRule>;
  selectorText?: string;
  style?: StyleDeclaration;
};

type StyleSheet = {
  cssRules?: CSSRuleList | ArrayLike<StyleRule>;
};

export type CssToken = {
  name: string;
  value: string;
};

function isDashboardTokenRule(rule: StyleRule): boolean {
  return rule.selectorText?.split(',').some((selector) => {
    const trimmed = selector.trim();
    return trimmed === ':root' || trimmed === '.dark';
  }) ?? false;
}

function isDashboardTokenName(name: string): boolean {
  return !name.startsWith('--tw-') && !name.startsWith('--lightningcss-');
}

function collectDashboardTokenNames(
  rules: CSSRuleList | ArrayLike<StyleRule> | undefined,
  names: Set<string>,
): void {
  if (!rules) return;

  for (const rule of Array.from(rules as ArrayLike<StyleRule | CSSRule>)) {
    const styleRule = rule as StyleRule;
    if (styleRule.style && isDashboardTokenRule(styleRule)) {
      for (let index = 0; index < styleRule.style.length; index += 1) {
        const name = styleRule.style.item(index);
        if (name.startsWith('--') && isDashboardTokenName(name)) names.add(name);
      }
    }
    collectDashboardTokenNames(styleRule.cssRules, names);
  }
}

export function collectCssTokens(
  styleSheets: Iterable<StyleSheet>,
  resolveValue: (name: string) => string,
): CssToken[] {
  const names = new Set<string>();

  for (const styleSheet of styleSheets) {
    try {
      collectDashboardTokenNames(styleSheet.cssRules, names);
    } catch {
      // A cross-origin stylesheet may not expose cssRules. The dashboard's own
      // styles stay readable, so ignore only the inaccessible sheet.
    }
  }

  return [...names]
    .sort()
    .map((name) => ({ name, value: resolveValue(name).trim() || 'unresolved' }));
}

function isHslToken(value: string): boolean {
  return /^-?[\d.]+(?:deg)?\s+-?[\d.]+%\s+-?[\d.]+%(?:\s*\/\s*-?[\d.]+%)?$/.test(value);
}

function useCssTokens(): CssToken[] {
  return useMemo(() => {
    if (typeof document === 'undefined') return [];
    const rootStyle = getComputedStyle(document.documentElement);
    return collectCssTokens(
      document.styleSheets,
      (name) => rootStyle.getPropertyValue(name),
    );
  }, []);
}

const statusKinds = ['running', 'queued', 'stalled', 'failed', 'done', 'merging', 'idle'] as const;
const pillKinds = ['default', 'accent', 'success', 'warning', 'danger', 'ghost'] as const;

export function VocabularyContent({ tokens }: { tokens: CssToken[] }) {
  return (
    <main className="min-h-screen bg-background p-6 text-foreground sm:p-10">
      <div className="mx-auto max-w-6xl space-y-10">
        <header className="space-y-2 border-b border-border pb-6">
          <SectionLabel trailing={<Pill kind="ghost">developer reference</Pill>}>
            dashboard vocabulary
          </SectionLabel>
          <h1 className="text-h1">Reference</h1>
          <p className="max-w-2xl text-body text-muted-foreground">
            Runtime tokens, Tailwind type steps, and shared UI states rendered from the modules the dashboard uses.
          </p>
        </header>

        <section className="space-y-3">
          <SectionLabel trailing={<Pill>{tokens.length} tokens</Pill>}>runtime tokens</SectionLabel>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {tokens.map((token) => (
              <article key={token.name} data-token={token.name} className="rounded-md border border-border bg-card p-3">
                {isHslToken(token.value) ? (
                  <div data-token-preview="color" className="mb-3 h-14 rounded-sm border border-border-subtle" style={{ background: `hsl(var(${token.name}))` }} />
                ) : (
                  <div data-token-preview="value" className="mb-3 flex h-14 items-center rounded-sm border border-border-subtle bg-muted px-3">
                    <div className="h-7 w-full bg-foreground" style={{ borderRadius: `var(${token.name})` }} />
                  </div>
                )}
                <code className="block text-body-md text-foreground">{token.name}</code>
                <code className="mt-1 block text-log text-muted-foreground">{token.value}</code>
              </article>
            ))}
          </div>
        </section>

        <section className="space-y-3">
          <SectionLabel>Tailwind type scale</SectionLabel>
          <div className="grid gap-3 rounded-md border border-border bg-card p-4">
            {typeScaleNames.map((step) => (
              <div key={step} data-type-step={step} className="flex items-baseline gap-4 border-b border-border-subtle pb-3 last:border-0 last:pb-0">
                <code className="w-20 shrink-0 text-log text-meta-foreground">text-{step}</code>
                <span className={`text-${step}`}>The quick brown fox verifies the scale.</span>
              </div>
            ))}
          </div>
        </section>

        <section className="space-y-3">
          <SectionLabel>Shared states</SectionLabel>
          <div className="grid gap-4 lg:grid-cols-2">
            <EmptyState
              title="No records match this reference"
              description="The dashboard uses this component whenever an expected collection is empty."
              icon={<StatusDot kind="idle" className="size-3" />}
              action={{ label: 'Example action', onClick: () => undefined, variant: 'secondary' }}
            />
            <ErrorState
              title="Reference failure"
              message="This is the shared error treatment, including its retry path."
              retry={() => undefined}
              helpHref="/ui/tasks"
            />
            <div className="rounded-md border border-border bg-card p-4">
              <LoadingState label="Loading reference rows" rows={4} />
            </div>
            <div className="space-y-3 rounded-md border border-border bg-card p-4">
              <SectionLabel>status dots</SectionLabel>
              <div className="flex flex-wrap gap-3">
                {statusKinds.map((kind) => (
                  <span key={kind} className="inline-flex items-center gap-1.5 text-body text-muted-foreground">
                    <StatusDot kind={kind} />
                    {kind}
                  </span>
                ))}
              </div>
              <SectionLabel>pills</SectionLabel>
              <div className="flex flex-wrap gap-2">
                {pillKinds.map((kind) => (
                  <Pill key={kind} kind={kind} strong={kind === 'accent'}>
                    {kind}
                  </Pill>
                ))}
              </div>
            </div>
          </div>
        </section>
      </div>
    </main>
  );
}

export default function Vocabulary() {
  return <VocabularyContent tokens={useCssTokens()} />;
}
