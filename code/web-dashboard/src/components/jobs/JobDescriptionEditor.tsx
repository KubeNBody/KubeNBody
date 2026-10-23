// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { Button } from '@/components/ui/button';
import { LoadingSpinner } from '@/components/ui/loading-spinner';
import { Pencil, Save, X, AlertCircle } from 'lucide-react';
import { updateJobDescription } from '@/actions/jobs';
import { capabilities } from '@/lib/capabilities';

interface JobDescriptionEditorProps {
  jobId: number;
  initialDescription: string | null | undefined;
}

const MAX_LENGTH = 1000;

export function JobDescriptionEditor({ jobId, initialDescription }: JobDescriptionEditorProps) {
  const router = useRouter();
  const [isEditing, setIsEditing] = useState(false);
  const [value, setValue] = useState(initialDescription ?? '');
  const [savedValue, setSavedValue] = useState<string | null>(initialDescription ?? null);
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const startEdit = () => {
    setValue(savedValue ?? '');
    setError(null);
    setIsEditing(true);
  };

  const cancel = () => {
    setValue(savedValue ?? '');
    setError(null);
    setIsEditing(false);
  };

  const save = async () => {
    setIsSaving(true);
    setError(null);
    try {
      const result = await updateJobDescription(jobId, value.trim() === '' ? null : value);
      if (!result.success) {
        setError(result.error);
        return;
      }
      setSavedValue((result.data as { description?: string | null }).description ?? null);
      setIsEditing(false);
      router.refresh();
    } catch (err) {
      setError((err instanceof Error && err.message) || 'Failed to save description');
    } finally {
      setIsSaving(false);
    }
  };

  if (!isEditing) {
    return (
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1 min-w-0">
          {savedValue ? (
            <p className="text-fg text-sm whitespace-pre-wrap wrap-break-word">{savedValue}</p>
          ) : (
            <p className="text-fg-muted text-sm italic">
              {capabilities.canMutate ? 'No description set. Click edit to add one.' : 'No description set.'}
            </p>
          )}
        </div>
        {capabilities.canMutate && (
          <Button
            variant="ghost"
            size="sm"
            onClick={startEdit}
            className="text-fg-muted hover:text-fg hover:bg-surface-raised shrink-0"
            aria-label="Edit description"
          >
            <Pencil className="h-4 w-4" />
          </Button>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-2">
      <textarea
        value={value}
        onChange={(e) => setValue(e.target.value)}
        rows={4}
        maxLength={MAX_LENGTH}
        autoFocus
        placeholder="Add a note for this job..."
        className="w-full px-3 py-2 bg-surface-raised border border-border-strong rounded-lg text-fg placeholder-fg-subtle focus:ring-2 focus:ring-ring focus:border-transparent resize-y"
      />
      <div className="flex items-center justify-between text-xs text-fg-muted">
        <span>{value.length} / {MAX_LENGTH}</span>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={cancel}
            disabled={isSaving}
            className="border-border-strong text-fg hover:bg-surface-raised"
          >
            <X className="h-4 w-4 mr-1" />
            Cancel
          </Button>
          <Button
            size="sm"
            onClick={save}
            disabled={isSaving || value.length > MAX_LENGTH}
            className="bg-primary hover:bg-primary-hover text-primary-fg"
          >
            {isSaving ? <LoadingSpinner size="sm" className="mr-1" /> : <Save className="h-4 w-4 mr-1" />}
            Save
          </Button>
        </div>
      </div>
      {error && (
        <div className="flex items-center gap-2 px-3 py-2 bg-danger/10 border border-danger/30 rounded text-danger text-sm">
          <AlertCircle className="h-4 w-4 shrink-0" />
          <span>{error}</span>
        </div>
      )}
    </div>
  );
}
