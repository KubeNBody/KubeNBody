// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
// What the /api/* proxy (app/api/[...proxy]/route.ts) passes between the browser
// and the backend. Pure functions, unit-tested in lib/__tests__/proxy-rules.test.ts.

// Request headers the backend needs. Everything else stays behind - above all the
// browser's cookie header (the session token travels only as the Authorization
// header the proxy sets) and any Authorization the browser sent itself.
const FORWARDED_REQUEST_HEADERS = [
  'accept',
  'accept-language',
  'content-type',
  'if-none-match',
  'if-modified-since',
  'range',
  'user-agent',
] as const;

// Response headers that must not be copied: fetch() already decoded the body, so
// its encoding and length no longer describe what we send. Hop-by-hop headers
// belong to the backend connection. The backend sets no cookies for the browser.
const DROPPED_RESPONSE_HEADERS = new Set([
  'content-encoding',
  'content-length',
  'transfer-encoding',
  'connection',
  'keep-alive',
  'set-cookie',
]);

export function forwardRequestHeaders(incoming: Headers, token: string | undefined): Headers {
  const out = new Headers();
  for (const name of FORWARDED_REQUEST_HEADERS) {
    const value = incoming.get(name);
    if (value !== null) out.set(name, value);
  }
  if (token && token.trim()) out.set('authorization', `Bearer ${token}`);
  return out;
}

export function filterResponseHeaders(incoming: Headers): Headers {
  const out = new Headers();
  incoming.forEach((value, name) => {
    if (!DROPPED_RESPONSE_HEADERS.has(name.toLowerCase())) out.set(name, value);
  });
  return out;
}

/**
 * Backend path for the proxied segments (`/api/<segments>`), each re-encoded so
 * a decoded "/" or "?" can't change the path. Returns null for "." / ".." segments, which
 * would otherwise let `fetch` resolve out of /api (e.g. to /metrics or /docs).
 */
export function backendApiPath(segments: string[]): string | null {
  if (segments.length === 0) return null;
  if (segments.some((s) => s === '' || s === '.' || s === '..')) return null;
  return `/api/${segments.map(encodeURIComponent).join('/')}`;
}

/**
 * Whether a request without a session cookie may reach the backend: only the
 * login. Everything else is answered 401 by the proxy itself, so an anonymous
 * visitor can't make the backend read a body or run a health check.
 */
export function allowedWithoutSession(method: string, path: string): boolean {
  return method.toUpperCase() === 'POST' && path === '/api/auth/login';
}

/**
 * Whether the session cookie gets the Secure flag: when the browser reached us
 * over HTTPS (directly, or through a TLS-terminating proxy that sets
 * x-forwarded-proto), or when COOKIE_SECURE=1 forces it.
 */
export function cookieShouldBeSecure(requestUrl: string, forwardedProto: string | null, force = process.env.COOKIE_SECURE): boolean {
  if (force === '1') return true;
  if (forwardedProto) return forwardedProto.split(',')[0].trim().toLowerCase() === 'https';
  return requestUrl.startsWith('https:');
}
