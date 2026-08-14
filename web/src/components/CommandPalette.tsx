// ⌘K palette - fuzzy across routes + recent tasks. Static for now;
// task screens can extend by hooking into a shared store later.

import { useEffect, useMemo, useRef, useState } from 'react';
import { Search, ListChecks, Activity, ShieldCheck, ScrollText, DollarSign, Settings as SettingsIcon, Command, GitPullRequest } from 'lucide-react';
import { cn } from '@/lib/utils';

type NavItem = { label: string; to: string };
type ExternalNavItem = { label: string; href: string };

type ActionItem = {
  id: string;
  label: string;
  hint?: string;
  to?: string;
  href?: string;
  icon: typeof Search;
};

interface Props {
  open: boolean;
  onClose: () => void;
  onNavigate: (path: string) => void;
  nav: NavItem[];
  externalNav: ExternalNavItem[];
}

const ICON_FOR_LABEL: Record<string, typeof Search> = {
  Tasks: ListChecks,
  Agents: Activity,
  Approvals: ShieldCheck,
  Audit: ScrollText,
  Costs: DollarSign,
  Settings: SettingsIcon,
};

export function CommandPalette({ open, onClose, onNavigate, nav, externalNav }: Props) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState('');
  const [activeIdx, setActiveIdx] = useState(0);

  const items: ActionItem[] = useMemo(() => {
    const rows: ActionItem[] = nav.map((n) => ({
      id: `nav-${n.to}`,
      label: n.label,
      hint: 'Go to',
      to: n.to,
      icon: ICON_FOR_LABEL[n.label] ?? Search,
    }));
    rows.push(
      ...externalNav.map((n) => ({
        id: `external-${n.href}`,
        label: n.label,
        hint: 'Open',
        href: n.href,
        icon: GitPullRequest,
      })),
    );
    rows.push({
      id: 'nav-settings',
      label: 'Settings',
      hint: 'Go to',
      to: '/settings',
      icon: SettingsIcon,
    });
    if (!query.trim()) return rows;
    const q = query.toLowerCase();
    return rows.filter((r) => r.label.toLowerCase().includes(q));
  }, [nav, query]);

  useEffect(() => {
    if (!open) return;
    inputRef.current?.focus();
    setQuery('');
    setActiveIdx(0);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        onClose();
        return;
      }
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setActiveIdx((i) => Math.min(i + 1, items.length - 1));
        return;
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault();
        setActiveIdx((i) => Math.max(i - 1, 0));
        return;
      }
      if (e.key === 'Enter') {
        e.preventDefault();
        const item = items[activeIdx];
        if (item?.to) onNavigate(item.to);
        if (item?.href) window.location.assign(item.href);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, items, activeIdx, onNavigate, onClose]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 grid place-items-start pt-[12vh] bg-foreground/30 animate-fade-in"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label="Command palette"
    >
      <div
        className="w-full max-w-xl mx-auto bg-popover text-popover-foreground border border-border rounded-md shadow-lg overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2 px-3 py-2.5 border-b border-border-subtle">
          <Search className="size-3.5 text-meta-foreground" strokeWidth={1.5} />
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setActiveIdx(0);
            }}
            placeholder="Type to search…"
            className="flex-1 bg-transparent border-0 outline-none text-[13px] placeholder:text-meta-foreground"
          />
          <span className="font-mono text-[10px] text-meta-foreground border border-border-subtle rounded px-1.5 py-px">
            ESC
          </span>
        </div>
        <ul className="max-h-[320px] overflow-y-auto py-1">
          {items.length === 0 && (
            <li className="px-3 py-3 text-[12.5px] text-meta-foreground">No results</li>
          )}
          {items.map((it, i) => {
            const Icon = it.icon;
            return (
              <li key={it.id}>
                <button
                  type="button"
                  onMouseEnter={() => setActiveIdx(i)}
                  onClick={() => {
                    if (it.to) onNavigate(it.to);
                    if (it.href) window.location.assign(it.href);
                  }}
                  className={cn(
                    'w-full flex items-center gap-3 px-3 py-2 text-left text-[13px]',
                    i === activeIdx ? 'bg-secondary text-foreground' : 'text-muted-foreground',
                  )}
                >
                  <Icon className="size-3.5 shrink-0" strokeWidth={1.5} />
                  <span className="flex-1">{it.label}</span>
                  {it.hint && (
                    <span className="font-mono text-[10px] text-meta-foreground">{it.hint}</span>
                  )}
                </button>
              </li>
            );
          })}
        </ul>
        <div className="flex items-center gap-3 px-3 py-2 border-t border-border-subtle text-[10px] font-mono text-meta-foreground uppercase tracking-[0.08em]">
          <span className="flex items-center gap-1">
            <Command className="size-3" strokeWidth={1.5} /> K · open
          </span>
          <span>↑↓ · move</span>
          <span>↵ · go</span>
          <span>esc · close</span>
        </div>
      </div>
    </div>
  );
}
