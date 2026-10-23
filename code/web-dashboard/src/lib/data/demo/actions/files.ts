// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
// Demo implementation of FileActions (../../types), swapped in for
// actions/files.ts by next.config.js. Runs in the browser. Refuses every edit.
import { DEMO_READ_ONLY_MESSAGE } from '@/lib/demo';
import type { FileActions, MutationResult } from '../../types';

const readOnly = async (): Promise<MutationResult> => ({ success: false, error: DEMO_READ_ONLY_MESSAGE });

export const updateFile: FileActions['updateFile'] = readOnly;
export const deleteFile: FileActions['deleteFile'] = readOnly;
