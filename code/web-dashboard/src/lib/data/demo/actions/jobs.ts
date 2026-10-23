// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
// Demo implementation of JobActions (../../types), swapped in for actions/jobs.ts
// by next.config.js. Static export has no server actions: these run in the
// browser and refuse every job mutation.
import { DEMO_READ_ONLY_MESSAGE } from '@/lib/demo';
import type { JobActions, MutationResult } from '../../types';

const readOnly = async (): Promise<MutationResult> => ({ success: false, error: DEMO_READ_ONLY_MESSAGE });

export const cancelJob: JobActions['cancelJob'] = readOnly;
export const pauseJob: JobActions['pauseJob'] = readOnly;
export const resumeJob: JobActions['resumeJob'] = readOnly;
export const deleteJob: JobActions['deleteJob'] = readOnly;
export const recoverJob: JobActions['recoverJob'] = readOnly;
export const updateJobDescription: JobActions['updateJobDescription'] = readOnly;
export const updateJobName: JobActions['updateJobName'] = readOnly;
