// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import { LogOut } from 'lucide-react';
import { logout } from '@/actions/auth';

export function LogoutButton() {
  return (
    <button
      type="button"
      onClick={() => logout()}
      className="flex w-full items-center gap-3 rounded-md px-3 py-2 text-sm font-medium text-fg-muted transition-colors hover:bg-danger/10 hover:text-danger"
    >
      <LogOut className="size-4" aria-hidden />
      Log out
    </button>
  );
}
