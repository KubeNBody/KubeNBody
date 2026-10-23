// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import { cookies } from 'next/headers';
import { NextRequest, NextResponse } from 'next/server';
import { API_BASE_URL } from '@/lib/data/server';
import { allowedWithoutSession, backendApiPath, filterResponseHeaders, forwardRequestHeaders } from '@/lib/proxy/rules';

// Browser -> backend proxy for every client-side /api/* call (axios, downloads).
// The backend is only reachable inside the cluster, and the session token lives
// in an httpOnly cookie: this handler turns the cookie into the Authorization
// header. What crosses in each direction is decided in lib/proxy/rules.ts.
async function proxy(request: NextRequest, { params }: { params: Promise<{ proxy: string[] }> }) {
  const { proxy: segments } = await params;
  const path = backendApiPath(segments);
  if (!path) {
    return NextResponse.json({ detail: 'Invalid API path' }, { status: 400 });
  }
  const search = request.nextUrl.search;
  const token = (await cookies()).get('auth_token')?.value;
  if (!token?.trim() && !allowedWithoutSession(request.method, path)) {
    return NextResponse.json({ detail: 'Not authenticated' }, { status: 401 });
  }

  try {
    const hasBody = request.method !== 'GET' && request.method !== 'HEAD';
    const response = await fetch(`${API_BASE_URL}${path}${search}`, {
      method: request.method,
      headers: forwardRequestHeaders(request.headers, token),
      // Streamed as-is, so multipart boundaries and large uploads pass through untouched
      body: hasBody ? request.body : undefined,
      // @ts-expect-error duplex is required by Node's fetch for a streamed body (not in the DOM types)
      duplex: 'half',
    });

    return new NextResponse(response.body, {
      status: response.status,
      statusText: response.statusText,
      headers: filterResponseHeaders(response.headers),
    });
  } catch (error) {
    // Log the cause server-side only. The client gets a generic gateway error
    console.error('Proxy error:', error);
    return NextResponse.json({ detail: 'Backend unavailable' }, { status: 502 });
  }
}

export { proxy as GET, proxy as HEAD, proxy as POST, proxy as PUT, proxy as PATCH, proxy as DELETE };
