// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import { useState } from 'react';
import Link from 'next/link';
import { Download, FolderOpen, MoreHorizontal, Pencil, Play, Trash2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { LoadingSpinner } from '@/components/ui/loading-spinner';
import { Pagination } from '@/components/ui/pagination';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import { PageHeader } from '@/components/layout/PageHeader';
import { FilterSelect, PageSizeSelect, SearchInput } from '@/components/list/ListToolbar';
import { RefreshControls } from '@/components/list/RefreshControls';
import { LocalTime } from '@/components/LocalTime';
import { deleteFile, updateFile } from '@/actions/files';
import { useServerAction } from '@/hooks/use-server-action';
import { useUrlFilters } from '@/hooks/use-url-filters';
import { capabilities } from '@/lib/capabilities';
import { fileDownloadUrl } from '@/lib/demo';
import { formatCount, formatFileSize } from '@/lib/utils';
import type { InputFileInfo } from '@/types/api';

const SORT_OPTIONS = [
  { value: 'last_used_at', label: 'Last used' },
  { value: 'name', label: 'Name' },
  { value: 'size', label: 'Size' },
] as const;

export interface FilesListProps {
  files: InputFileInfo[];
  total: number;
  page: number;
  size: number;
  pages: number;
}

/** The input-file library: uploaded body files, reusable for new jobs */
export function FilesList({ files, total, page, size, pages }: FilesListProps) {
  const { searchParams, currentPage, currentSize, pastLastPage, handleFilterChange, handleSearchChange } = useUrlFilters(
    '/dashboard/files',
    { initialPage: page, initialSize: size, total, pages }
  );
  const search = searchParams.get('search') || '';
  const sortBy = searchParams.get('sort_by') || '';
  // One edit dialog and one delete confirmation for the whole list
  const [editing, setEditing] = useState<InputFileInfo | null>(null);
  const [deleting, setDeleting] = useState<InputFileInfo | null>(null);
  const { run, running } = useServerAction();

  return (
    <div>
      <PageHeader
        title="Input files"
        description={`${formatCount(total)} ${search ? 'matching ' : ''}file${total === 1 ? '' : 's'} in the library`}
        actions={
          <>
            <RefreshControls label="Refresh files" />
            <Button asChild>
              <Link href="/dashboard/submit">Upload in a new job</Link>
            </Button>
          </>
        }
      />

      <div className="mb-4 flex flex-wrap items-center gap-2" role="search">
        <SearchInput defaultValue={search} onChange={handleSearchChange} placeholder="Search name or description" label="Search files" />
        <FilterSelect
          label="Sort by"
          value={sortBy}
          options={SORT_OPTIONS}
          allLabel="Newest first"
          onChange={(v) => handleFilterChange('sort_by', v)}
        />
        <div className="ml-auto">
          <PageSizeSelect value={currentSize} onChange={(s) => handleFilterChange('size', String(s))} />
        </div>
      </div>

      {pastLastPage ? null : files.length === 0 ? (
        <div className="rounded-lg border border-border bg-surface px-6 py-14 text-center">
          <FolderOpen className="mx-auto mb-3 size-10 text-fg-subtle" aria-hidden />
          <p className="font-medium text-fg">No files found</p>
          <p className="text-sm text-fg-muted">{search ? 'Try another search.' : 'Files you upload with a job are kept here for reuse.'}</p>
        </div>
      ) : (
        <>
          <div className="hidden overflow-hidden rounded-lg border border-border bg-surface md:block">
            <Table>
              <TableHeader>
                <TableRow className="border-border hover:bg-transparent">
                  <TableHead className="pl-4 text-fg-muted">File</TableHead>
                  <TableHead className="text-right text-fg-muted">Bodies</TableHead>
                  <TableHead className="text-right text-fg-muted">Size</TableHead>
                  <TableHead className="text-right text-fg-muted">Used by</TableHead>
                  <TableHead className="hidden text-right text-fg-muted lg:table-cell">Last used</TableHead>
                  <TableHead className="hidden text-right text-fg-muted lg:table-cell">Added</TableHead>
                  <TableHead className="w-10 pr-3">
                    <span className="sr-only">Actions</span>
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {files.map((file) => (
                  <TableRow key={file.file_id} className="border-border hover:bg-surface-raised">
                    <TableCell className="max-w-80 pl-4">
                      <FileName file={file} />
                    </TableCell>
                    <TableCell className="text-right tabular-nums text-fg-muted">{formatCount(file.num_bodies)}</TableCell>
                    <TableCell className="text-right tabular-nums text-fg-muted">{formatFileSize(file.file_size)}</TableCell>
                    <TableCell className="text-right tabular-nums text-fg-muted">
                      {file.usage_count} job{file.usage_count === 1 ? '' : 's'}
                    </TableCell>
                    <TableCell className="hidden text-right text-fg-muted lg:table-cell">
                      <LocalTime value={file.last_used_at} format="date" fallback="Never" />
                    </TableCell>
                    <TableCell className="hidden text-right text-fg-muted lg:table-cell">
                      <LocalTime value={file.created_at} format="date" />
                    </TableCell>
                    <TableCell className="pr-3 text-right">
                      <FileActionsMenu file={file} onEdit={setEditing} onDelete={setDeleting} />
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>

          <ul className="space-y-2 md:hidden">
            {files.map((file) => (
              <li key={file.file_id} className="rounded-lg border border-border bg-surface p-3">
                <div className="flex items-start justify-between gap-2">
                  <FileName file={file} />
                  <FileActionsMenu file={file} onEdit={setEditing} onDelete={setDeleting} />
                </div>
                <p className="mt-2 text-xs text-fg-subtle">
                  {formatCount(file.num_bodies)} bodies · {formatFileSize(file.file_size)} · used by {file.usage_count} job
                  {file.usage_count === 1 ? '' : 's'}
                </p>
              </li>
            ))}
          </ul>
        </>
      )}

      <div className="mt-4">
        <Pagination
          currentPage={currentPage}
          pages={pages}
          total={total}
          pageSize={currentSize}
          itemLabel="files"
          onPageChange={(p) => handleFilterChange('page', String(p))}
        />
      </div>

      <EditFileDialog
        file={editing}
        onClose={() => setEditing(null)}
        saving={running === 'edit'}
        onSave={async (name, description) => {
          if (!editing) return;
          const ok = await run('edit', () => updateFile(editing.file_id, name, description), {
            success: 'File updated',
            error: 'Could not update the file',
          });
          if (ok) setEditing(null);
        }}
      />
      <ConfirmDialog
        open={deleting !== null}
        onOpenChange={(o) => !o && setDeleting(null)}
        title={`Delete ${deleting?.user_friendly_name ?? 'file'}?`}
        description="The file is removed from the library. Jobs that already used it keep their results."
        confirmLabel="Delete file"
        destructive
        pending={running === 'delete'}
        onConfirm={async () => {
          if (!deleting) return;
          const ok = await run('delete', () => deleteFile(deleting.file_id), { success: 'File deleted', error: 'Could not delete the file' });
          if (ok) setDeleting(null);
        }}
      />
    </div>
  );
}

function FileName({ file }: { file: InputFileInfo }) {
  return (
    <div className="min-w-0">
      <p className="truncate font-medium text-fg" title={file.user_friendly_name}>
        {file.user_friendly_name}
      </p>
      <div className="flex min-w-0 items-center gap-1.5 text-xs text-fg-subtle">
        <span className="max-w-[60%] shrink-0 truncate" title={file.original_filename}>
          {file.original_filename}
        </span>
        {file.description && (
          <Tooltip>
            <TooltipTrigger asChild>
              <span className="truncate" tabIndex={0}>
                · {file.description}
              </span>
            </TooltipTrigger>
            <TooltipContent className="max-w-sm">{file.description}</TooltipContent>
          </Tooltip>
        )}
      </div>
    </div>
  );
}

function FileActionsMenu({
  file,
  onEdit,
  onDelete,
}: {
  file: InputFileInfo;
  onEdit: (file: InputFileInfo) => void;
  onDelete: (file: InputFileInfo) => void;
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon-sm" aria-label={`Actions for ${file.user_friendly_name}`} className="text-fg-muted hover:text-fg">
          <MoreHorizontal />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-52 border-border bg-surface-raised">
        <DropdownMenuItem asChild>
          <Link href={`/dashboard/submit?file=${file.file_id}`}>
            <Play /> Use in a new job
          </Link>
        </DropdownMenuItem>
        <DropdownMenuItem asChild>
          <a href={fileDownloadUrl(file.file_id)} download={file.original_filename}>
            <Download /> Download
          </a>
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        {capabilities.readOnlyReason && <DropdownMenuLabel className="text-xs font-normal text-fg-subtle">{capabilities.readOnlyReason}</DropdownMenuLabel>}
        <DropdownMenuItem disabled={!capabilities.canMutate} onSelect={() => onEdit(file)}>
          <Pencil /> Rename / describe…
        </DropdownMenuItem>
        <DropdownMenuItem disabled={!capabilities.canMutate} variant="destructive" onSelect={() => onDelete(file)}>
          <Trash2 /> Delete…
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function EditFileDialog({
  file,
  onClose,
  onSave,
  saving,
}: {
  file: InputFileInfo | null;
  onClose: () => void;
  onSave: (name: string, description: string) => void;
  saving: boolean;
}) {
  return (
    <Dialog open={file !== null} onOpenChange={(o) => !o && !saving && onClose()}>
      <DialogContent className="border-border bg-surface sm:max-w-md">
        {/* Keyed by file: the form starts from that file's values each time */}
        {file && <EditFileForm key={file.file_id} file={file} onCancel={onClose} onSave={onSave} saving={saving} />}
      </DialogContent>
    </Dialog>
  );
}

function EditFileForm({
  file,
  onCancel,
  onSave,
  saving,
}: {
  file: InputFileInfo;
  onCancel: () => void;
  onSave: (name: string, description: string) => void;
  saving: boolean;
}) {
  const [name, setName] = useState(file.user_friendly_name);
  const [description, setDescription] = useState(file.description || '');
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (name.trim()) onSave(name.trim(), description);
      }}
      className="space-y-4"
    >
      <DialogHeader>
        <DialogTitle className="text-fg">Edit file</DialogTitle>
        <DialogDescription className="text-fg-muted">{file.original_filename}</DialogDescription>
      </DialogHeader>
      <div className="space-y-2">
        <Label htmlFor="file-name" className="text-fg">
          Name
        </Label>
        <Input id="file-name" value={name} onChange={(e) => setName(e.target.value)} className="border-border-strong bg-surface-raised text-fg" required />
      </div>
      <div className="space-y-2">
        <Label htmlFor="file-description" className="text-fg">
          Description <span className="font-normal text-fg-subtle">(optional)</span>
        </Label>
        <textarea
          id="file-description"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          rows={3}
          className="w-full rounded-md border border-border-strong bg-surface-raised px-3 py-2 text-sm text-fg placeholder:text-fg-subtle"
          placeholder="What is in this file?"
        />
      </div>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel} disabled={saving}>
          Cancel
        </Button>
        <Button type="submit" disabled={saving || !name.trim()}>
          {saving && <LoadingSpinner size="sm" />}
          Save
        </Button>
      </DialogFooter>
    </form>
  );
}
