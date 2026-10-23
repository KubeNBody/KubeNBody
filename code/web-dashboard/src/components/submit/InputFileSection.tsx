// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import { useCallback, useState } from 'react';
import { useDropzone, type FileRejection } from 'react-dropzone';
import { AlertCircle, CheckCircle, ChevronsUpDown, FileText, Library, Upload, X } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from '@/components/ui/command';
import { FormField, fieldClass } from '@/components/ui/form-field';
import { LoadingSpinner } from '@/components/ui/loading-spinner';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group';
import { useFileValidation, useInputFiles } from '@/hooks/use-api';
import { cn, formatCount, formatFileSize } from '@/lib/utils';
import type { FileSource, FileSourceAction } from './file-source';
import { FileFormatHelp } from './FileFormatHelp';

export const MAX_UPLOAD_BYTES = 100 * 1024 * 1024;
export const UPLOAD_ACCEPT = { 'text/csv': ['.csv'], 'text/plain': ['.txt', '.dat'] };

/** Upload a new body file (checked in the browser first) or pick one from the library */
export function InputFileSection({ source, dispatch }: { source: FileSource; dispatch: (a: FileSourceAction) => void }) {
  return (
    <section className="space-y-4 rounded-lg border border-border bg-surface p-5">
      <div>
        <h2 className="text-sm font-semibold text-fg">Input file</h2>
        <p className="mt-0.5 text-xs text-fg-muted">Initial positions, velocities and masses of the bodies</p>
      </div>
      <ToggleGroup
        type="single"
        value={source.kind}
        onValueChange={(v) => v && dispatch({ type: 'mode', kind: v as FileSource['kind'] })}
        className="grid w-full grid-cols-2 rounded-md border border-border bg-surface-raised p-1"
        aria-label="Input file source"
      >
        <ToggleGroupItem value="upload" className="text-fg-muted data-[state=on]:bg-primary data-[state=on]:text-primary-fg">
          <Upload /> Upload new
        </ToggleGroupItem>
        <ToggleGroupItem value="existing" className="text-fg-muted data-[state=on]:bg-primary data-[state=on]:text-primary-fg">
          <Library /> From library
        </ToggleGroupItem>
      </ToggleGroup>

      {source.kind === 'upload' ? <UploadPanel source={source} dispatch={dispatch} /> : <LibraryPicker fileId={source.fileId} dispatch={dispatch} />}
      <FileFormatHelp />
    </section>
  );
}

function UploadPanel({ source, dispatch }: { source: Extract<FileSource, { kind: 'upload' }>; dispatch: (a: FileSourceAction) => void }) {
  const validator = useFileValidation();

  const onDrop = useCallback(
    async (accepted: File[]) => {
      const file = accepted[0];
      if (!file) return;
      dispatch({ type: 'picked', file });
      try {
        dispatch({ type: 'validated', file, validation: await validator.mutateAsync(file) });
      } catch {
        dispatch({ type: 'validated', file, validation: { valid: false, error: 'Could not read the file' } });
      }
    },
    [dispatch, validator]
  );
  // Rejected drops (too large, wrong type) would otherwise be silent
  const onDropRejected = useCallback(
    (rejections: FileRejection[]) => {
      const code = rejections[0]?.errors[0]?.code;
      dispatch({
        type: 'rejected',
        error:
          code === 'file-too-large'
            ? `The file is larger than ${MAX_UPLOAD_BYTES / 2 ** 20} MB`
            : code === 'file-invalid-type'
              ? 'Unsupported file type (use .csv, .txt or .dat)'
              : rejections[0]?.errors[0]?.message || 'File rejected',
      });
    },
    [dispatch]
  );
  const { getRootProps, getInputProps, isDragActive, isDragReject } = useDropzone({
    onDrop,
    onDropRejected,
    accept: UPLOAD_ACCEPT,
    multiple: false,
    maxSize: MAX_UPLOAD_BYTES,
  });

  if (!source.file) {
    return (
      <>
        <div
          {...getRootProps()}
          className={cn(
            'cursor-pointer rounded-lg border-2 border-dashed px-6 py-10 text-center transition-colors',
            isDragReject ? 'border-danger bg-danger/10' : isDragActive ? 'border-primary bg-primary/10' : 'border-border-strong hover:border-fg-subtle hover:bg-surface-raised'
          )}
        >
          <input {...getInputProps()} aria-label="Choose a body file" />
          <FileText className={cn('mx-auto mb-3 size-10', isDragActive ? 'text-link' : 'text-fg-subtle')} aria-hidden />
          <p className="font-medium text-fg">{isDragActive ? (isDragReject ? 'Unsupported file' : 'Drop the file') : 'Drop a body file here, or click to choose'}</p>
          <p className="text-sm text-fg-muted">.csv, .txt or .dat, up to {MAX_UPLOAD_BYTES / 2 ** 20} MB</p>
        </div>
        {source.validation && !source.validation.valid && <ValidationResult validation={source.validation} />}
      </>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3 rounded-md bg-surface-raised p-3">
        <div className="flex min-w-0 items-center gap-3">
          <FileText className="size-5 shrink-0 text-link" aria-hidden />
          <div className="min-w-0">
            <p className="truncate font-medium text-fg">{source.file.name}</p>
            <p className="text-sm text-fg-muted">{formatFileSize(source.file.size)}</p>
          </div>
        </div>
        <Button variant="ghost" size="icon-sm" onClick={() => dispatch({ type: 'cleared' })} aria-label="Remove file" className="text-fg-muted hover:text-fg">
          <X />
        </Button>
      </div>

      {source.validating ? (
        <p className="flex items-center gap-2 text-sm text-fg-muted" role="status">
          <LoadingSpinner size="sm" /> Checking the file…
        </p>
      ) : (
        source.validation && <ValidationResult validation={source.validation} />
      )}

      <FormField id="file_name" label="Library name (optional)" help="Leave blank to use the file name. The file is kept in the library for reuse.">
        {(p) => (
          <input
            {...p}
            className={fieldClass}
            placeholder={source.file!.name.replace(/\.[^.]+$/, '')}
            value={source.name}
            onChange={(e) => dispatch({ type: 'meta', name: e.target.value })}
          />
        )}
      </FormField>
      <FormField id="file_description" label="File description (optional)">
        {(p) => (
          <input
            {...p}
            className={fieldClass}
            placeholder="E.g. two disk galaxies, 602 bodies"
            value={source.description}
            onChange={(e) => dispatch({ type: 'meta', description: e.target.value })}
          />
        )}
      </FormField>
    </div>
  );
}

function ValidationResult({ validation }: { validation: NonNullable<Extract<FileSource, { kind: 'upload' }>['validation']> }) {
  const Icon = validation.valid ? CheckCircle : AlertCircle;
  return (
    <div
      role={validation.valid ? 'status' : 'alert'}
      className={cn('flex items-start gap-2 rounded-md border p-3 text-sm', validation.valid ? 'border-success/30 bg-success/10' : 'border-danger/30 bg-danger/10')}
    >
      <Icon className={cn('mt-0.5 size-4 shrink-0', validation.valid ? 'text-success' : 'text-danger')} aria-hidden />
      <div>
        <p className={cn('font-medium', validation.valid ? 'text-success' : 'text-danger')}>
          {validation.valid ? 'File looks good' : 'This file cannot be used'}
        </p>
        {validation.error && <p className="text-danger">{validation.error}</p>}
        {validation.preview && <p className="text-fg-muted">{formatCount(validation.preview.totalRows)} bodies</p>}
      </div>
    </div>
  );
}

function LibraryPicker({ fileId, dispatch }: { fileId: number | null; dispatch: (a: FileSourceAction) => void }) {
  const { data, isLoading, isError, error } = useInputFiles();
  const [open, setOpen] = useState(false);
  const files = data?.files ?? [];
  const selected = files.find((f) => f.file_id === fileId);

  if (isLoading) {
    return (
      <p className="flex items-center gap-2 text-sm text-fg-muted" role="status">
        <LoadingSpinner size="sm" /> Loading the library…
      </p>
    );
  }
  if (isError) {
    return (
      <p className="rounded-md border border-danger/30 bg-danger/10 p-3 text-sm text-danger" role="alert">
        Could not load the library: {error?.message || 'try reloading the page'}
      </p>
    );
  }
  if (files.length === 0) {
    return <p className="rounded-md border border-border bg-surface-raised p-3 text-sm text-fg-muted">The library is empty: upload a file first.</p>;
  }

  return (
    <div className="space-y-3">
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <Button variant="outline" role="combobox" aria-expanded={open} aria-label="Library file" className="h-auto w-full justify-between py-2 text-left">
            <span className="min-w-0">
              {selected ? (
                <>
                  <span className="block truncate text-fg">{selected.user_friendly_name}</span>
                  <span className="block text-xs text-fg-muted">
                    {formatCount(selected.num_bodies)} bodies · {formatFileSize(selected.file_size)}
                  </span>
                </>
              ) : (
                <span className="text-fg-muted">Choose a file…</span>
              )}
            </span>
            <ChevronsUpDown className="text-fg-subtle" />
          </Button>
        </PopoverTrigger>
        <PopoverContent className="w-(--radix-popover-trigger-width) border-border bg-surface-raised p-0" align="start">
          {/* Substring match: cmdk's default fuzzy score matches scattered letters ("goggles" in "galaxies") */}
          <Command filter={(value, search) => (value.toLowerCase().includes(search.trim().toLowerCase()) ? 1 : 0)}>
            <CommandInput placeholder="Search the library…" />
            <CommandList>
              <CommandEmpty>No file matches.</CommandEmpty>
              <CommandGroup>
                {files.map((f) => (
                  <CommandItem
                    key={f.file_id}
                    value={`${f.user_friendly_name} ${f.original_filename} ${f.description ?? ''} ${f.file_id}`}
                    onSelect={() => {
                      dispatch({ type: 'existing', fileId: f.file_id });
                      setOpen(false);
                    }}
                  >
                    <div className="min-w-0">
                      <p className="truncate">{f.user_friendly_name}</p>
                      <p className="truncate text-xs text-fg-muted">
                        {formatCount(f.num_bodies)} bodies · {formatFileSize(f.file_size)} · used by {f.usage_count}
                      </p>
                    </div>
                    {f.file_id === fileId && <CheckCircle className="ml-auto text-success" />}
                  </CommandItem>
                ))}
              </CommandGroup>
            </CommandList>
          </Command>
        </PopoverContent>
      </Popover>
      {selected?.description && <p className="text-xs text-fg-muted">{selected.description}</p>}
    </div>
  );
}
