// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import { SectionLoading } from '@/components/ui/loading-spinner';

export default function DashboardLoading() {
  return <SectionLoading text="Loading..." className="min-h-[50vh]" />;
}
