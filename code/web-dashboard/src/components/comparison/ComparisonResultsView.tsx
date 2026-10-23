// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import { AlertTriangle } from 'lucide-react';
import { jobWithLabel, labelsFromResults } from '@/lib/domain/comparison-labels';
import type { JobToJobComparisonResponse } from '@/types/api';
import { ComparisonSummaryTable } from './ComparisonSummaryTable';
import { EnergyComparisonChart } from './EnergyComparisonChart';
import { MomentumComparisonChart } from './MomentumComparisonChart';
import { TrajectoryDivergenceChart } from './TrajectoryDivergenceChart';

function Section({ title, description, children }: { title: string; description?: string; children: React.ReactNode }) {
  return (
    <section className="print-section space-y-3">
      <div>
        <h2 className="text-base font-semibold text-fg">{title}</h2>
        {description && <p className="text-sm text-fg-muted">{description}</p>}
      </div>
      {children}
    </section>
  );
}

/** A comparison's results, the same on the compare page and a saved comparison */
export function ComparisonResultsView({ results }: { results: JobToJobComparisonResponse }) {
  const { comparisons, skipped_jobs: skipped, reference_job_id: ref } = results;
  const labels = labelsFromResults(results);
  return (
    <div className="space-y-8">
      {skipped.length > 0 && (
        <div role="status" className="flex items-start gap-2 rounded-md border border-warning/30 bg-warning/10 p-3 text-sm">
          <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden />
          <div>
            <p className="font-medium text-warning">Skipped {skipped.length === 1 ? 'one job' : `${skipped.length} jobs`}</p>
            {skipped.map((s) => (
              <p key={s.job_id} className="text-fg-muted">
                #{s.job_id}: {s.reason}
              </p>
            ))}
          </div>
        </div>
      )}
      <Section title="Summary" description={`Each job against reference ${jobWithLabel(ref, labels.reference)}, at the last common snapshot`}>
        <ComparisonSummaryTable
          comparisons={comparisons}
          referenceEnergyDrift={comparisons[0]?.energy_drift_reference ?? 0}
          referenceMomentumDrift={comparisons[0]?.momentum_drift_reference ?? 0}
        />
      </Section>
      {comparisons.length > 0 && (
        <>
          <Section title="Trajectory divergence" description="How far each run's bodies drift from the reference's">
            <TrajectoryDivergenceChart comparisons={comparisons} labels={labels} />
          </Section>
          <Section title="Energy conservation">
            <EnergyComparisonChart comparisons={comparisons} referenceJobId={ref} labels={labels} />
          </Section>
          <Section title="Momentum conservation">
            <MomentumComparisonChart comparisons={comparisons} referenceJobId={ref} labels={labels} />
          </Section>
        </>
      )}
    </div>
  );
}
