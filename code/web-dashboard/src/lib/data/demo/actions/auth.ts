// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
// Demo implementation of AuthActions (../../types), swapped in for
// actions/auth.ts by next.config.js. Static export has no server actions: these
// run in the browser. The login is a cosmetic gate - the demo credentials are public.
import {
  BASE_PATH,
  DEMO_PASSWORD,
  DEMO_USERNAME,
  setDemoAuthenticated,
} from '@/lib/demo';
import type { LoginState } from '../../types';

export async function login(_previous: LoginState, form: FormData): Promise<LoginState> {
  if (form.get('username') !== DEMO_USERNAME || form.get('password') !== DEMO_PASSWORD) {
    return { error: 'Invalid username or password' };
  }
  setDemoAuthenticated(true);
  window.location.assign(`${BASE_PATH}/dashboard/`);
  return undefined;
}

export async function logout() {
  setDemoAuthenticated(false);
  window.location.assign(`${BASE_PATH}/login/`);
}
