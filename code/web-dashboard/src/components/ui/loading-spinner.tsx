// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import { cn } from '@/lib/utils';
import { Loader2 } from 'lucide-react';

interface LoadingSpinnerProps {
  size?: 'sm' | 'md' | 'lg';
  className?: string;
  text?: string;
}

export function LoadingSpinner({ size = 'md', className, text }: LoadingSpinnerProps) {
  const sizeClasses = {
    sm: 'h-4 w-4',
    md: 'h-8 w-8',
    lg: 'h-12 w-12',
  };

  return (
    <div className={cn('flex items-center justify-center gap-2', className)}>
      <Loader2 className={cn('animate-spin text-link', sizeClasses[size])} />
      {text && <span className="text-white/80">{text}</span>}
    </div>
  );
}

interface SectionLoadingProps {
  text?: string;
  className?: string;
}

export function SectionLoading({ text = 'Loading...', className }: SectionLoadingProps) {
  return (
    <div className={cn('flex items-center justify-center p-8', className)}>
      <LoadingSpinner text={text} />
    </div>
  );
}