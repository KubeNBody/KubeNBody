// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import { useCallback, useEffect, useRef, useState, type ComponentRef } from 'react';
import { Canvas, useFrame, useThree } from '@react-three/fiber';
import { OrbitControls, Stats } from '@react-three/drei';
import * as THREE from 'three';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { LoadingSpinner } from '@/components/ui/loading-spinner';
import { InfiniteGridWithLabels } from './InfiniteGridWithLabels';
import { useAnimationPages } from './useAnimationPages';
import { useCanvasRecorder } from './useCanvasRecorder';
import { OptionsPanel, PlaybackControls, type ViewOptions } from './AnimationControls';
import { bounds, niceNumber } from './scene/camera';
import { ParticleSystem } from './scene/ParticleSystem';

// The three-stdlib OrbitControls instance behind drei's <OrbitControls>
type OrbitControlsImpl = ComponentRef<typeof OrbitControls>;

interface AnimationViewerProps {
  jobId: number;
  particleSize?: number;
  showStats?: boolean;
  snapshotsPerPage?: number;
}

// Trails cost O(N x trail length) memory and work, so they start off for large N
const TRAILS_DEFAULT_MAX_PARTICLES = 500;
// A few bodies are drawn at full size, a crowd small so it doesn't merge into a blob
const LARGE_PARTICLES_MAX = 100;
// Trail length in rendered frames (60 per second). A few bodies get long trails
// that draw their orbits (10 s of playback), a crowd short ones
const LONG_TRAILS_MAX_PARTICLES = 100;
const LONG_TRAIL_LENGTH = 600;
const SHORT_TRAIL_LENGTH = 100;
// How often a playing animation tells React where the playhead is (slider, epoch
// label, page prefetch). The scene itself follows the playhead every frame
const PUBLISH_INTERVAL_MS = 100;

/**
 * Advances the playhead inside the render loop: framesPerTick (speed
 * included, see useAnimationPages) per 60 Hz frame, scaled by the real frame time. It holds while the snapshots the
 * next step needs are not loaded, and reports to React at most every
 * PUBLISH_INTERVAL_MS - immediately when it crosses into another page, so the
 * scene always has that page's snapshots.
 */
function PlaybackDriver({
  frameRef,
  playing,
  framesPerTick,
  maxFrame,
  pageSize,
  isFrameReady,
  onFrame,
}: {
  frameRef: React.RefObject<number>;
  playing: boolean;
  framesPerTick: number;
  maxFrame: number;
  pageSize: number;
  isFrameReady: (frame: number) => boolean;
  onFrame: (frame: number, ended: boolean) => void;
}) {
  const lastPublish = useRef(0);
  useFrame((_, delta) => {
    if (!playing || maxFrame < 0) return;
    // A hidden tab resumes with a huge delta: cap the jump
    const step = framesPerTick * Math.min(delta, 0.1) * 60;
    const current = frameRef.current;
    const next = Math.min(current + step, maxFrame);
    if (!isFrameReady(next)) return;
    frameRef.current = next;
    const now = performance.now();
    const ended = next >= maxFrame;
    if (ended || Math.floor(next / pageSize) !== Math.floor(current / pageSize) || now - lastPublish.current >= PUBLISH_INTERVAL_MS) {
      lastPublish.current = now;
      onFrame(next, ended);
    }
  });
  return null;
}

// Component to setup recording from inside the Canvas
function RecordingManager({ onRecordingSetup }: { onRecordingSetup: (canvas: HTMLCanvasElement) => void }) {
  const { gl } = useThree();
  const setupDoneRef = useRef(false);
  useEffect(() => {
    if (!setupDoneRef.current) {
      onRecordingSetup(gl.domElement);
      setupDoneRef.current = true;
    }
  }, [gl, onRecordingSetup]);
  return null;
}

function Overlay({ children, tone = 'border-border' }: { children: React.ReactNode; tone?: string }) {
  return (
    <div className="relative flex h-full w-full items-center justify-center overflow-hidden bg-bg">
      <div className={`rounded-lg border ${tone} bg-surface p-6 text-center`}>{children}</div>
    </div>
  );
}

export function AnimationViewer({
  jobId,
  particleSize: initialParticleSize,
  showStats = false,
  snapshotsPerPage = 30,
}: AnimationViewerProps) {
  const [isPlaying, setIsPlaying] = useState(false);
  // The playhead: frameRef moves every rendered frame while playing. currentFrame
  // is its last published value (slider, epoch label, page loading)
  const frameRef = useRef(0);
  const [currentFrame, setCurrentFrameState] = useState(0);
  const setCurrentFrame = useCallback((frame: number) => {
    frameRef.current = frame;
    setCurrentFrameState(frame);
  }, []);
  const [playbackSpeed, setPlaybackSpeed] = useState(1);
  const [options, setOptions] = useState<ViewOptions>({
    showGrid: false,
    showGridXY: true,
    showGridXZ: false,
    showGridYZ: false,
    showTrails: null,
    showStats,
    showInfoPanel: true,
    particleSize: initialParticleSize ?? null,
    trailLength: null,
    followCenterOfMass: false,
    showCenterOfMassMarker: false,
  });
  const updateOptions = (patch: Partial<ViewOptions>) => setOptions(prev => ({ ...prev, ...patch }));
  const [frameAllTrigger, setFrameAllTrigger] = useState(0); // Increment to trigger frame all

  // Grid state
  const [gridSize, setGridSize] = useState(10);
  const gridSizeInitializedRef = useRef(false);

  const recorder = useCanvasRecorder();
  const pages = useAnimationPages({ jobId, snapshotsPerPage, currentFrame, isPlaying, playbackSpeed });
  const { pageSize, totalSnapshots, particleCount, firstSnapshot, maxFrame, snapshots, pageError } = pages;
  const showTrails = options.showTrails ?? particleCount <= TRAILS_DEFAULT_MAX_PARTICLES;
  const particleSize = options.particleSize ?? (particleCount <= LARGE_PARTICLES_MAX ? 1 : 0.1);
  const trailLength = options.trailLength ?? (particleCount <= LONG_TRAILS_MAX_PARTICLES ? LONG_TRAIL_LENGTH : SHORT_TRAIL_LENGTH);

  const controlsRef = useRef<OrbitControlsImpl>(null);
  const viewerRef = useRef<HTMLDivElement>(null);

  // Reset grid initialization when jobId changes
  useEffect(() => {
    gridSizeInitializedRef.current = false;
  }, [jobId]);

  // Grid spacing from the extent of the run's first snapshot (once per job)
  useEffect(() => {
    if (gridSizeInitializedRef.current || !firstSnapshot || firstSnapshot.particles.length === 0) return;
    let maxAbs = 0;
    for (const p of firstSnapshot.particles) maxAbs = Math.max(maxAbs, Math.abs(p[0]), Math.abs(p[1]), Math.abs(p[2]));
    setGridSize(niceNumber(Math.max(Math.max(maxAbs, bounds(firstSnapshot.particles).maxDim) / 50, 0.1)));
    gridSizeInitializedRef.current = true;
  }, [firstSnapshot]);

  const onDriverFrame = useCallback((frame: number, ended: boolean) => {
    setCurrentFrameState(frame);
    if (ended) setIsPlaying(false);
  }, []);

  const handleReset = () => {
    setCurrentFrame(0);
    setIsPlaying(false);
    if (controlsRef.current) {
      controlsRef.current.reset();
    }
  };

  const handlePlayPause = () => {
    if (isPlaying) {
      // Publish where the playhead stopped
      setCurrentFrameState(frameRef.current);
    } else if (frameRef.current >= maxFrame) {
      setCurrentFrame(0);
    }
    setIsPlaying(!isPlaying);
  };

  const handleFrameChange = (value: number[]) => {
    const newFrame = value[0];
    setCurrentFrame(newFrame);
    setIsPlaying(false);
    // Burst prefetch around the target when it is more than a page away
    pages.seekTo(newFrame);
  };

  const handleStepBackward = () => {
    setCurrentFrame(Math.max(0, Math.floor(frameRef.current) - 1));
    setIsPlaying(false);
  };

  const handleStepForward = () => {
    setCurrentFrame(Math.min(maxFrame, Math.floor(frameRef.current) + 1));
    setIsPlaying(false);
  };

  const localFrame = currentFrame - pages.currentPage * pageSize;
  const currentSnapshotIndex = Math.min(Math.floor(localFrame), Math.max(0, snapshots.length - 1));
  const currentEpoch = snapshots[currentSnapshotIndex]?.epoch || 0;
  const progress = maxFrame > 0 ? (currentFrame / maxFrame) * 100 : 0;

  const errorMessage = pageError && (
    <>
      Failed to load: {pageError.message}
      {pageError.retrying && <span className="block text-xs text-fg-muted">Retrying automatically...</span>}
    </>
  );

  // Show error state if the first page failed
  if (totalSnapshots === 0 && pageError) {
    return (
      <Overlay tone="border-danger/30">
        <p className="mb-3 text-sm text-danger">{errorMessage}</p>
        <Button variant="outline" size="sm" onClick={pages.retry}>
          Retry
        </Button>
      </Overlay>
    );
  }

  // Show loading state if no data loaded yet
  if (totalSnapshots === 0 && pages.isLoading) {
    return (
      <Overlay>
        <p className="flex items-center gap-3 text-sm text-fg" role="status">
          <LoadingSpinner size="sm" /> Loading the animation…
        </p>
      </Overlay>
    );
  }

  // No snapshots available
  if (totalSnapshots === 0) {
    return (
      <Overlay>
        <p className="mb-1 text-sm text-fg">No snapshots yet</p>
        <p className="text-xs text-fg-muted">The animation appears once the job has saved its first snapshot.</p>
      </Overlay>
    );
  }

  return (
    <div ref={viewerRef} className="relative w-full h-full overflow-hidden bg-black/50">
      {/* 3D Canvas */}
      {/* One camera: the Canvas's (Z up); ParticleSystem frames it on the first snapshot */}
      <Canvas
        camera={{ position: [50, 50, 30], fov: 60, near: 0.1, far: 10000000, up: [0, 0, 1] }}
        gl={{ antialias: true, alpha: true, toneMapping: THREE.ACESFilmicToneMapping }}
      >
        <color attach="background" args={['#000814']} />

        <ambientLight intensity={0.3} />
        <directionalLight position={[100, 100, 50]} intensity={1.2} />
        <directionalLight position={[-100, -100, -50]} intensity={0.6} />
        <pointLight position={[0, 50, 0]} intensity={0.5} color="#ffffff" />
        <hemisphereLight args={['#ffffff', '#080820', 0.3]} />

        <PlaybackDriver
          frameRef={frameRef}
          playing={isPlaying}
          framesPerTick={pages.framesPerTick}
          maxFrame={maxFrame}
          pageSize={pageSize}
          isFrameReady={pages.isFrameReady}
          onFrame={onDriverFrame}
        />
        <ParticleSystem
          frameRef={frameRef}
          pageOffset={pages.currentPage * pageSize}
          snapshots={snapshots}
          firstSnapshot={firstSnapshot}
          particleSize={particleSize}
          showTrails={showTrails}
          trailLength={trailLength}
          followCenterOfMass={options.followCenterOfMass}
          showCenterOfMassMarker={options.showCenterOfMassMarker}
          controlsRef={controlsRef}
          frameAllTrigger={frameAllTrigger}
        />

        {/* Infinite grid with labels */}
        {options.showGrid && (
          <InfiniteGridWithLabels
            gridSize={gridSize}
            showXY={options.showGridXY}
            showXZ={options.showGridXZ}
            showYZ={options.showGridYZ}
          />
        )}

        {/* Controls */}
        <OrbitControls
          ref={controlsRef}
          enablePan={true}
          enableZoom={true}
          enableRotate={true}
          autoRotate={false}
          autoRotateSpeed={0.5}
          minDistance={1}
          maxDistance={1000000}
          enableDamping={true}
          dampingFactor={0.05}
        />

        {/* Performance Stats */}
        {/* Mounted in the viewer (drei's default is position:fixed on body, over the sidebar) */}
        {options.showStats && <Stats parent={viewerRef as React.RefObject<HTMLElement>} className="absolute! left-auto! right-4! top-4!" />}

        {/* Recording Manager */}
        <RecordingManager onRecordingSetup={recorder.setCanvas} />
      </Canvas>

      <PlaybackControls
        currentFrame={currentFrame}
        maxFrame={maxFrame}
        isPlaying={isPlaying}
        playbackSpeed={playbackSpeed}
        currentEpoch={currentEpoch}
        progress={progress}
        onSeek={handleFrameChange}
        onStepBackward={handleStepBackward}
        onPlayPause={handlePlayPause}
        onStepForward={handleStepForward}
        onReset={handleReset}
        onSpeedChange={setPlaybackSpeed}
      />

      <OptionsPanel
        options={options}
        showTrails={showTrails}
        particleSize={particleSize}
        trailLength={trailLength}
        onChange={updateOptions}
        onFrameAll={() => setFrameAllTrigger(prev => prev + 1)}
        recorder={recorder}
      />

      {/* Loading indicator - only while the playhead waits for a page (actual stutter) */}
      {pages.isWaiting && (
        <div className="pointer-events-none absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2">
          <p className="flex items-center gap-3 rounded-lg border border-border bg-surface/90 p-4 text-sm text-fg" role="status">
            <LoadingSpinner size="sm" /> Loading snapshots…
          </p>
        </div>
      )}

      {/* Error indicator with retry (a page the playhead needs failed) */}
      {pageError && (
        <div className="absolute top-4 right-4">
          <Card className="surface-card border-danger/30 p-4">
            <div className="flex items-center gap-3">
              <div className="text-danger text-sm">{errorMessage}</div>
              <Button variant="outline" size="sm" onClick={pages.retry} className="text-xs">
                Retry
              </Button>
            </div>
          </Card>
        </div>
      )}

      {/* Info Panel */}
      {options.showInfoPanel && (
        <div className={`absolute right-4 hidden sm:block ${options.showStats ? 'top-20' : 'top-4'}`}>
          <Card className="surface-card border-border p-3">
            <div className="text-fg text-sm space-y-1">
              <p>Particles: {particleCount}</p>
              <p>Snapshots: {totalSnapshots} (loaded: {pages.loadedPageCount * pageSize})</p>
              <p className="text-fg-muted text-xs">
                Drag to rotate • Scroll to zoom • Shift+drag to pan
              </p>
            </div>
          </Card>
        </div>
      )}
    </div>
  );
}
