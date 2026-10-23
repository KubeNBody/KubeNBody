// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
export type FormField = string | number | boolean | File | Blob | null | undefined;

/**
 * A multipart form from a flat object (job submissions): undefined and null
 * fields are left out, booleans become "true"/"false", numbers their string form.
 */
export function toFormData(fields: Record<string, FormField>): FormData {
  const form = new FormData();
  for (const [key, value] of Object.entries(fields)) {
    if (value === undefined || value === null) continue;
    if (value instanceof Blob) form.append(key, value);
    else form.append(key, typeof value === 'boolean' ? (value ? 'true' : 'false') : String(value));
  }
  return form;
}
