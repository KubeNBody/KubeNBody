// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import { cookies } from 'next/headers';
import { redirect } from 'next/navigation';

// Target for an expired/invalid session (client 401s, server fetches that get a 401):
// a page can't delete the httpOnly cookie while rendering, so it redirects here.
export async function GET() {
  (await cookies()).delete('auth_token');
  redirect('/login');
}
