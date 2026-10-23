// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
const path = require('path');

// One source tree, two builds. Pages and layouts that exist in only one build carry a
// suffix, page.server.tsx (normal build) or page.demo.tsx (static demo), and each
// build's pageExtensions sees only its own. Plain .tsx ones are shared. The demo
// build also ignores plain .ts route files: route handlers and the middleware,
// which a static export rejects, are normal-build only. (They keep plain names:
// a route.server.ts handler makes next's file tracing look for a client manifest
// that route handlers don't have.)
const IS_DEMO = process.env.NEXT_PUBLIC_DEMO_MODE === '1';

// Demo build: the data layer's real modules (server fetches with the session
// cookie, server actions) are swapped for their fixture-backed implementations.
// Both sides implement src/lib/data/types.ts, which type-checks them.
const DEMO_MODULES = [
  [/^@\/lib\/data\/server$/, 'src/lib/data/demo/server.ts'],
  [/^@\/actions\/(jobs|files|auth)$/, 'src/lib/data/demo/actions/$1.ts'],
];

/** @type {import('next').NextConfig} */
const nextConfig = {
  pageExtensions: IS_DEMO ? ['demo.tsx', 'tsx', 'jsx'] : ['server.tsx', 'tsx', 'ts', 'jsx', 'js'],
  output: 'standalone',
  // Trace from this app, not a lockfile further up (keeps server.js at the standalone root)
  outputFileTracingRoot: __dirname,
  // No next/image: nothing to optimize, so the image the dashboard ships
  // carries no sharp and its bundled libvips (LGPL)
  images: { unoptimized: true },
  outputFileTracingExcludes: { '*': ['node_modules/sharp/**', 'node_modules/@img/**'] },
  poweredByHeader: false,
  compress: true,
  experimental: {
    scrollRestoration: true,
    // Barrel packages: import only the parts used (the shadcn primitives import
    // from the 'radix-ui' umbrella, which otherwise pulls in every primitive)
    optimizePackageImports: ['radix-ui', 'lucide-react'],
  },
  // Always defined, so Next inlines it and the static demo's branches
  // (src/lib/demo.ts) are dropped from the normal bundle as dead code
  env: {
    NEXT_PUBLIC_DEMO_MODE: process.env.NEXT_PUBLIC_DEMO_MODE || '0',
  },
  webpack: (config, { dev, webpack }) => {
    if (IS_DEMO) {
      for (const [pattern, target] of DEMO_MODULES) {
        config.plugins.push(
          new webpack.NormalModuleReplacementPlugin(pattern, (resource) => {
            resource.request = path.join(__dirname, resource.request.replace(pattern, target));
          })
        );
      }
    }

    // Enhanced optimizations (tree shaking is handled by Next.js)
    if (!dev) {
      config.optimization.splitChunks.cacheGroups = {
        ...config.optimization.splitChunks.cacheGroups,
        three: {
          name: 'three',
          chunks: 'all',
          test: /[\\/]node_modules[\\/](three|@react-three)[\\/]/,
          priority: 30,
        },
      };
    }

    // Readable module boundaries in development builds
    if (dev) {
      config.optimization.concatenateModules = false;
    }

    return config;
  },
  // Enable transpilation of external packages
  transpilePackages: ['three', '@react-three/fiber', '@react-three/drei'],
};

// Static read-only demo build (scripts/build-static-demo.mjs)
if (IS_DEMO) {
  nextConfig.output = 'export';
  // With output 'export', distDir is where the exported site goes. The build
  // itself still runs in .next (scripts/build-static-demo.mjs keeps a normal
  // build's .next aside meanwhile)
  nextConfig.distDir = 'out';
  nextConfig.basePath = process.env.NEXT_PUBLIC_BASE_PATH || '';
  // dashboard/jobs/index.html etc., so deep links and reloads resolve on static hosts
  nextConfig.trailingSlash = true;
}

module.exports = nextConfig;