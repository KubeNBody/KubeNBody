// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { cn } from '@/lib/utils';
import { NAV_SECTIONS, isNavActive } from './nav';

export function NavLinks({ onNavigate }: { onNavigate?: () => void }) {
  const pathname = usePathname();
  return (
    <nav aria-label="Main" className="space-y-5">
      {NAV_SECTIONS.map((section) => (
        <div key={section.title}>
          <p className="mb-1.5 px-3 text-xs font-medium uppercase tracking-wider text-fg-subtle">{section.title}</p>
          <ul className="space-y-0.5">
            {section.items.map((item) => {
              const active = isNavActive(pathname, item.href);
              const Icon = item.icon;
              return (
                <li key={item.href}>
                  <Link
                    href={item.href}
                    onClick={onNavigate}
                    aria-current={active ? 'page' : undefined}
                    className={cn(
                      'flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors',
                      active ? 'bg-primary/15 text-fg' : 'text-fg-muted hover:bg-surface-raised hover:text-fg'
                    )}
                  >
                    <Icon className={cn('size-4', active && 'text-link')} aria-hidden />
                    {item.name}
                  </Link>
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </nav>
  );
}
