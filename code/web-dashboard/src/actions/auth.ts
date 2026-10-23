// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use server';

import { cookies, headers } from 'next/headers';
import { redirect } from 'next/navigation';
import { APIServerError } from '@/lib/api-error';
import { backendFetch } from '@/lib/data/server';
import { cookieShouldBeSecure } from '@/lib/proxy/rules';
import type { LoginState } from '@/lib/data/types';

interface LoginResponse {
  access_token: string;
  token_type: string;
}

export async function login(_previous: LoginState, form: FormData): Promise<LoginState> {
  const username = String(form.get('username') ?? '');
  const password = String(form.get('password') ?? '');
  let data: LoginResponse;
  try {
    data = await backendFetch<LoginResponse>('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    });
  } catch (error) {
    // A 4xx is the backend's answer (wrong credentials, validation): show it
    if (error instanceof APIServerError && error.status !== undefined && error.status < 500) {
      return { error: error.message || 'Invalid username or password' };
    }
    console.error('Login error:', error);
    return { error: 'An error occurred during login' };
  }

  // httpOnly: the token never reaches browser JavaScript. The /api proxy and the
  // server components read it from here. Secure whenever the browser came in
  // over HTTPS (or COOKIE_SECURE=1), so it is never sent over plain HTTP then.
  const forwardedProto = (await headers()).get('x-forwarded-proto');
  (await cookies()).set('auth_token', data.access_token, {
    httpOnly: true,
    secure: cookieShouldBeSecure('', forwardedProto),
    sameSite: 'lax',
    maxAge: 60 * 60 * 24, // the token's lifetime (JWT_EXPIRE_MINUTES = 1440)
    path: '/',
  });

  // Outside the try: redirect() works by throwing
  redirect('/dashboard');
}

export async function logout() {
  (await cookies()).delete('auth_token');
  redirect('/login');
}
