// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import { DemoAuthGate, DemoBanner } from '@/components/demo/DemoMode';
import { Brand } from '@/components/layout/Brand';
import { HealthStatus } from '@/components/layout/HealthStatus';
import { LogoutButton } from '@/components/layout/LogoutButton';
import { MobileNav } from '@/components/layout/MobileNav';
import { NavLinks } from '@/components/layout/NavLinks';

// The dashboard shell: a server component, with client islands only where state
// lives (active nav item, health check, logout, the mobile menu sheet).
export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  return (
    <DemoAuthGate>
      <div className="min-h-screen bg-bg">
        <a
          href="#main"
          className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-[60] focus:rounded-md focus:bg-primary focus:px-3 focus:py-2 focus:text-primary-fg"
        >
          Skip to content
        </a>

        {/* Desktop sidebar */}
        <aside className="fixed inset-y-0 left-0 hidden w-60 flex-col gap-6 border-r border-border bg-surface-sunken px-4 py-5 lg:flex">
          <Brand />
          <NavLinks />
          <div className="mt-auto space-y-2">
            <HealthStatus />
            <LogoutButton />
          </div>
        </aside>

        <div className="flex min-h-dvh flex-col lg:pl-60">
          <MobileNav />
          <main id="main" tabIndex={-1} className="flex flex-1 flex-col p-4 focus:outline-none sm:p-6">
            <DemoBanner />
            {children}
          </main>
        </div>
      </div>
    </DemoAuthGate>
  );
}
