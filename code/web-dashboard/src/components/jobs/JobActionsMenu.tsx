// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import { useState } from 'react';
import Link from 'next/link';
import { Ban, Download, Eye, Film, MoreHorizontal, Pause, Play, RotateCcw, Trash2 } from 'lucide-react';
import { useRouter } from 'next/navigation';
import { Button } from '@/components/ui/button';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { cancelJob, deleteJob, pauseJob, recoverJob, resumeJob } from '@/actions/jobs';
import { useServerAction } from '@/hooks/use-server-action';
import { capabilities } from '@/lib/capabilities';
import { resultsDownloadUrl } from '@/lib/demo';
import {
  canCancelStatus,
  canDeleteStatus,
  canForceRecoverStatus,
  canPauseStatus,
  canRecoverStatus,
  canResumeStatus,
  recoverConfirmPrompt,
} from '@/lib/domain/job-status';
import type { Job } from '@/types/api';
import { jobTitle } from '@/lib/domain/job-title';

/**
 * Everything one can do with a job, in one menu: the jobs table's row actions,
 * and the job page's lifecycle menu (`onDetailPage`: no links to the page or to
 * what its header already shows, plus v2 recovery, and delete leaves the page).
 * Lifecycle actions follow the backend's rules (lib/domain/job-status). The
 * read-only demo shows them disabled.
 */
export function JobActionsMenu({ job, onDetailPage = false }: { job: Job; onDetailPage?: boolean }) {
  const router = useRouter();
  const { run, running } = useServerAction();
  const [confirm, setConfirm] = useState<'cancel' | 'delete' | 'recover' | null>(null);
  const recoverForce = canForceRecoverStatus(job.job_type, job.status);
  const canRecover = onDetailPage && (recoverForce || canRecoverStatus(job.job_type, job.status));
  const recoverPrompt = recoverConfirmPrompt(job.status);
  const doRecover = () =>
    run('recover', () => recoverJob(job.job_id, recoverForce), {
      success: recoverForce ? `Rerunning ${jobTitle(job)} from its latest snapshot` : 'Recovery started',
      error: 'Could not recover the job',
    });
  const name = jobTitle(job);
  const busy = running !== null;
  const completed = job.status === 'COMPLETED';
  const mutable = capabilities.canMutate;

  const lifecycle = [
    canPauseStatus(job.status) && { key: 'pause', label: 'Pause', icon: Pause, onSelect: () => run('pause', () => pauseJob(job.job_id, job.job_type), { success: `Paused ${name}`, error: 'Could not pause the job' }) },
    canResumeStatus(job.status) && { key: 'resume', label: 'Resume', icon: Play, onSelect: () => run('resume', () => resumeJob(job.job_id, job.job_type), { success: `Resumed ${name}`, error: 'Could not resume the job' }) },
    canCancelStatus(job.status) && { key: 'cancel', label: 'Cancel…', icon: Ban, onSelect: () => setConfirm('cancel') },
    canRecover && {
      key: 'recover',
      label: recoverForce ? 'Rerun from snapshot…' : recoverPrompt ? 'Recover…' : 'Recover',
      icon: RotateCcw,
      onSelect: () => (recoverPrompt ? setConfirm('recover') : void doRecover()),
    },
  ].filter(Boolean) as { key: string; label: string; icon: typeof Pause; onSelect: () => void }[];

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button variant="ghost" size="icon-sm" aria-label={`Actions for ${name}`} className="text-fg-muted hover:text-fg" disabled={busy}>
            <MoreHorizontal />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-52 border-border bg-surface-raised">
          {!onDetailPage && (
            <DropdownMenuItem asChild>
              <Link href={`/dashboard/jobs/${job.job_id}`}>
                <Eye /> View details
              </Link>
            </DropdownMenuItem>
          )}
          {completed && !onDetailPage && (
            <>
              <DropdownMenuItem asChild>
                <Link href={`/dashboard/jobs/${job.job_id}/animation`}>
                  <Film /> Open animation
                </Link>
              </DropdownMenuItem>
              <DropdownMenuItem asChild>
                <a href={resultsDownloadUrl(job.job_id, job.job_type)} download>
                  <Download /> Download results
                </a>
              </DropdownMenuItem>
            </>
          )}
          {!onDetailPage && (lifecycle.length > 0 || canDeleteStatus(job.status)) && <DropdownMenuSeparator />}
          {!mutable && (lifecycle.length > 0 || canDeleteStatus(job.status)) && (
            <DropdownMenuLabel className="text-xs font-normal text-fg-subtle">{capabilities.readOnlyReason}</DropdownMenuLabel>
          )}
          {lifecycle.map((a) => (
            <DropdownMenuItem key={a.key} disabled={!mutable} onSelect={a.onSelect}>
              <a.icon /> {a.label}
            </DropdownMenuItem>
          ))}
          {canDeleteStatus(job.status) && (
            <DropdownMenuItem disabled={!mutable} variant="destructive" onSelect={() => setConfirm('delete')}>
              <Trash2 /> Delete…
            </DropdownMenuItem>
          )}
        </DropdownMenuContent>
      </DropdownMenu>

      <ConfirmDialog
        open={confirm === 'cancel'}
        onOpenChange={(o) => !o && setConfirm(null)}
        title={`Cancel ${name}?`}
        description="The job stops and can't be resumed. Finished epochs are kept."
        confirmLabel="Cancel job"
        destructive
        pending={running === 'cancel'}
        onConfirm={async () => {
          if (await run('cancel', () => cancelJob(job.job_id, job.job_type), { success: `Cancelled ${name}`, error: 'Could not cancel the job' })) setConfirm(null);
        }}
      />
      <ConfirmDialog
        open={confirm === 'delete'}
        onOpenChange={(o) => !o && setConfirm(null)}
        title={`Delete ${name}?`}
        description="The job, its snapshots and its results are removed permanently."
        confirmLabel="Delete job"
        destructive
        pending={running === 'delete'}
        onConfirm={async () => {
          if (await run('delete', () => deleteJob(job.job_id), { success: `Deleted ${name}`, error: 'Could not delete the job' })) {
            setConfirm(null);
            if (onDetailPage) router.push('/dashboard/jobs');
          }
        }}
      />
      <ConfirmDialog
        open={confirm === 'recover'}
        onOpenChange={(o) => !o && setConfirm(null)}
        title={recoverForce ? `Rerun ${name}?` : `Recover ${name}?`}
        description={recoverPrompt ?? ''}
        confirmLabel={recoverForce ? 'Rerun' : 'Recover'}
        pending={running === 'recover'}
        onConfirm={async () => {
          if (await doRecover()) setConfirm(null);
        }}
      />
    </>
  );
}
