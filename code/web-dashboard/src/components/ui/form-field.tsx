// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import { cn } from '@/lib/utils';

/**
 * Label + control + help + error, with the ARIA wiring (aria-describedby,
 * aria-invalid) done once. The control is passed as a render function that
 * receives the props to spread on it.
 */
export function FormField({
  id,
  label,
  help,
  error,
  className,
  children,
}: {
  id: string;
  label: React.ReactNode;
  help?: React.ReactNode;
  error?: string;
  className?: string;
  children: (control: { id: string; 'aria-describedby'?: string; 'aria-invalid'?: boolean }) => React.ReactNode;
}) {
  const helpId = help ? `${id}-help` : undefined;
  const errorId = error ? `${id}-error` : undefined;
  const describedBy = [errorId, helpId].filter(Boolean).join(' ') || undefined;
  return (
    <div className={cn('space-y-1.5', className)}>
      <label htmlFor={id} className="block text-sm font-medium text-fg">
        {label}
      </label>
      {children({ id, 'aria-describedby': describedBy, 'aria-invalid': error ? true : undefined })}
      {error && (
        <p id={errorId} className="text-xs text-danger">
          {error}
        </p>
      )}
      {help && (
        <p id={helpId} className="text-xs text-fg-muted">
          {help}
        </p>
      )}
    </div>
  );
}

/** Text input styling shared by the form's inputs (matches ui/input) */
export const fieldClass =
  'h-9 w-full rounded-md border border-border-strong bg-surface-raised px-3 text-sm text-fg placeholder:text-fg-subtle transition-colors focus-visible:border-ring focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40 aria-invalid:border-danger disabled:opacity-50';

/** A native <select> in the form's style (native: the best picker on phones, works with register) */
export function NativeSelect({ className, ...props }: React.ComponentProps<'select'>) {
  return <select {...props} className={cn(fieldClass, 'pr-8', className)} />;
}
