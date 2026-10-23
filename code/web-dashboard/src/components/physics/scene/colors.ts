// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import * as THREE from 'three';
import type { AnimationSnapshot } from '@/types/api';

// Body colours. A few bodies get distinct hues chosen away from the X/Y/Z axis
// colours (red, green, blue) and bright enough on the near-black scene. Many
// bodies are coloured by log-mass with a lightness floor, so light bodies stay
// visible after tone mapping.
const DISTINCT = ['#ffb000', '#4cc9ff', '#ff5ec4', '#c6ff4a', '#b388ff', '#f2f2f2', '#ff8a3d', '#3ef0c0', '#ff8fa3', '#ffe066'].map(
  (c) => new THREE.Color(c)
);

export const COM_MARKER_COLOR = '#ffffff';

/** Masses do not change during a run, so this depends on one snapshot only */
export function particleColors(snapshot: AnimationSnapshot | undefined): THREE.Color[] {
  if (!snapshot) return [];
  const rows = snapshot.particles;
  if (rows.length <= DISTINCT.length) return rows.map((_, i) => DISTINCT[i]);

  // Loops, not Math.min(...masses): spreading N arguments overflows the stack
  let minMass = Infinity;
  let maxMass = -Infinity;
  for (const row of rows) {
    if (row[3] < minMass) minMass = row[3];
    if (row[3] > maxMass) maxMass = row[3];
  }
  const logMin = Math.log10(Math.max(minMass, 0.1));
  const logRange = Math.log10(Math.max(maxMass, 0.1)) - logMin || 1;
  return rows.map((row) => {
    const t = (Math.log10(Math.max(row[3], 0.1)) - logMin) / logRange;
    // Light (blue) to heavy (red), lightness 0.6-0.75
    return new THREE.Color().setHSL((1 - t) * 0.6, 1, 0.6 + t * 0.15);
  });
}
