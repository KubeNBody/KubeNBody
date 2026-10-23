// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
// What this build of the dashboard can do. Components ask these instead of
// checking the demo flag themselves (scripts/check-classes.mjs keeps IS_DEMO out
// of components): the static demo is read-only, with recorded data.
import { DEMO_PASSWORD, DEMO_READ_ONLY_MESSAGE, DEMO_USERNAME, IS_DEMO } from '@/lib/demo';

export const capabilities = {
  /** Submit, pause/resume/cancel/recover/delete jobs. Edit or delete files and comparisons */
  canMutate: !IS_DEMO,
  /** Data that changes while you look (auto-refresh, polling make sense) */
  liveData: !IS_DEMO,
  /** Why a disabled action is disabled (null when nothing is) */
  readOnlyReason: IS_DEMO ? DEMO_READ_ONLY_MESSAGE : null,
  /** What the health indicator calls a reachable data source */
  dataSourceLabel: IS_DEMO ? 'Demo data' : 'Connected',
  /** Credentials to prefill on the login page (the demo's public ones) */
  prefilledLogin: IS_DEMO ? { username: DEMO_USERNAME, password: DEMO_PASSWORD } : null,
} as const;
