// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import { useEffect, useLayoutEffect, useMemo, useRef, type ComponentRef, type RefObject } from 'react';
import { useFrame, useThree } from '@react-three/fiber';
import { Billboard, OrbitControls, Text } from '@react-three/drei';
import * as THREE from 'three';
import type { AnimationSnapshot } from '@/types/api';
import { bounds, frameCamera } from './camera';
import { LABEL_FONT } from '../InfiniteGridWithLabels';
import { COM_MARKER_COLOR, particleColors } from './colors';

type OrbitControlsImpl = ComponentRef<typeof OrbitControls>;

export interface ParticleSystemProps {
  /** The playhead (global frame), advanced outside React */
  frameRef: RefObject<number>;
  /** Global frame of snapshots[0] (the loaded page's first frame) */
  pageOffset: number;
  snapshots: AnimationSnapshot[];
  /** The run's first snapshot: sizes, colours and the initial framing come from it */
  firstSnapshot: AnimationSnapshot | undefined;
  particleSize: number;
  showTrails: boolean;
  trailLength: number;
  followCenterOfMass: boolean;
  showCenterOfMassMarker: boolean;
  controlsRef: RefObject<OrbitControlsImpl | null>;
  frameAllTrigger: number;
}

/**
 * The bodies, their trails and the centre-of-mass marker. Positions are
 * interpolated between snapshots inside the render loop (useFrame) into one
 * reused buffer and written straight to the instance matrices, so playback
 * neither re-renders React nor allocates per frame.
 */
export function ParticleSystem({
  frameRef,
  pageOffset,
  snapshots,
  firstSnapshot,
  particleSize,
  showTrails,
  trailLength,
  followCenterOfMass,
  showCenterOfMassMarker,
  controlsRef,
  frameAllTrigger,
}: ParticleSystemProps) {
  const { camera } = useThree();
  const count = firstSnapshot?.particles.length ?? 0;
  const meshRef = useRef<THREE.InstancedMesh>(null);
  const comRef = useRef<THREE.Group>(null);

  // Reused state of the render loop
  const loop = useMemo(
    () => ({
      positions: new Float32Array(count * 3),
      com: new THREE.Vector3(),
      matrix: new THREE.Matrix4(),
      scaleV: new THREE.Vector3(),
      quat: new THREE.Quaternion(),
      posV: new THREE.Vector3(),
      direction: new THREE.Vector3(),
      cameraTarget: new THREE.Vector3(),
      drawn: { frame: NaN, snapshots: null as AnimationSnapshot[] | null, size: NaN },
    }),
    [count]
  );

  // Body size in world units suits a few bodies tens of units across: shrink it
  // for small systems and for many bodies (1/sqrt(N)). From the run's first
  // snapshot, so it does not jump at page boundaries.
  const sizeScale = useMemo(() => {
    if (!firstSnapshot || count === 0) return 1;
    const { maxDim } = bounds(firstSnapshot.particles);
    return (maxDim > 0 ? Math.min(1, maxDim / 20) : 1) * Math.min(1, 10 / Math.sqrt(count));
  }, [firstSnapshot, count]);
  const size = particleSize * sizeScale;

  // Per-body scale by log-mass (masses do not change during a run)
  const massScales = useMemo(
    () => Float32Array.from(firstSnapshot?.particles ?? [], (p) => Math.max(0.3, Math.min(Math.log10(Math.max(p[3], 0.1)) * 0.15 + 1, 2.5))),
    [firstSnapshot]
  );
  const colors = useMemo(() => particleColors(firstSnapshot), [firstSnapshot]);

  useLayoutEffect(() => {
    const mesh = meshRef.current;
    if (!mesh) return;
    colors.forEach((c, i) => mesh.setColorAt(i, c));
    if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
  }, [colors]);

  const trails = useTrails(count, trailLength, colors);

  // Frame the camera on the run's first snapshot once it is known
  const framed = useRef<AnimationSnapshot | null>(null);
  useEffect(() => {
    if (!firstSnapshot || framed.current === firstSnapshot) return;
    const { center, maxDim } = bounds(firstSnapshot.particles);
    frameCamera(camera, controlsRef.current, center, maxDim);
    framed.current = firstSnapshot;
  }, [firstSnapshot, camera, controlsRef]);

  // "Frame all": the current positions
  useEffect(() => {
    if (frameAllTrigger === 0 || count === 0) return;
    const { center, maxDim } = bounds(loop.positions, count);
    frameCamera(camera, controlsRef.current, center, Math.max(maxDim, 1));
  }, [frameAllTrigger, camera, controlsRef, loop, count]);

  useFrame(() => {
    const mesh = meshRef.current;
    if (!mesh || snapshots.length === 0) return;
    const local = Math.max(0, Math.min(frameRef.current - pageOffset, snapshots.length - 1));
    const d = loop.drawn;

    if (local !== d.frame || snapshots !== d.snapshots || size !== d.size) {
      const i0 = Math.floor(local);
      const i1 = Math.min(i0 + 1, snapshots.length - 1);
      const t = local - i0;
      const a = snapshots[i0];
      const b = snapshots[i1];
      const n = Math.min(count, a.particles.length, b.particles.length);
      const pos = loop.positions;
      for (let i = 0; i < n; i++) {
        const p = a.particles[i];
        const q = b.particles[i];
        pos[i * 3] = p[0] + (q[0] - p[0]) * t;
        pos[i * 3 + 1] = p[1] + (q[1] - p[1]) * t;
        pos[i * 3 + 2] = p[2] + (q[2] - p[2]) * t;
        loop.posV.set(pos[i * 3], pos[i * 3 + 1], pos[i * 3 + 2]);
        loop.scaleV.setScalar(size * massScales[i]);
        loop.matrix.compose(loop.posV, loop.quat, loop.scaleV);
        mesh.setMatrixAt(i, loop.matrix);
      }
      mesh.instanceMatrix.needsUpdate = true;
      const ca = a.center_of_mass;
      const cb = b.center_of_mass;
      loop.com.set(ca[0] + (cb[0] - ca[0]) * t, ca[1] + (cb[1] - ca[1]) * t, ca[2] + (cb[2] - ca[2]) * t);
      comRef.current?.position.copy(loop.com);
      // Trails grow only when the bodies move (not while paused)
      if (showTrails && local !== d.frame) trails.append(pos);
      d.frame = local;
      d.snapshots = snapshots;
      d.size = size;
    }

    // Follow the centre of mass, keeping the user's zoom and angle
    const controls = controlsRef.current;
    if (followCenterOfMass && controls) {
      loop.direction.subVectors(camera.position, controls.target).normalize();
      const distance = camera.position.distanceTo(controls.target);
      loop.cameraTarget.copy(loop.com).addScaledVector(loop.direction, distance);
      camera.position.lerp(loop.cameraTarget, 0.1);
      controls.target.lerp(loop.com, 0.1);
      controls.update();
    }
  });

  if (count === 0) return null;

  return (
    <group>
      {showTrails && <primitive object={trails.lines} />}
      <instancedMesh ref={meshRef} args={[undefined, undefined, count]} frustumCulled={false}>
        <sphereGeometry args={[1, 16, 16]} />
        <meshStandardMaterial metalness={0.3} roughness={0.4} toneMapped={false} />
      </instancedMesh>
      <group ref={comRef} visible={showCenterOfMassMarker}>
        <mesh rotation={[Math.PI / 2, 0, 0]}>
          <ringGeometry args={[size * 3, size * 3.5, 32]} />
          <meshBasicMaterial color={COM_MARKER_COLOR} side={THREE.DoubleSide} transparent opacity={0.85} toneMapped={false} />
        </mesh>
        <Billboard position={[0, 0, size * 5]}>
          <Text font={LABEL_FONT} fontSize={size * 4} color={COM_MARKER_COLOR} anchorX="center" anchorY="middle">
            COM
          </Text>
        </Billboard>
      </group>
    </group>
  );
}

/**
 * All trails as one LineSegments ring buffer: a trail of L positions is L - 1
 * segments. Each append writes one segment per body (previous -> current) into
 * the oldest slot. Slot-major layout, so a slot is one contiguous upload and the
 * filled slots are a prefix for the draw range. No allocation per append.
 */
function useTrails(count: number, trailLength: number, colors: THREE.Color[]) {
  const slots = Math.max(1, trailLength - 1);
  const trail = useMemo(() => {
    const geometry = new THREE.BufferGeometry();
    const position = new THREE.BufferAttribute(new Float32Array(slots * count * 6), 3);
    position.setUsage(THREE.DynamicDrawUsage);
    geometry.setAttribute('position', position);
    geometry.setAttribute('color', new THREE.BufferAttribute(new Float32Array(slots * count * 6), 3));
    geometry.setDrawRange(0, 0);
    const lines = new THREE.LineSegments(geometry, new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, opacity: 0.35 }));
    lines.frustumCulled = false;
    const state = { previous: new Float32Array(count * 3), hasPrevious: false, head: 0, filled: 0 };
    const append = (pos: Float32Array) => {
      if (state.hasPrevious) {
        const array = position.array as Float32Array;
        const base = state.head * count * 6;
        for (let i = 0; i < count; i++) {
          const o = base + i * 6;
          array[o] = state.previous[i * 3];
          array[o + 1] = state.previous[i * 3 + 1];
          array[o + 2] = state.previous[i * 3 + 2];
          array[o + 3] = pos[i * 3];
          array[o + 4] = pos[i * 3 + 1];
          array[o + 5] = pos[i * 3 + 2];
        }
        position.addUpdateRange(base, count * 6);
        position.needsUpdate = true;
        state.head = (state.head + 1) % slots;
        state.filled = Math.min(state.filled + 1, slots);
        geometry.setDrawRange(0, state.filled * count * 2);
      }
      state.previous.set(pos.subarray(0, count * 3));
      state.hasPrevious = true;
    };
    return { lines, append };
  }, [count, slots]);

  useEffect(
    () => () => {
      trail.lines.geometry.dispose();
      (trail.lines.material as THREE.Material).dispose();
    },
    [trail]
  );

  // Both vertices of every segment of a body get that body's colour
  useLayoutEffect(() => {
    const attribute = trail.lines.geometry.getAttribute('color') as THREE.BufferAttribute;
    const array = attribute.array as Float32Array;
    for (let i = 0; i < count; i++) {
      const c = colors[i];
      if (!c) continue;
      for (let s = 0; s < slots; s++) {
        const o = (s * count + i) * 6;
        array[o] = array[o + 3] = c.r;
        array[o + 1] = array[o + 4] = c.g;
        array[o + 2] = array[o + 5] = c.b;
      }
    }
    attribute.needsUpdate = true;
  }, [trail, colors, count, slots]);

  return trail;
}
