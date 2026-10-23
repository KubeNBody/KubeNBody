// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import { AlertTriangle } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { FileUploadConflictResponse } from '@/types/api';
import { formatCount } from '@/lib/utils';

export interface NameConflict {
  open: boolean;
  existingFile?: FileUploadConflictResponse['existing_file'];
  userChoice?: 'reuse' | 'rename';
  newName?: string;
}

// The uploaded file's name is taken in the library (a 409 on submit): reuse
// that file or upload under a new name
export function NameConflictDialog({
  conflict,
  onChange,
  isSubmitting,
  onContinue,
}: {
  conflict: NameConflict | null;
  onChange: (conflict: NameConflict | null) => void;
  isSubmitting: boolean;
  onContinue: () => void;
}) {
  return (
    <Dialog open={conflict?.open || false} onOpenChange={(open) => {
      if (!open) {
        onChange(null);
      }
    }}>
      <DialogContent className="surface-card border-border-strong bg-surface text-fg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 text-fg">
            <AlertTriangle className="h-5 w-5 text-warning" />
            File Name Already Exists
          </DialogTitle>
          <DialogDescription className="text-fg-muted">
            A file with this name already exists in your library. How would you like to proceed?
          </DialogDescription>
        </DialogHeader>

        {conflict?.existingFile && (
          <div className="space-y-4">
            {/* Existing File Details */}
            <div className="p-3 bg-surface-raised border border-border rounded-lg">
              <p className="text-fg-muted text-xs mb-2">Existing file:</p>
              <p className="text-fg font-medium">{conflict.existingFile.user_friendly_name}</p>
              <p className="text-fg-muted text-sm mt-1">
                {conflict.existingFile.num_bodies > 0 ? `${formatCount(conflict.existingFile.num_bodies)} bodies • ` : ''}{(conflict.existingFile.file_size / 1024 / 1024).toFixed(2)} MB
              </p>
            </div>

            {/* Choice Radio Buttons */}
            <div className="space-y-3">
              <label className="flex items-start gap-3 p-3 bg-surface-raised border-2 border-border rounded-lg cursor-pointer hover:bg-surface-raised transition-colors">
                <input
                  type="radio"
                  name="conflictChoice"
                  value="reuse"
                  checked={conflict.userChoice === 'reuse'}
                  onChange={() => onChange({ ...conflict, userChoice: 'reuse' })}
                  className="mt-1 w-4 h-4 text-link"
                />
                <div className="flex-1">
                  <p className="text-fg font-medium">Use existing file</p>
                  <p className="text-fg-muted text-sm">
                    Use the file that&apos;s already in your library for this job
                  </p>
                </div>
              </label>

              <label className="flex items-start gap-3 p-3 bg-surface-raised border-2 border-border rounded-lg cursor-pointer hover:bg-surface-raised transition-colors">
                <input
                  type="radio"
                  name="conflictChoice"
                  value="rename"
                  checked={conflict.userChoice === 'rename'}
                  onChange={() => onChange({ ...conflict, userChoice: 'rename' })}
                  className="mt-1 w-4 h-4 text-link"
                />
                <div className="flex-1">
                  <p className="text-fg font-medium">Upload as new file with different name</p>
                  <p className="text-fg-muted text-sm">
                    Upload this file as a new entry with a unique name
                  </p>
                </div>
              </label>
            </div>

            {/* New Name Input (if rename selected) */}
            {conflict.userChoice === 'rename' && (
              <div
                className="space-y-2"
              >
                <Label htmlFor="newFileName" className="text-fg">
                  New file name
                </Label>
                <Input
                  id="newFileName"
                  placeholder={`${conflict.existingFile.user_friendly_name}_v2`}
                  value={conflict.newName || ''}
                  onChange={(e) => onChange({ ...conflict, newName: e.target.value })}
                  className="bg-surface-raised border-border-strong text-fg"
                />
                <p className="text-fg-muted text-xs">
                  Choose a unique name for this file
                </p>
              </div>
            )}
          </div>
        )}

        <DialogFooter className="gap-2">
          <Button
            variant="outline"
            onClick={() => onChange(null)}
            className="border-border-strong text-fg hover:bg-surface-raised"
          >
            Cancel
          </Button>
          <Button
            onClick={onContinue}
            disabled={isSubmitting || !conflict?.userChoice || (conflict.userChoice === 'rename' && !conflict.newName?.trim())}
            className="bg-primary hover:bg-primary-hover text-primary-fg"
          >
            Continue
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
