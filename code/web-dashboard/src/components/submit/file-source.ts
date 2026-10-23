// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
// Where the job's input file comes from: a new upload (checked in the browser
// first) or a file already in the library. One reducer, so the form can't end
// up with a mix of both.

export interface FileValidation {
  valid: boolean;
  error?: string;
  preview?: { totalRows: number; fileSize: number };
}

export type FileSource =
  | { kind: 'upload'; file: File | null; validation: FileValidation | null; validating: boolean; name: string; description: string }
  | { kind: 'existing'; fileId: number | null };

export type FileSourceAction =
  | { type: 'mode'; kind: FileSource['kind'] }
  | { type: 'picked'; file: File }
  | { type: 'validated'; file: File; validation: FileValidation }
  | { type: 'rejected'; error: string }
  | { type: 'cleared' }
  | { type: 'meta'; name?: string; description?: string }
  | { type: 'existing'; fileId: number | null };

export type UploadSource = Extract<FileSource, { kind: 'upload' }>;

export const emptyUpload: UploadSource = { kind: 'upload', file: null, validation: null, validating: false, name: '', description: '' };

export function fileSourceReducer(state: FileSource, action: FileSourceAction): FileSource {
  switch (action.type) {
    case 'mode':
      if (state.kind === action.kind) return state;
      return action.kind === 'upload' ? emptyUpload : { kind: 'existing', fileId: null };
    case 'picked':
      return { ...emptyUpload, file: action.file, validating: true };
    case 'validated':
      // A later pick or a removal supersedes a validation still in flight
      if (state.kind !== 'upload' || state.file !== action.file) return state;
      return { ...state, validation: action.validation, validating: false };
    case 'rejected':
      return { ...emptyUpload, validation: { valid: false, error: action.error } };
    case 'cleared':
      return emptyUpload;
    case 'meta':
      if (state.kind !== 'upload') return state;
      return { ...state, name: action.name ?? state.name, description: action.description ?? state.description };
    case 'existing':
      return { kind: 'existing', fileId: action.fileId };
  }
}

/** Ready to submit: a validated upload, or a chosen library file */
export function fileSourceReady(s: FileSource): boolean {
  return s.kind === 'upload' ? Boolean(s.file && s.validation?.valid && !s.validating) : s.fileId !== null;
}
