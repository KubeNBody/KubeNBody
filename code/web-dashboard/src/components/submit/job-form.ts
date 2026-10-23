// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import { z } from 'zod';
import { JOB_NAME_MAX } from '@/lib/domain/job-title';

// Submit form: schema and field helpers (shared by the page and its sections)
const num = () => z.number({ invalid_type_error: 'Enter a number', required_error: 'Required' });

export const jobSchema = z.object({
  num_epochs: num().int('Must be a whole number').min(1, 'Must be at least 1'),
  package_size: num().int('Must be a whole number').min(1, 'Must be at least 1').optional(),
  time_step: num().positive('Must be positive').max(1.0, 'Must be at most 1.0'),
  snapshot_interval: num().int('Must be a whole number').min(1, 'Must be at least 1'),
  job_type: z.enum(['distributed', 'monolithic', 'distributed_v2']),
  use_gpu: z.boolean().default(false),
  // Algorithm selection (for monolithic and distributed_v2)
  force_algorithm: z.enum(['direct', 'barnes_hut']).default('direct'),
  barnes_hut_theta: z.number().min(0.1).max(2.0).default(0.5),
  softening_sq: z.number().positive().default(1.0),
  // Integration method (base integrator). For distributed jobs with a
  // multi-stage integrator this is combined with `background_predictor` at
  // submit time to form a g-named method (e.g. rk4 + accel -> grk4_accel).
  integration_method: z.enum(['simple_euler', 'symplectic_euler', 'velocity_verlet', 'rk4', 'forest_ruth']).default('symplectic_euler'),
  // Background-predictor rung for distributed multi-stage integrators.
  background_predictor: z.enum(['frozen', 'linear', 'accel']).default('linear'),
  // Package partitioning (distributed_v2). How bodies are permuted once at job
  // creation so contiguous packages take a given shape: linear (file order),
  // random (seeded shuffle), spatial (Morton/Z-order locality) or orb
  // (orthogonal recursive bisection).
  partition_strategy: z.enum(['linear', 'random', 'spatial', 'orb']).default('linear'),
  partition_seed: z.number().int().min(0).default(0),
  // Adaptive timestepping
  adaptive_enabled: z.boolean().default(false),
  adaptive_eta: z.number().min(0.001).max(0.5).default(0.02).optional(),
  adaptive_dt_min: z.number().positive().optional(),
  adaptive_dt_max: z.number().positive().optional(),
  // Optional user-supplied free-text note for this job
  job_description: z.string().max(1000).optional(),
  // Optional short title shown instead of the input file name
  job_name: z.string().max(JOB_NAME_MAX).optional(),
}).refine((job) => !(job.use_gpu && job.force_algorithm === 'barnes_hut'), {
  // the GPU backend has direct summation only (the API refuses the combination)
  message: 'Barnes–Hut runs on CPU only. Choose Direct or untick GPU',
  path: ['force_algorithm'],
});

export type JobFormData = z.infer<typeof jobSchema>;

// For optional numeric inputs: valueAsNumber turns a blank field into NaN, which
// z.number().optional() rejects (and .default() doesn't replace), blocking submit
export const optionalNumber = (v: unknown) => (v === '' || v == null ? undefined : parseNumber(v));

/**
 * A number typed into a text field: accepts a decimal comma ("0,001", as a
 * Polish-locale keyboard gives) and exponents ("1e-5"). NaN for anything else,
 * which the schema reports as "Enter a number". The form uses text inputs with
 * inputMode="decimal" rather than type="number", which shows the value in the
 * browser's locale and silently drops what it can't parse.
 */
export const parseNumber = (v: unknown): number => {
  if (typeof v === 'number') return v;
  const text = String(v ?? '').trim().replace(',', '.');
  return text === '' || !/^[-+]?(\d+\.?\d*|\.\d+)(e[-+]?\d+)?$/i.test(text) ? NaN : Number(text);
};

// Help text under the job type and integration method selects
export const JOB_TYPE_HELP: Record<JobFormData['job_type'], string> = {
  distributed: 'Best for large N (many bodies), splits work across multiple workers',
  monolithic: 'Best for large epochs (100K+), processes all epochs in one worker',
  distributed_v2: 'V2 architecture uses Redis for faster synchronization and automatic crash recovery.',
};

export const INTEGRATION_METHOD_HELP: Record<JobFormData['integration_method'], string> = {
  symplectic_euler: 'Symplectic Euler: Updates velocity first, then position with new velocity. Better energy conservation for long simulations.',
  velocity_verlet: 'Velocity Verlet: 2nd-order symplectic integrator. Increased energy conservation and accuracy.',
  rk4: 'RK4: 4th-order Runge-Kutta integrator. Highest accuracy per step at the cost of 4 force evaluations per epoch.',
  forest_ruth: 'Forest-Ruth: 4th-order symplectic integrator. Same order as RK4, with an energy error that stays bounded at a fixed timestep. 3 force evaluations per epoch.',
  simple_euler: 'Simple Euler: Updates position first with old velocity, then velocity. Simpler but may drift energy over time.',
};
