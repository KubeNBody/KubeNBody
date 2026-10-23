// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import { useRef, useMemo, useEffect } from 'react';
import { useFrame } from '@react-three/fiber';
import { Billboard, Text, Line } from '@react-three/drei';
import * as THREE from 'three';

// Served with the dashboard (public/fonts, SIL OFL): without a font troika
// fetches one from a CDN at runtime, which a cluster without internet access
// cannot reach.
export const LABEL_FONT = `${process.env.NEXT_PUBLIC_BASE_PATH ?? ''}/fonts/NotoSans-Regular.ttf`;

const X_TICK_COLOR = new THREE.Color('#ff6666');
const Y_TICK_COLOR = new THREE.Color('#66ff66');
const Z_TICK_COLOR = new THREE.Color('#6666ff');

interface InfiniteGridWithLabelsProps {
  gridSize?: number;
  showXY?: boolean;
  showXZ?: boolean;
  showYZ?: boolean;
}

export function InfiniteGridWithLabels({
  gridSize = 10,
  showXY = true,
  showXZ = false,
  showYZ = false,
}: InfiniteGridWithLabelsProps) {
  const gridXYRef = useRef<THREE.GridHelper>(null);
  const gridXZRef = useRef<THREE.GridHelper>(null);
  const gridYZRef = useRef<THREE.GridHelper>(null);

  // Fixed large grid that covers any reasonable view
  // Grid extends 1000 * gridSize in each direction from center
  const gridTotalSize = gridSize * 2000;
  const divisions = 2000;

  const tickInterval = gridSize * 10;
  const labelRange = 10; // Show 10 labels in each direction

  useFrame(({ camera }) => {
    // Snap grid position to prevent edge visibility
    const snapSize = gridSize * 10;

    if (gridXYRef.current) {
      const x = Math.round(camera.position.x / snapSize) * snapSize;
      const y = Math.round(camera.position.y / snapSize) * snapSize;
      gridXYRef.current.position.set(x, y, 0);
    }

    if (gridXZRef.current) {
      const x = Math.round(camera.position.x / snapSize) * snapSize;
      const z = Math.round(camera.position.z / snapSize) * snapSize;
      gridXZRef.current.position.set(x, 0, z);
    }

    if (gridYZRef.current) {
      const y = Math.round(camera.position.y / snapSize) * snapSize;
      const z = Math.round(camera.position.z / snapSize) * snapSize;
      gridYZRef.current.position.set(0, y, z);
    }
  });

  // Generate labels and ticks for axes
  const { xLabels, yLabels, zLabels, tickGeometry } = useMemo(() => {
    const xLabels: Array<{ pos: [number, number, number]; text: string }> = [];
    const yLabels: Array<{ pos: [number, number, number]; text: string }> = [];
    const zLabels: Array<{ pos: [number, number, number]; text: string }> = [];
    // All tick marks as one line-segment geometry (two vertices per tick,
    // colored per axis) instead of one <Line> object per tick
    const positions: number[] = [];
    const colors: number[] = [];
    const addTick = (from: [number, number, number], to: [number, number, number], color: THREE.Color) => {
      positions.push(...from, ...to);
      colors.push(color.r, color.g, color.b, color.r, color.g, color.b);
    };

    const tickSize = gridSize;
    const labelOffset = gridSize * 2;

    for (let i = -labelRange; i <= labelRange; i++) {
      if (i === 0) continue;

      const worldPos = i * tickInterval;

      xLabels.push({ pos: [worldPos, -labelOffset, 0], text: formatNumber(worldPos) });
      addTick([worldPos, -tickSize, 0], [worldPos, tickSize, 0], X_TICK_COLOR);

      yLabels.push({ pos: [-labelOffset, worldPos, 0], text: formatNumber(worldPos) });
      addTick([-tickSize, worldPos, 0], [tickSize, worldPos, 0], Y_TICK_COLOR);

      zLabels.push({ pos: [-labelOffset, 0, worldPos], text: formatNumber(worldPos) });
      addTick([-tickSize, 0, worldPos], [tickSize, 0, worldPos], Z_TICK_COLOR);
    }

    const tickGeometry = new THREE.BufferGeometry();
    tickGeometry.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
    tickGeometry.setAttribute('color', new THREE.Float32BufferAttribute(colors, 3));

    return { xLabels, yLabels, zLabels, tickGeometry };
  }, [tickInterval, gridSize]);

  // A geometry passed as a prop is not disposed by R3F: free the replaced one
  useEffect(() => () => tickGeometry.dispose(), [tickGeometry]);

  // A quarter of the label spacing, so labels stay the same size relative to the
  // axes at any system scale (a fixed 0.5-unit floor made them half a tick wide
  // for a system ~2 units across)
  const fontSize = tickInterval * 0.25;

  return (
    <>
      {/* XY Plane Grid (Z=0) */}
      {showXY && (
        <gridHelper
          key={`grid-xy-${gridSize}`}
          ref={gridXYRef}
          args={[gridTotalSize, divisions, '#8888aa', '#555577']}
          rotation={[Math.PI / 2, 0, 0]}
        />
      )}

      {/* XZ Plane Grid (Y=0) */}
      {showXZ && (
        <gridHelper
          key={`grid-xz-${gridSize}`}
          ref={gridXZRef}
          args={[gridTotalSize, divisions, '#8888aa', '#555577']}
          rotation={[0, 0, 0]}
        />
      )}

      {/* YZ Plane Grid (X=0) */}
      {showYZ && (
        <gridHelper
          key={`grid-yz-${gridSize}`}
          ref={gridYZRef}
          args={[gridTotalSize, divisions, '#8888aa', '#555577']}
          rotation={[0, 0, Math.PI / 2]}
        />
      )}

      {/* Main axis lines */}
      <Line points={[[-1e6, 0, 0], [1e6, 0, 0]]} color="#ff4444" lineWidth={2} />
      <Line points={[[0, -1e6, 0], [0, 1e6, 0]]} color="#44ff44" lineWidth={2} />
      <Line points={[[0, 0, -1e6], [0, 0, 1e6]]} color="#4444ff" lineWidth={2} />

      {/* Axis ticks */}
      <lineSegments geometry={tickGeometry}>
        <lineBasicMaterial vertexColors />
      </lineSegments>

      {/* Tick labels face the camera (Billboard): flat in the axis planes they read
          mirrored or edge-on from most angles */}
      {/* X axis labels */}
      {xLabels.map((label, i) => (
        <Billboard key={`xlabel-${i}`} position={label.pos}>
          <Text font={LABEL_FONT} fontSize={fontSize} color="#ff8888" anchorX="center" anchorY="top">{label.text}</Text>
        </Billboard>
      ))}

      {/* Y axis labels */}
      {yLabels.map((label, i) => (
        <Billboard key={`ylabel-${i}`} position={label.pos}>
          <Text font={LABEL_FONT} fontSize={fontSize} color="#88ff88" anchorX="right" anchorY="middle">{label.text}</Text>
        </Billboard>
      ))}

      {/* Z axis labels */}
      {zLabels.map((label, i) => (
        <Billboard key={`zlabel-${i}`} position={label.pos}>
          <Text font={LABEL_FONT} fontSize={fontSize} color="#8888ff" anchorX="right" anchorY="middle">{label.text}</Text>
        </Billboard>
      ))}

      {/* Axis name labels */}
      <Billboard position={[gridSize * 120, 0, 0]}>
        <Text font={LABEL_FONT} fontSize={fontSize * 1.2} color="#ff6666" anchorX="left">X</Text>
      </Billboard>
      <Billboard position={[0, gridSize * 120, 0]}>
        <Text font={LABEL_FONT} fontSize={fontSize * 1.2} color="#66ff66" anchorX="left">Y</Text>
      </Billboard>
      <Billboard position={[0, 0, gridSize * 120]}>
        <Text font={LABEL_FONT} fontSize={fontSize * 1.2} color="#6666ff" anchorX="left">Z</Text>
      </Billboard>
    </>
  );
}

function formatNumber(n: number): string {
  if (n === 0) return '0';
  const abs = Math.abs(n);

  if (abs >= 1e6) return n.toExponential(0);
  if (abs >= 10000) return (n / 1000).toFixed(0) + 'k';
  if (abs >= 1) return n.toFixed(0);
  if (abs >= 0.01) return n.toFixed(2);
  return n.toExponential(0);
}
