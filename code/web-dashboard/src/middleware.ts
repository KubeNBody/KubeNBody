// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';

// True when the JWT's `exp` has passed. The signature isn't verified here (the backend
// does that). This only stops an expired cookie from bouncing /login -> /dashboard.
function isExpired(token: string): boolean {
  try {
    const payload = token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
    const { exp } = JSON.parse(atob(payload));
    return typeof exp === 'number' && exp * 1000 <= Date.now();
  } catch {
    return true;
  }
}

export function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;

  // Get auth token from cookies. An expired one counts as none
  const cookieToken = request.cookies.get('auth_token')?.value;
  const token = cookieToken && !isExpired(cookieToken) ? cookieToken : undefined;

  // Public paths that don't require authentication
  const publicPaths = ['/login'];
  const isPublicPath = publicPaths.some(path => pathname.startsWith(path));

  // API routes, Next.js internals, and static files are allowed
  if (
    pathname === '/logout' ||
    pathname.startsWith('/api/') ||
    pathname.startsWith('/_next/') ||
    pathname.startsWith('/static/') ||
    pathname.includes('.')
  ) {
    return NextResponse.next();
  }

  // Redirect to dashboard if logged in and trying to access login page
  if (isPublicPath && token) {
    return NextResponse.redirect(new URL('/dashboard', request.url));
  }

  // Redirect to login if not authenticated and trying to access protected route
  if (!isPublicPath && !token) {
    const loginUrl = new URL('/login', request.url);
    const response = NextResponse.redirect(loginUrl);
    if (cookieToken) {
      response.cookies.delete('auth_token');
    }
    return response;
  }

  return NextResponse.next();
}

export const config = {
  matcher: [
    /*
     * Match all request paths except:
     * - api/ (the proxy reads the cookie itself, and a request that passes through
     *   middleware has its body cut at 10 MB by Next 15.5 (middlewareClientMaxBodySize),
     *   which silently truncated every larger upload)
     * - _next/static (static files)
     * - _next/image (image optimization files)
     * - favicon.ico (favicon file)
     */
    '/((?!api/|_next/static|_next/image|favicon.ico).*)',
  ],
};
