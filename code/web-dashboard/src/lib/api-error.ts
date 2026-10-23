// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
/**
 * Turn a FastAPI error `detail` into display text. It is a string for an
 * HTTPException, but a list of {loc, msg} objects for a 422 validation error,
 * which React can't render as a child.
 */
export function formatApiDetail(detail: unknown): string {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        if (item && typeof item === 'object' && 'msg' in item) {
          const { msg, loc } = item as { msg: unknown; loc?: unknown };
          // loc is e.g. ['body', 'num_epochs']: the last segment names the field
          const field = Array.isArray(loc) && loc.length > 0 ? loc[loc.length - 1] : undefined;
          return field !== undefined ? `${field}: ${String(msg)}` : String(msg);
        }
        return typeof item === 'string' ? item : JSON.stringify(item);
      })
      .join(' | ');
  }
  if (detail === null || detail === undefined) return '';
  return JSON.stringify(detail);
}

/** A failed backend request on the Next.js server (or a missing demo fixture) */
export class APIServerError extends Error {
  constructor(
    message: string,
    public status?: number,
    public code?: string
  ) {
    super(message);
    this.name = 'APIServerError';
  }
}
