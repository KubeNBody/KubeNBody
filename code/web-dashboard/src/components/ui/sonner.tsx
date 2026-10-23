// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import { Toaster as Sonner } from 'sonner';

// App-wide toast outlet (the dashboard is dark-only). Call toast()/toast.error()
// from 'sonner' anywhere. A close button keeps toasts dismissable on touch.
export function Toaster() {
  return <Sonner theme="dark" position="bottom-right" richColors closeButton />;
}
