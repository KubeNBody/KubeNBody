// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import { Search, X } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { cn } from '@/lib/utils';

// Building blocks of a list page's filter row (jobs, files, comparisons). The
// values live in the URL (hooks/use-url-filters), so these are controlled by it.

const ALL = '__all__'; // Radix Select items can't have an empty value

export function SearchInput({
  defaultValue,
  onChange,
  placeholder,
  label,
  className,
}: {
  defaultValue: string;
  onChange: (value: string) => void;
  placeholder: string;
  label: string;
  className?: string;
}) {
  return (
    <div className={cn('relative min-w-48 flex-1', className)}>
      <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-fg-subtle" aria-hidden />
      <Input
        type="search"
        defaultValue={defaultValue}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        aria-label={label}
        className="h-9 border-border bg-surface pl-9 text-fg placeholder:text-fg-subtle"
      />
    </div>
  );
}

export function FilterSelect({
  label,
  value,
  options,
  allLabel,
  onChange,
  className,
}: {
  label: string;
  value: string;
  options: readonly { value: string; label: string }[];
  /** Text of the "no filter" choice, e.g. "All statuses" */
  allLabel: string;
  onChange: (value: string) => void;
  className?: string;
}) {
  const active = value !== '';
  return (
    <Select value={value || ALL} onValueChange={(v) => onChange(v === ALL ? '' : v)}>
      <SelectTrigger
        aria-label={label}
        size="sm"
        className={cn(
          'min-w-36 border-border bg-surface text-fg',
          active && 'border-primary/60 bg-primary/10',
          className
        )}
      >
        <SelectValue />
      </SelectTrigger>
      <SelectContent className="max-h-80">
        <SelectItem value={ALL}>{allLabel}</SelectItem>
        {options.map((o) => (
          <SelectItem key={o.value} value={o.value}>
            {o.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

export function PageSizeSelect({ value, sizes = [20, 50, 100], onChange }: { value: number; sizes?: number[]; onChange: (size: number) => void }) {
  return (
    <Select value={String(value)} onValueChange={(v) => onChange(Number(v))}>
      <SelectTrigger aria-label="Rows per page" size="sm" className="w-28 border-border bg-surface text-fg">
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {sizes.map((s) => (
          <SelectItem key={s} value={String(s)}>
            {s} / page
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

export function ClearFilters({ visible, onClear }: { visible: boolean; onClear: () => void }) {
  if (!visible) return null;
  return (
    <Button variant="ghost" size="sm" onClick={onClear} className="text-fg-muted hover:text-fg">
      <X />
      Clear filters
    </Button>
  );
}
