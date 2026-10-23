// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import { useState } from 'react';
import { Menu } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Sheet, SheetContent, SheetDescription, SheetTitle, SheetTrigger } from '@/components/ui/sheet';
import { Brand } from './Brand';
import { HealthStatus } from './HealthStatus';
import { LogoutButton } from './LogoutButton';
import { NavLinks } from './NavLinks';

/** Below lg: a top bar whose menu opens the navigation in a sheet (focus-trapped, Esc closes) */
export function MobileNav() {
  const [open, setOpen] = useState(false);
  return (
    <header className="sticky top-0 z-40 flex items-center gap-3 border-b border-border bg-bg/95 px-4 py-3 backdrop-blur lg:hidden">
      <Sheet open={open} onOpenChange={setOpen}>
        <SheetTrigger asChild>
          <Button variant="ghost" size="icon" aria-label="Open menu">
            <Menu />
          </Button>
        </SheetTrigger>
        <SheetContent side="left" className="w-72 border-border bg-surface-sunken p-0">
          <SheetTitle className="sr-only">Navigation</SheetTitle>
          <SheetDescription className="sr-only">Pages of the dashboard</SheetDescription>
          <div className="flex h-full flex-col gap-6 px-4 py-5">
            <Brand />
            <NavLinks onNavigate={() => setOpen(false)} />
            <div className="mt-auto space-y-2">
              <HealthStatus />
              <LogoutButton />
            </div>
          </div>
        </SheetContent>
      </Sheet>
      <Brand />
    </header>
  );
}
