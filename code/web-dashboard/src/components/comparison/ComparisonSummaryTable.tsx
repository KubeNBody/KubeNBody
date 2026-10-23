// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { AlgorithmBadge, IntegratorBadge, JobTypeBadge } from '@/components/jobs/badges';
import { formatValue } from '@/lib/chart-format';
import { driftQuality } from '@/lib/domain/drift';
import { TONE_TEXT } from '@/lib/domain/tones';
import { seriesStyle } from '@/lib/theme/chart';
import type { SingleJobComparisonResult } from '@/types/api';

/** Final divergence and conservation of each compared job. The swatch matches its chart line */
export function ComparisonSummaryTable({
  comparisons,
  referenceEnergyDrift,
  referenceMomentumDrift,
}: {
  comparisons: SingleJobComparisonResult[];
  referenceEnergyDrift: number;
  referenceMomentumDrift: number;
}) {
  if (comparisons.length === 0) {
    return <p className="py-8 text-center text-sm text-fg-muted">No comparable data: every job was skipped (their snapshots do not line up with the reference).</p>;
  }
  return (
    <div className="space-y-3">
      <p className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-fg-muted">
        <span>
          Reference energy drift <span className={`tabular-nums ${TONE_TEXT[driftQuality(referenceEnergyDrift).tone]}`}>{formatValue(referenceEnergyDrift)}%</span>
        </span>
        <span>
          Reference momentum drift <span className="tabular-nums text-fg">{formatValue(referenceMomentumDrift)}%</span>
        </span>
      </p>
      <div className="rounded-md border border-border">
        <Table>
          <TableHeader>
            <TableRow className="border-border hover:bg-transparent">
              <TableHead className="pl-3 text-fg-muted">Job</TableHead>
              <TableHead className="text-fg-muted">Type</TableHead>
              <TableHead className="text-fg-muted">Algorithm</TableHead>
              <TableHead className="text-fg-muted">Integrator</TableHead>
              <TableHead className="text-right text-fg-muted">Position RMSE</TableHead>
              <TableHead className="text-right text-fg-muted">Velocity RMSE</TableHead>
              <TableHead className="text-right text-fg-muted">Energy drift</TableHead>
              <TableHead className="pr-3 text-right text-fg-muted">Momentum drift</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {comparisons.map((c, i) => {
              const style = seriesStyle(i);
              return (
                <TableRow key={c.job_id} className="border-border hover:bg-surface-raised">
                  <TableCell className="pl-3">
                    <span className="flex items-center gap-2 font-medium tabular-nums text-fg">
                      <svg width="14" height="4" aria-hidden>
                        <line x1="0" x2="14" y1="2" y2="2" stroke={style.color} strokeWidth="3" strokeDasharray={style.dash} />
                      </svg>
                      #{c.job_id}
                    </span>
                  </TableCell>
                  <TableCell>
                    <JobTypeBadge jobType={c.job_info.job_type} />
                  </TableCell>
                  <TableCell>
                    <AlgorithmBadge algorithm={c.job_info.force_algorithm} />
                  </TableCell>
                  <TableCell>
                    <IntegratorBadge method={c.job_info.integration_method} />
                  </TableCell>
                  <TableCell className="text-right tabular-nums text-fg-muted">{formatValue(c.position_rmse_final)}</TableCell>
                  <TableCell className="text-right tabular-nums text-fg-muted">{formatValue(c.velocity_rmse_final)}</TableCell>
                  <TableCell className={`text-right tabular-nums ${TONE_TEXT[driftQuality(c.energy_drift_comparison).tone]}`}>
                    {formatValue(c.energy_drift_comparison)}%
                  </TableCell>
                  <TableCell className="pr-3 text-right tabular-nums text-fg-muted">{formatValue(c.momentum_drift_comparison)}%</TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}
