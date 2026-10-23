// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use server';

import { backendAction, invalidId } from '@/lib/data/server';
import { InputFileInfo } from '@/types/api';

const filePages = ['/dashboard/files', '/dashboard/submit'];

export async function updateFile(
  fileId: number,
  name: string,
  description: string
) {
  const bad = invalidId(fileId, 'file ID');
  if (bad) return bad;
  const formData = new FormData();
  formData.append('user_friendly_name', name);
  // An empty form field reads as absent to FastAPI, so a cleared description
  // is sent as clear_description (src/routes/files.py update_file)
  if (description.trim()) {
    formData.append('description', description.trim());
  } else {
    formData.append('clear_description', 'true');
  }

  return backendAction<InputFileInfo>(`/api/files/${fileId}`, { method: 'PATCH', body: formData }, {
    fallbackError: 'Failed to update file',
    revalidate: filePages,
  });
}

export async function deleteFile(fileId: number) {
  const bad = invalidId(fileId, 'file ID');
  if (bad) return bad;
  return backendAction(`/api/files/${fileId}`, { method: 'DELETE' }, {
    fallbackError: 'Failed to delete file',
    revalidate: filePages,
  });
}
