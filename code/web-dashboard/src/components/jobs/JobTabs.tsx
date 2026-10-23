// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import { useEffect, useState } from 'react';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';

export interface JobTab {
  id: string;
  label: string;
  content: React.ReactNode;
}

/** The job page's sections as tabs. The open tab is kept in the URL hash (#parameters) */
export function JobTabs({ tabs }: { tabs: JobTab[] }) {
  const [active, setActive] = useState(tabs[0].id);

  useEffect(() => {
    const fromHash = () => {
      const id = window.location.hash.slice(1);
      if (tabs.some((t) => t.id === id)) setActive(id);
    };
    fromHash();
    window.addEventListener('hashchange', fromHash);
    return () => window.removeEventListener('hashchange', fromHash);
  }, [tabs]);

  return (
    <Tabs
      value={active}
      onValueChange={(id) => {
        setActive(id);
        // Keep Next's router state in the entry: replaceState(null, ...) wiped it,
        // after which router.refresh() (the running job's auto-refresh) stopped
        window.history.replaceState(window.history.state, '', `#${id}`);
      }}
    >
      <TabsList className="mb-4 bg-surface">
        {tabs.map((t) => (
          <TabsTrigger key={t.id} value={t.id} className="data-[state=active]:bg-surface-raised data-[state=active]:text-fg text-fg-muted">
            {t.label}
          </TabsTrigger>
        ))}
      </TabsList>
      {tabs.map((t) => (
        <TabsContent key={t.id} value={t.id}>
          {t.content}
        </TabsContent>
      ))}
    </Tabs>
  );
}
