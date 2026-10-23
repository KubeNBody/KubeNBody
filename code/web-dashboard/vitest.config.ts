// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import { defineConfig } from 'vitest/config';

// Unit tests for the pure modules (lib/, form schema).
export default defineConfig({
  test: {
    include: ['src/**/*.test.ts'],
    environment: 'node',
    alias: {
      '@/': new URL('./src/', import.meta.url).pathname,
    },
  },
});
