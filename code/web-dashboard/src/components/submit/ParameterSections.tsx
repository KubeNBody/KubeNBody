// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import { useState } from 'react';
import { ChevronDown } from 'lucide-react';
import { useFormContext, useWatch } from 'react-hook-form';
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible';
import { FormField, NativeSelect, fieldClass } from '@/components/ui/form-field';
import {
  PREDICTOR_META,
  PREDICTOR_RUNGS,
  composePredictorMethod,
  effectiveOrder,
  supportsPredictor,
} from '@/lib/format-integration';
import { cn } from '@/lib/utils';
import { DEFAULT_PACKAGE_SIZE, isDistributed } from './build-request';
import { INTEGRATION_METHOD_HELP, JOB_TYPE_HELP, optionalNumber, parseNumber, type JobFormData } from './job-form';

// The submit form's parameter sections. They read the form through
// react-hook-form's context (the page wraps them in a FormProvider).

const numberInput = { type: 'text', inputMode: 'decimal' as const, autoComplete: 'off', spellCheck: false };

function Section({ title, description, children }: { title: string; description?: string; children: React.ReactNode }) {
  return (
    <section className="space-y-4 rounded-lg border border-border bg-surface p-5">
      <div>
        <h2 className="text-sm font-semibold text-fg">{title}</h2>
        {description && <p className="mt-0.5 text-xs text-fg-muted">{description}</p>}
      </div>
      {children}
    </section>
  );
}

function Checkbox({ id, label, help, ...props }: { id: string; label: string; help?: string } & React.ComponentProps<'input'>) {
  return (
    <div className="flex items-start gap-3 rounded-md border border-border bg-surface-raised p-3">
      <input id={id} type="checkbox" className="mt-0.5 size-4" aria-describedby={help ? `${id}-help` : undefined} {...props} />
      <div>
        <label htmlFor={id} className="text-sm font-medium text-fg">
          {label}
        </label>
        {help && (
          <p id={`${id}-help`} className="text-xs text-fg-muted">
            {help}
          </p>
        )}
      </div>
    </div>
  );
}

export function ExecutionSection() {
  const { register, setValue, getValues, formState } = useFormContext<JobFormData>();
  const [jobType, integrationMethod, backgroundPredictor, partitionStrategy] = useWatch<
    JobFormData,
    ['job_type', 'integration_method', 'background_predictor', 'partition_strategy']
  >({ name: ['job_type', 'integration_method', 'background_predictor', 'partition_strategy'] });
  const distributed = isDistributed(jobType);
  const showPredictor = distributed && supportsPredictor(integrationMethod);

  return (
    <Section title="Execution" description="Where and how the simulation runs">
      <FormField id="job_type" label="Job type" help={JOB_TYPE_HELP[jobType]}>
        {(p) => (
          <NativeSelect
            {...p}
            {...register('job_type', {
              // Each type's package size: none for monolithic, its own default otherwise
              onChange: (e) => {
                const t = e.target.value as JobFormData['job_type'];
                if (t === 'monolithic') setValue('package_size', undefined);
                else if (getValues('package_size') === undefined) setValue('package_size', DEFAULT_PACKAGE_SIZE[t]);
              },
            })}
          >
            <option value="monolithic">Monolithic (one worker)</option>
            <option value="distributed">Distributed v1 (RabbitMQ)</option>
            <option value="distributed_v2">Distributed v2 (Redis, recoverable)</option>
          </NativeSelect>
        )}
      </FormField>

      <Checkbox
        id="use_gpu"
        label="Use GPU"
        help="Needs a GPU worker. The GPU has direct summation only."
        {...register('use_gpu', {
          onChange: (e) => {
            if (!e.target.checked) return;
            setValue('force_algorithm', 'direct');
          },
        })}
      />

      {distributed && (
        <FormField id="package_size" label="Package size" help="Bodies per work package" error={formState.errors.package_size?.message}>
          {(p) => <input {...p} {...numberInput} className={fieldClass} {...register('package_size', { setValueAs: optionalNumber })} />}
        </FormField>
      )}

      {showPredictor && (
        <FormField
          id="background_predictor"
          label="Background predictor"
          help={
            <>
              How the other packages&apos; bodies are extrapolated to each sub-stage time. Higher rungs raise the global
              order. Submitted as <span className="font-mono text-fg">{composePredictorMethod(integrationMethod, backgroundPredictor)}</span>.
            </>
          }
        >
          {(p) => (
            <NativeSelect {...p} {...register('background_predictor')}>
              {(PREDICTOR_RUNGS[integrationMethod] ?? []).map((rung) => (
                <option key={rung} value={rung}>
                  {PREDICTOR_META[rung].label} — order {effectiveOrder(integrationMethod, rung)} ({PREDICTOR_META[rung].formula})
                </option>
              ))}
            </NativeSelect>
          )}
        </FormField>
      )}

      {jobType === 'distributed_v2' && (
        <FormField
          id="partition_strategy"
          label="Package partitioning"
          help={
            {
              linear: 'Packages are contiguous slices of the input file (no reordering).',
              random: 'Shuffles bodies once (seeded): balanced packages, decorrelated from file order.',
              spatial: 'Morton (Z-order) sort: each package is a compact region, so close pairs stay together.',
              orb: 'Recursive bisection: one axis-aligned box per package. Helps predicted RK4 / Forest–Ruth when close encounters are resolved. With large softening, prefer Linear.',
            }[partitionStrategy]
          }
        >
          {(p) => (
            <NativeSelect {...p} {...register('partition_strategy')}>
              <option value="linear">Linear (file order)</option>
              <option value="random">Random (seeded shuffle)</option>
              <option value="spatial">Spatial (Morton / Z-order)</option>
              <option value="orb">ORB (recursive bisection)</option>
            </NativeSelect>
          )}
        </FormField>
      )}
      {jobType === 'distributed_v2' && partitionStrategy === 'random' && (
        <FormField id="partition_seed" label="Shuffle seed" help="Same seed and input give the same partition.">
          {(p) => (
            <input
              {...p}
              {...numberInput}
              className={fieldClass}
              // A cleared field means seed 0 (not NaN, which would block submit)
              {...register('partition_seed', { setValueAs: (v) => (Number.isFinite(parseNumber(v)) ? parseNumber(v) : 0) })}
            />
          )}
        </FormField>
      )}
    </Section>
  );
}

export function PhysicsSection() {
  const { register, setValue, getValues, formState } = useFormContext<JobFormData>();
  const [forceAlgorithm, integrationMethod, theta, useGpu] = useWatch<
    JobFormData,
    ['force_algorithm', 'integration_method', 'barnes_hut_theta', 'use_gpu']
  >({
    name: ['force_algorithm', 'integration_method', 'barnes_hut_theta', 'use_gpu'],
  });
  const e = formState.errors;

  return (
    <Section title="Physics" description="Time integration and forces">
      <div className="grid gap-4 sm:grid-cols-2">
        <FormField id="num_epochs" label="Epochs" help="Time steps to simulate" error={e.num_epochs?.message}>
          {(p) => <input {...p} {...numberInput} className={fieldClass} {...register('num_epochs', { setValueAs: parseNumber })} />}
        </FormField>
        <FormField id="time_step" label="Time step (dt)" help="Up to 1.0, e.g. 1e-4" error={e.time_step?.message}>
          {(p) => <input {...p} {...numberInput} className={fieldClass} {...register('time_step', { setValueAs: parseNumber })} />}
        </FormField>
        <FormField id="snapshot_interval" label="Snapshot interval" help="Save a snapshot every N epochs" error={e.snapshot_interval?.message}>
          {(p) => <input {...p} {...numberInput} className={fieldClass} {...register('snapshot_interval', { setValueAs: parseNumber })} />}
        </FormField>
        <FormField id="softening_sq" label="Softening (ε²)" help="Gravitational softening, squared" error={e.softening_sq?.message}>
          {(p) => <input {...p} {...numberInput} className={fieldClass} {...register('softening_sq', { setValueAs: optionalNumber })} />}
        </FormField>
      </div>

      <FormField
        id="integration_method"
        label="Integrator"
        help={INTEGRATION_METHOD_HELP[integrationMethod]}
      >
        {(p) => (
          <NativeSelect
            {...p}
            {...register('integration_method', {
              // Keep the predictor rung valid for the new base (back to linear if it lacks it)
              onChange: (ev) => {
                const rungs = PREDICTOR_RUNGS[ev.target.value];
                if (rungs && !rungs.includes(getValues('background_predictor'))) setValue('background_predictor', 'linear');
              },
            })}
          >
            <option value="symplectic_euler">Symplectic Euler — order 1, symplectic</option>
            <option value="velocity_verlet">Velocity Verlet — order 2, symplectic</option>
            <option value="simple_euler">Simple Euler — order 1</option>
            <option value="rk4">RK4 — order 4</option>
            <option value="forest_ruth">Forest–Ruth — order 4, symplectic</option>
          </NativeSelect>
        )}
      </FormField>

      <FormField
        id="force_algorithm"
        label="Force algorithm"
        error={e.force_algorithm?.message}
        help={
          forceAlgorithm === 'barnes_hut'
            ? 'Octree approximation of distant bodies: O(N log N), approximate. For large N.'
            : 'Every body pair: O(N²), exact. Best below ~1000 bodies.'
        }
      >
        {(p) => (
          <NativeSelect {...p} {...register('force_algorithm')}>
            <option value="direct">Direct — exact, O(N²)</option>
            <option value="barnes_hut" disabled={useGpu}>
              Barnes–Hut — approximate, O(N log N){useGpu ? ' (CPU only)' : ''}
            </option>
          </NativeSelect>
        )}
      </FormField>
      {forceAlgorithm === 'barnes_hut' && (
        <FormField id="barnes_hut_theta" label={`Opening angle θ = ${(theta ?? 0.5).toFixed(1)}`} help="Lower is more accurate and slower (0.3–0.5 typical).">
          {(p) => (
            <input {...p} type="range" min="0.1" max="2.0" step="0.1" className="w-full" {...register('barnes_hut_theta', { setValueAs: optionalNumber })} />
          )}
        </FormField>
      )}
    </Section>
  );
}

export function AdvancedSection() {
  const { register, formState } = useFormContext<JobFormData>();
  const adaptive = useWatch<JobFormData, 'adaptive_enabled'>({ name: 'adaptive_enabled' });
  const [open, setOpen] = useState(false);
  // An invalid field in here must not hide behind the collapsed section
  const hasError = Boolean(formState.errors.adaptive_eta || formState.errors.job_description);

  return (
    <Collapsible open={open || hasError} onOpenChange={setOpen} className="rounded-lg border border-border bg-surface">
      <CollapsibleTrigger className="flex w-full items-center justify-between rounded-lg px-5 py-4 text-left">
        <span>
          <span className="block text-sm font-semibold text-fg">Advanced</span>
          <span className="text-xs text-fg-muted">Adaptive time stepping and a note for this job</span>
        </span>
        <ChevronDown className={cn('size-4 text-fg-muted transition-transform', open && 'rotate-180')} aria-hidden />
      </CollapsibleTrigger>
      <CollapsibleContent className="space-y-4 px-5 pb-5">
        <Checkbox
          id="adaptive_enabled"
          label="Adaptive time stepping"
          help="Shrinks dt during close encounters (softening-length criterion, dt = η·sqrt(ε/a_max))."
          {...register('adaptive_enabled')}
        />
        {adaptive && (
          <div className="grid gap-4 sm:grid-cols-3">
            <FormField id="adaptive_eta" label="Safety factor η" help="0.001–0.5, default 0.02" error={formState.errors.adaptive_eta?.message}>
              {(p) => <input {...p} {...numberInput} className={fieldClass} {...register('adaptive_eta', { setValueAs: optionalNumber })} />}
            </FormField>
            <FormField id="adaptive_dt_min" label="Min dt" help="Optional (default 1e-10)">
              {(p) => <input {...p} {...numberInput} placeholder="1e-10" className={fieldClass} {...register('adaptive_dt_min', { setValueAs: optionalNumber })} />}
            </FormField>
            <FormField id="adaptive_dt_max" label="Max dt" help="Optional (default: the time step)">
              {(p) => <input {...p} {...numberInput} placeholder="dt" className={fieldClass} {...register('adaptive_dt_max', { setValueAs: optionalNumber })} />}
            </FormField>
          </div>
        )}
        <FormField
          id="job_description"
          label="Job note"
          help="Free text kept with this job (up to 1000 characters), editable later on the job page."
          error={formState.errors.job_description?.message}
        >
          {(p) => (
            <textarea
              {...p}
              rows={3}
              maxLength={1000}
              placeholder="E.g. baseline run, high-ε² stability test, compare with #42…"
              className="w-full resize-y rounded-md border border-border-strong bg-surface-raised px-3 py-2 text-sm text-fg placeholder:text-fg-subtle"
              {...register('job_description')}
            />
          )}
        </FormField>
      </CollapsibleContent>
    </Collapsible>
  );
}
