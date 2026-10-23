// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import { useReducer, useState } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import { FormProvider, useForm, type FieldErrors } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { AlertCircle, Rocket } from 'lucide-react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { LoadingSpinner } from '@/components/ui/loading-spinner';
import { FormField, fieldClass } from '@/components/ui/form-field';
import { Progress } from '@/components/ui/progress';
import { DemoNote } from '@/components/demo/DemoMode';
import { useCreateJob, useCreateV2Job } from '@/hooks/use-api';
import { APIError } from '@/lib/api-client';
import { capabilities } from '@/lib/capabilities';
import { JOB_NAME_MAX } from '@/lib/domain/job-title';
import type { FileUploadConflictResponse } from '@/types/api';
import { buildV1Request, buildV2Request } from './build-request';
import { emptyUpload, fileSourceReady, fileSourceReducer, type FileSource } from './file-source';
import { InputFileSection } from './InputFileSection';
import { jobSchema, type JobFormData } from './job-form';
import { NameConflictDialog, type NameConflict } from './NameConflictDialog';
import { AdvancedSection, ExecutionSection, PhysicsSection } from './ParameterSections';

const DEFAULTS: Partial<JobFormData> = {
  num_epochs: 100000,
  package_size: 100,
  time_step: 0.00001,
  snapshot_interval: 500,
  job_type: 'distributed',
  use_gpu: false,
  force_algorithm: 'direct',
  barnes_hut_theta: 0.5,
  softening_sq: 1.0,
  integration_method: 'symplectic_euler',
  background_predictor: 'linear',
  partition_strategy: 'linear',
  partition_seed: 0,
  adaptive_enabled: false,
  adaptive_eta: 0.02,
};

/** ?file=<id> (the files page's "Use in a new job") starts from that library file */
function initialSource(fileParam: string | null): FileSource {
  const id = Number(fileParam);
  return Number.isInteger(id) && id > 0 ? { kind: 'existing', fileId: id } : emptyUpload;
}

export function SubmitJobForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [source, dispatch] = useReducer(fileSourceReducer, searchParams.get('file'), initialSource);
  const [uploadProgress, setUploadProgress] = useState<number | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [conflict, setConflict] = useState<NameConflict | null>(null);
  const createJob = useCreateJob();
  const createV2Job = useCreateV2Job();
  const submitting = createJob.isPending || createV2Job.isPending;

  const form = useForm<JobFormData>({ resolver: zodResolver(jobSchema), defaultValues: DEFAULTS });
  const ready = fileSourceReady(source);

  const submit = async (data: JobFormData, overrides: { forceNew?: boolean; fileName?: string } = {}) => {
    if (!capabilities.canMutate) return; // the read-only demo shows the form but never submits
    if (!ready) {
      toast.error(source.kind === 'upload' ? 'Add a valid input file first' : 'Choose a library file first');
      return;
    }
    setSubmitError(null);
    const upload = source.kind === 'upload' ? source : null;
    const libraryFileId = source.kind === 'existing' ? source.fileId! : undefined;
    // An upload also becomes a library file, under this name and description
    const libraryName = upload ? (overrides.fileName ?? upload.name).trim() || undefined : undefined;
    const libraryDescription = upload?.description.trim() || undefined;
    const forceNew = overrides.forceNew ?? false;
    if (upload) setUploadProgress(0);

    // The input file goes only through the source (both endpoints take the same
    // file fields). The job data carries the parameters
    const fileFields = upload
      ? { file: upload.file!, onProgress: setUploadProgress, userFriendlyName: libraryName, description: libraryDescription, forceNew }
      : { fileId: libraryFileId };
    try {
      const job =
        data.job_type === 'distributed_v2'
          ? await createV2Job.mutateAsync({ jobData: buildV2Request(data), ...fileFields })
          : await createJob.mutateAsync({ jobData: buildV1Request(data), ...fileFields });
      router.push(`/dashboard/jobs/${job.job_id}`);
    } catch (error) {
      // 409: the upload's library name is taken -> reuse that file or rename
      const body = error instanceof APIError && error.status === 409 ? (error.response?.data as FileUploadConflictResponse | undefined) : undefined;
      if (body?.error === 'name_conflict') {
        setConflict({ open: true, existingFile: body.existing_file });
      } else {
        setSubmitError((error instanceof Error && error.message) || 'Unexpected error');
      }
    } finally {
      setUploadProgress(null);
    }
  };

  // Fields in the collapsed section or without inline errors: list every problem
  const onInvalid = (errors: FieldErrors<JobFormData>) => {
    toast.error('Please fix the form', {
      description: Object.entries(errors)
        .map(([field, e]) => `${field}: ${e?.message || 'invalid value'}`)
        .join(' | '),
    });
  };

  const continueAfterConflict = async () => {
    if (!conflict) return;
    if (conflict.userChoice === 'reuse' && conflict.existingFile) {
      dispatch({ type: 'existing', fileId: conflict.existingFile.file_id });
      setConflict(null);
    } else if (conflict.userChoice === 'rename') {
      const name = conflict.newName?.trim();
      if (!name) {
        toast.error('Enter a new name');
        return;
      }
      dispatch({ type: 'meta', name });
      setConflict(null);
      await submit(form.getValues(), { fileName: name });
    }
  };

  return (
    <FormProvider {...form}>
      <form onSubmit={form.handleSubmit((d) => submit(d), onInvalid)} noValidate>
        <div className="grid items-start gap-6 lg:grid-cols-5">
          <div className="space-y-6 lg:sticky lg:top-6 lg:col-span-2">
            <InputFileSection source={source} dispatch={dispatch} />

            <div className="space-y-3 rounded-lg border border-border bg-surface p-5">
              <FormField
                id="job_name"
                label="Job name"
                help="Optional. Shown in the job list instead of the file name, so runs of the same file are easy to tell apart."
                error={form.formState.errors.job_name?.message}
              >
                {(p) => (
                  <input
                    {...p}
                    type="text"
                    maxLength={JOB_NAME_MAX}
                    placeholder="E.g. Figure eight with RK4"
                    className={fieldClass}
                    {...form.register('job_name')}
                  />
                )}
              </FormField>
              {uploadProgress !== null && (
                <div className="space-y-1.5" role="status">
                  <div className="flex justify-between text-sm">
                    <span className="text-fg">Uploading…</span>
                    <span className="tabular-nums text-fg-muted">{uploadProgress}%</span>
                  </div>
                  <Progress value={uploadProgress} className="h-2 bg-surface-raised" />
                </div>
              )}
              <Button type="submit" size="lg" className="w-full" disabled={!capabilities.canMutate || submitting}>
                {submitting ? <LoadingSpinner size="sm" /> : <Rocket />}
                {!capabilities.canMutate ? 'Submitting is disabled in the demo' : submitting ? 'Submitting…' : 'Submit simulation'}
              </Button>
              {capabilities.canMutate && !ready && (
                <p className="text-center text-xs text-fg-muted">
                  {source.kind === 'upload' ? 'Add a valid input file to submit.' : 'Choose a library file to submit.'}
                </p>
              )}
              <DemoNote>The form is here to explore the options, the demo has no simulation backend.</DemoNote>
              {submitError && (
                <p className="flex items-start gap-2 rounded-md border border-danger/30 bg-danger/10 p-3 text-sm text-danger" role="alert">
                  <AlertCircle className="mt-0.5 size-4 shrink-0" aria-hidden />
                  <span>
                    <span className="font-medium">Submission failed.</span> {submitError}
                  </span>
                </p>
              )}
            </div>
          </div>

          <div className="space-y-6 lg:col-span-3">
            <ExecutionSection />
            <PhysicsSection />
            <AdvancedSection />
          </div>
        </div>
      </form>

      <NameConflictDialog conflict={conflict} onChange={setConflict} isSubmitting={submitting} onContinue={continueAfterConflict} />
    </FormProvider>
  );
}
