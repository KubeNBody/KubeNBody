// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import type { Metadata } from 'next';
import { Inter } from 'next/font/google';
import './globals.css';
import { Providers } from './providers';
import { Toaster } from '@/components/ui/sonner';

// Exposed as --font-inter, which globals.css makes the default sans (font-sans)
const inter = Inter({ subsets: ['latin'], variable: '--font-inter' });

export const metadata: Metadata = {
  title: 'KubeNBody',
  description: 'Submit, follow, replay and compare N-body simulations run by KubeNBody',
  keywords: ['physics', 'simulation', 'n-body', 'dashboard', 'visualization'],
  authors: [{ name: 'Dariusz R. Augustyn' }, { name: 'Piotr Grzesik' }, { name: 'Łukasz Wyciślik' }],
};

export const viewport = {
  width: 'device-width',
  initialScale: 1,
  // Dark-only design (no light theme): browser chrome and native controls follow it
  themeColor: '#0f172a',
  colorScheme: 'dark',
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className={`dark ${inter.variable}`}>
      <body className="font-sans">
        <Providers>
          {children}
          <Toaster />
        </Providers>
      </body>
    </html>
  );
}