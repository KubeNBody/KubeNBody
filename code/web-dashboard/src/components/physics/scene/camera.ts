// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
import * as THREE from 'three';

/** Centre and largest extent of a set of [x, y, z, ...] rows, or of a flat xyz buffer */
export function bounds(points: ArrayLike<number>[] | Float32Array, count?: number): { center: THREE.Vector3; maxDim: number } {
  const min = new THREE.Vector3(Infinity, Infinity, Infinity);
  const max = new THREE.Vector3(-Infinity, -Infinity, -Infinity);
  const n = points instanceof Float32Array ? (count ?? points.length / 3) : points.length;
  for (let i = 0; i < n; i++) {
    const x = points instanceof Float32Array ? points[i * 3] : points[i][0];
    const y = points instanceof Float32Array ? points[i * 3 + 1] : points[i][1];
    const z = points instanceof Float32Array ? points[i * 3 + 2] : points[i][2];
    if (x < min.x) min.x = x;
    if (y < min.y) min.y = y;
    if (z < min.z) min.z = z;
    if (x > max.x) max.x = x;
    if (y > max.y) max.y = y;
    if (z > max.z) max.z = z;
  }
  if (n === 0) return { center: new THREE.Vector3(), maxDim: 1 };
  const size = new THREE.Vector3().subVectors(max, min);
  return { center: new THREE.Vector3().addVectors(min, max).multiplyScalar(0.5), maxDim: Math.max(size.x, size.y, size.z) };
}

/** Put the whole box in view from above a diagonal, Z up (used at load and by "Frame all") */
export function frameCamera(
  camera: THREE.Camera,
  controls: { target: THREE.Vector3; update: () => void } | null | undefined,
  center: THREE.Vector3,
  maxDim: number
) {
  const fov = camera instanceof THREE.PerspectiveCamera ? camera.fov : 50;
  const distance = (Math.max(maxDim, 1e-6) * 1.5) / (2 * Math.tan((fov * Math.PI) / 360));
  camera.position.set(center.x + distance, center.y + distance, center.z + distance / 2);
  camera.up.set(0, 0, 1);
  camera.lookAt(center);
  if (controls) {
    controls.target.copy(center);
    controls.update();
  }
}

/** A "nice" grid spacing (1, 2 or 5 times a power of ten) */
export function niceNumber(value: number): number {
  if (value <= 0) return 1;
  const exponent = Math.floor(Math.log10(value));
  const fraction = value / 10 ** exponent;
  const nice = fraction < 1.5 ? 1 : fraction < 3.5 ? 2 : fraction < 7.5 ? 5 : 10;
  return nice * 10 ** exponent;
}
