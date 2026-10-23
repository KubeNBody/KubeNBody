// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from 'vitest';
import { allowedWithoutSession, backendApiPath, cookieShouldBeSecure, filterResponseHeaders, forwardRequestHeaders } from '@/lib/proxy/rules';

describe('forwardRequestHeaders', () => {
  const incoming = new Headers({
    accept: 'application/json',
    'content-type': 'multipart/form-data; boundary=x',
    cookie: 'auth_token=secret; other=1',
    authorization: 'Bearer from-the-browser',
    host: 'dashboard',
    'x-forwarded-for': '1.2.3.4',
  });

  it('keeps the allowlisted headers and nothing else', () => {
    const out = forwardRequestHeaders(incoming, undefined);
    expect(out.get('accept')).toBe('application/json');
    expect(out.get('content-type')).toBe('multipart/form-data; boundary=x');
    for (const h of ['cookie', 'authorization', 'host', 'x-forwarded-for']) expect(out.has(h)).toBe(false);
  });

  it('sets the session token as the only Authorization', () => {
    expect(forwardRequestHeaders(incoming, 'tok').get('authorization')).toBe('Bearer tok');
    expect(forwardRequestHeaders(incoming, '  ').has('authorization')).toBe(false);
  });
});

describe('filterResponseHeaders', () => {
  it('drops encoding, length, hop-by-hop and cookies', () => {
    const out = filterResponseHeaders(
      new Headers({
        'content-type': 'text/csv',
        'content-disposition': 'attachment; filename="x.csv"',
        'content-encoding': 'gzip',
        'content-length': '10',
        'set-cookie': 'a=b',
        connection: 'keep-alive',
      })
    );
    expect([...out.keys()].sort()).toEqual(['content-disposition', 'content-type']);
  });
});

describe('backendApiPath', () => {
  it('maps segments under /api, re-encoded', () => {
    expect(backendApiPath(['jobs', '41', 'progress'])).toBe('/api/jobs/41/progress');
    expect(backendApiPath(['files', 'a/b?c'])).toBe('/api/files/a%2Fb%3Fc');
  });

  it('refuses dot segments, which would leave /api', () => {
    expect(backendApiPath(['x', '..', '..', 'metrics'])).toBeNull();
    expect(backendApiPath(['.'])).toBeNull();
    expect(backendApiPath([])).toBeNull();
  });
});

describe('cookieShouldBeSecure', () => {
  it('follows the scheme the browser used', () => {
    expect(cookieShouldBeSecure('http://dash/login', null, undefined)).toBe(false);
    expect(cookieShouldBeSecure('https://dash/login', null, undefined)).toBe(true);
    expect(cookieShouldBeSecure('http://pod:8501/login', 'https', undefined)).toBe(true);
    expect(cookieShouldBeSecure('http://pod:8501/login', 'http, https', undefined)).toBe(false);
  });

  it('can be forced on', () => {
    expect(cookieShouldBeSecure('http://dash/login', null, '1')).toBe(true);
  });
});

describe('allowedWithoutSession', () => {
  it('lets only the login through without a session', () => {
    expect(allowedWithoutSession('POST', '/api/auth/login')).toBe(true);
    expect(allowedWithoutSession('post', '/api/auth/login')).toBe(true);
    expect(allowedWithoutSession('GET', '/api/auth/login')).toBe(false);
    expect(allowedWithoutSession('GET', '/api/health')).toBe(false);
    expect(allowedWithoutSession('POST', '/api/jobs')).toBe(false);
    expect(allowedWithoutSession('POST', '/api/auth/login/x')).toBe(false);
  });
});
