// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import { BarChart3, FolderOpen, GitCompare, History, Home, Upload, type LucideIcon } from 'lucide-react';

export interface NavItem {
  name: string;
  href: string;
  icon: LucideIcon;
}

export const NAV_SECTIONS: { title: string; items: NavItem[] }[] = [
  {
    title: 'Simulations',
    items: [
      { name: 'Overview', href: '/dashboard', icon: Home },
      { name: 'Jobs', href: '/dashboard/jobs', icon: BarChart3 },
      { name: 'Submit Job', href: '/dashboard/submit', icon: Upload },
    ],
  },
  {
    title: 'Analysis',
    items: [
      { name: 'Compare', href: '/dashboard/compare', icon: GitCompare },
      { name: 'Saved Comparisons', href: '/dashboard/comparisons', icon: History },
    ],
  },
  {
    title: 'Data',
    items: [{ name: 'Input Files', href: '/dashboard/files', icon: FolderOpen }],
  },
];

/** Active for the item's page and anything under it (Jobs for /dashboard/jobs/41) */
export function isNavActive(pathname: string, href: string): boolean {
  // Static export uses trailing slashes, so compare without them
  const path = pathname.replace(/\/$/, '') || '/';
  if (href === '/dashboard') return path === href;
  return path === href || path.startsWith(`${href}/`);
}
