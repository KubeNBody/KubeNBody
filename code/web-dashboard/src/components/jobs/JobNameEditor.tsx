// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { AlertCircle, Check, Pencil, X } from 'lucide-react';
import { updateJobName } from '@/actions/jobs';
import { Button } from '@/components/ui/button';
import { LoadingSpinner } from '@/components/ui/loading-spinner';
import { capabilities } from '@/lib/capabilities';
import { JOB_NAME_MAX, jobTitle } from '@/lib/domain/job-title';

/** The job page title: the job's name (or its input file's), renamed in place */
export function JobNameEditor({ jobId, name, filename }: { jobId: number; name: string | null | undefined; filename: string | null | undefined }) {
  const router = useRouter();
  const [saved, setSaved] = useState<string | null>(name ?? null);
  const [value, setValue] = useState(name ?? '');
  const [isEditing, setIsEditing] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const cancel = () => {
    setValue(saved ?? '');
    setError(null);
    setIsEditing(false);
  };

  const save = async () => {
    setIsSaving(true);
    setError(null);
    try {
      const result = await updateJobName(jobId, value);
      if (!result.success) {
        setError(result.error);
        return;
      }
      setSaved((result.data as { name?: string | null }).name ?? null);
      setIsEditing(false);
      router.refresh();
    } catch (err) {
      setError((err instanceof Error && err.message) || 'Failed to save the name');
    } finally {
      setIsSaving(false);
    }
  };

  if (!isEditing) {
    return (
      <span className="inline-flex items-center gap-1">
        <span className="break-all">{jobTitle({ job_id: jobId, name: saved, filename })}</span>
        {capabilities.canMutate && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setIsEditing(true)}
            className="shrink-0 text-fg-muted hover:bg-surface-raised hover:text-fg"
            aria-label={saved ? 'Rename job' : 'Name this job'}
          >
            <Pencil className="size-4" />
          </Button>
        )}
      </span>
    );
  }

  return (
    <span className="block space-y-2">
      <span className="flex items-center gap-2">
        <input
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') save();
            if (e.key === 'Escape') cancel();
          }}
          maxLength={JOB_NAME_MAX}
          autoFocus
          placeholder={filename || `Job ${jobId}`}
          aria-label="Job name"
          className="min-w-0 flex-1 rounded-md border border-border-strong bg-surface-raised px-3 py-1.5 text-base font-normal text-fg placeholder:text-fg-subtle focus:border-transparent focus:ring-2 focus:ring-ring"
        />
        <Button size="sm" onClick={save} disabled={isSaving} aria-label="Save name">
          {isSaving ? <LoadingSpinner size="sm" /> : <Check className="size-4" />}
        </Button>
        <Button variant="outline" size="sm" onClick={cancel} disabled={isSaving} aria-label="Cancel renaming" className="border-border-strong text-fg hover:bg-surface-raised">
          <X className="size-4" />
        </Button>
      </span>
      <span className="block text-xs font-normal text-fg-muted">Leave empty to show the input file name.</span>
      {error && (
        <span className="flex items-center gap-2 rounded border border-danger/30 bg-danger/10 px-3 py-2 text-sm font-normal text-danger">
          <AlertCircle className="size-4 shrink-0" />
          {error}
        </span>
      )}
    </span>
  );
}
