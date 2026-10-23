// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Slider } from '@/components/ui/slider';
import { Play, Pause, RotateCcw, SkipBack, SkipForward, Info, Video, Square, Download, Maximize, SlidersHorizontal, X } from 'lucide-react';
import { cn } from '@/lib/utils';
import type { useCanvasRecorder } from './useCanvasRecorder';

// Display options of the animation viewer (the left-hand panel)
export interface ViewOptions {
  showGrid: boolean;
  showGridXY: boolean;
  showGridXZ: boolean;
  showGridYZ: boolean;
  // null until the user toggles trails: the default then depends on N
  showTrails: boolean | null;
  showStats: boolean;
  showInfoPanel: boolean;
  // null until the user moves the slider: the default then depends on N
  particleSize: number | null;
  // null until the user moves the slider: the default then depends on N
  trailLength: number | null;
  followCenterOfMass: boolean;
  showCenterOfMassMarker: boolean;
}

// Timeline, transport buttons and speed (bottom of the viewer)
export function PlaybackControls({
  currentFrame,
  maxFrame,
  isPlaying,
  playbackSpeed,
  currentEpoch,
  progress,
  onSeek,
  onStepBackward,
  onPlayPause,
  onStepForward,
  onReset,
  onSpeedChange,
}: {
  currentFrame: number;
  maxFrame: number;
  isPlaying: boolean;
  playbackSpeed: number;
  currentEpoch: number;
  progress: number;
  onSeek: (value: number[]) => void;
  onStepBackward: () => void;
  onPlayPause: () => void;
  onStepForward: () => void;
  onReset: () => void;
  onSpeedChange: (speed: number) => void;
}) {
  return (
    <div className="absolute bottom-4 left-1/2 -translate-x-1/2 w-full max-w-3xl px-4">
      <Card className="surface-card border-border p-4">
        <div className="space-y-3">
          {/* Seek slider */}
          <Slider
            value={[currentFrame]}
            onValueChange={onSeek}
            min={0}
            max={maxFrame}
            step={0.01}
            aria-label="Playback position"
            className="w-full"
          />

          {/* Controls Row */}
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <Button
                size="sm"
                variant="ghost"
                onClick={onStepBackward}
                aria-label="Step backward"
                className="h-8 w-8 p-0 text-fg hover:bg-surface-raised"
                disabled={currentFrame <= 0}
              >
                <SkipBack className="h-4 w-4" />
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={onPlayPause}
                aria-label={isPlaying ? 'Pause' : 'Play'}
                className="h-8 w-8 p-0 text-fg hover:bg-surface-raised"
              >
                {isPlaying ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4" />}
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={onStepForward}
                aria-label="Step forward"
                className="h-8 w-8 p-0 text-fg hover:bg-surface-raised"
                disabled={currentFrame >= maxFrame}
              >
                <SkipForward className="h-4 w-4" />
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={onReset}
                aria-label="Reset to start"
                className="h-8 w-8 p-0 text-fg hover:bg-surface-raised"
              >
                <RotateCcw className="h-4 w-4" />
              </Button>
            </div>

            {/* Info Display */}
            <div className="text-fg text-sm">
              <span>Epoch: {currentEpoch}</span>
              <span className="mx-2">|</span>
              <span>{progress.toFixed(1)}%</span>
            </div>

            {/* Speed Control */}
            <div className="flex items-center gap-2">
              <label htmlFor="playback-speed" className="text-fg text-sm">Speed:</label>
              <select
                id="playback-speed"
                value={playbackSpeed}
                onChange={(e) => onSpeedChange(Number(e.target.value))}
                className="bg-surface-raised border border-border text-fg text-sm rounded px-2 py-1"
              >
                <option value={0.25}>0.25x</option>
                <option value={0.5}>0.5x</option>
                <option value={1}>1x</option>
                <option value={2}>2x</option>
                <option value={4}>4x</option>
              </select>
            </div>
          </div>
        </div>
      </Card>
    </div>
  );
}

function Checkbox({
  checked,
  onChange,
  small,
  children,
}: {
  checked: boolean;
  onChange: (checked: boolean) => void;
  small?: boolean;
  children: React.ReactNode;
}) {
  return (
    <label className={`flex items-center gap-2 text-fg ${small ? 'text-xs' : 'text-sm'} cursor-pointer`}>
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="rounded border-border bg-surface-raised"
      />
      {children}
    </label>
  );
}

// Visualization, camera, size and recording options (top left of the viewer)
export function OptionsPanel({
  options,
  showTrails,
  particleSize,
  trailLength,
  onChange,
  onFrameAll,
  recorder,
}: {
  options: ViewOptions;
  // Effective trails setting (options.showTrails, or the default for N)
  showTrails: boolean;
  // Effective particle size (options.particleSize, or the default for N)
  particleSize: number;
  // Effective trail length (options.trailLength, or the default for N)
  trailLength: number;
  onChange: (patch: Partial<ViewOptions>) => void;
  onFrameAll: () => void;
  recorder: ReturnType<typeof useCanvasRecorder>;
}) {
  const { isRecording, isProcessing, recordedBlob, recordingDuration } = recorder;
  // Below md the column would cover the whole scene: collapsed behind a button
  const [openOnMobile, setOpenOnMobile] = useState(false);

  return (
    <>
    <Button
      variant="outline"
      size="sm"
      onClick={() => setOpenOnMobile(true)}
      className={cn('absolute top-4 left-4 md:hidden', openOnMobile && 'hidden')}
      aria-expanded={openOnMobile}
    >
      <SlidersHorizontal className="h-4 w-4 mr-2" />
      Options
    </Button>
    <div
      className={cn(
        // Scrolls inside the viewer instead of running into the playback bar
        'absolute top-4 left-4 w-60 space-y-2 overflow-y-auto pr-1 max-h-[calc(100%-11rem)]',
        openOnMobile ? 'block' : 'hidden md:block'
      )}
    >
      <Card className="surface-card border-border p-3">
        <h3 className="text-fg text-sm font-semibold mb-2 flex items-center gap-2">
          <Info className="h-4 w-4" />
          Visualization
          <button
            type="button"
            onClick={() => setOpenOnMobile(false)}
            className="ml-auto rounded p-0.5 text-fg-muted hover:text-fg md:hidden"
            aria-label="Close options"
          >
            <X className="h-4 w-4" />
          </button>
        </h3>
        <div className="space-y-2">
          <Checkbox checked={options.showGrid} onChange={(v) => onChange({ showGrid: v })}>
            Coordinate Grid
          </Checkbox>
          {options.showGrid && (
            <div className="ml-4 space-y-1 border-l border-border-strong pl-2">
              <Checkbox small checked={options.showGridXY} onChange={(v) => onChange({ showGridXY: v })}>
                XY Plane
              </Checkbox>
              <Checkbox small checked={options.showGridXZ} onChange={(v) => onChange({ showGridXZ: v })}>
                XZ Plane
              </Checkbox>
              <Checkbox small checked={options.showGridYZ} onChange={(v) => onChange({ showGridYZ: v })}>
                YZ Plane
              </Checkbox>
            </div>
          )}
          <Checkbox checked={showTrails} onChange={(v) => onChange({ showTrails: v })}>
            Particle Trails
          </Checkbox>
          <Checkbox checked={options.showStats} onChange={(v) => onChange({ showStats: v })}>
            Performance Stats
          </Checkbox>
          <Checkbox checked={options.showInfoPanel} onChange={(v) => onChange({ showInfoPanel: v })}>
            Info Panel
          </Checkbox>
        </div>
      </Card>

      {/* Camera Controls */}
      <Card className="surface-card border-border p-3">
        <h3 className="text-fg text-sm font-semibold mb-2 flex items-center gap-2">
          <Info className="h-4 w-4" />
          Camera
        </h3>
        <div className="space-y-2">
          <Button
            size="sm"
            variant="outline"
            onClick={onFrameAll}
            className="w-full text-fg border-border-strong bg-surface-raised hover:bg-surface-raised"
          >
            <Maximize className="h-4 w-4 mr-2" />
            Frame All Particles
          </Button>
          <Checkbox checked={options.followCenterOfMass} onChange={(v) => onChange({ followCenterOfMass: v })}>
            Follow Center of Mass
          </Checkbox>
          <Checkbox checked={options.showCenterOfMassMarker} onChange={(v) => onChange({ showCenterOfMassMarker: v })}>
            Show COM Marker
          </Checkbox>
        </div>
      </Card>

      {/* Particle Size */}
      <Card className="surface-card border-border p-3">
        <div className="space-y-3">
          <div className="space-y-1">
            <span id="particle-size-label" className="block text-fg text-sm font-medium">Particle Size</span>
            <Slider
              value={[particleSize]}
              onValueChange={(value) => onChange({ particleSize: value[0] })}
              min={0.1}
              max={2}
              step={0.1}
              aria-labelledby="particle-size-label"
              className="w-32"
            />
            <div className="text-xs text-fg-muted">{particleSize.toFixed(1)}x</div>
          </div>

          {showTrails && (
            <div className="space-y-1">
              <span id="trail-length-label" className="block text-fg text-sm font-medium">Trail Length</span>
              <Slider
                value={[trailLength]}
                onValueChange={(value) => onChange({ trailLength: value[0] })}
                min={10}
                max={1000}
                step={10}
                aria-labelledby="trail-length-label"
                className="w-32"
              />
              <div className="text-xs text-fg-muted">{trailLength} frames</div>
            </div>
          )}

        </div>
      </Card>

      {/* Recording Controls */}
      <Card className="surface-card border-border p-3">
        <h3 className="text-fg text-sm font-semibold mb-2 flex items-center gap-2">
          <Video className="h-4 w-4" />
          Recording
        </h3>
        <div className="space-y-2">
          {!isRecording && !isProcessing && !recordedBlob && (
            <Button
              size="sm"
              onClick={recorder.startRecording}
              className="w-full bg-danger-solid hover:bg-danger-solid/90 text-fg"
            >
              <Video className="h-4 w-4 mr-2" />
              Start Recording
            </Button>
          )}

          {isRecording && (
            <div className="space-y-2">
              <Button
                size="sm"
                onClick={recorder.stopRecording}
                className="w-full bg-danger-solid hover:bg-danger-solid/90 text-fg"
              >
                <Square className="h-4 w-4 mr-2" />
                Stop Recording
              </Button>
              <div className="flex items-center gap-2 text-fg text-sm">
                <div className="w-2 h-2 bg-danger rounded-full animate-pulse"></div>
                <span>{Math.floor(recordingDuration / 60)}:{(recordingDuration % 60).toString().padStart(2, '0')}</span>
              </div>
            </div>
          )}

          {isProcessing && (
            <div className="flex items-center gap-2 text-fg text-sm justify-center py-2">
              <div className="animate-spin rounded-full h-4 w-4 border-b-2 border-fg"></div>
              <span>Processing video...</span>
            </div>
          )}

          {recordedBlob && (
            <div className="space-y-2">
              <Button
                size="sm"
                onClick={recorder.downloadRecording}
                className="w-full bg-success-solid hover:bg-success-solid/90 text-fg"
              >
                <Download className="h-4 w-4 mr-2" />
                Download Video
              </Button>
              <Button
                size="sm"
                onClick={recorder.discardRecording}
                variant="outline"
                className="w-full text-fg border-border-strong hover:bg-surface-raised"
                disabled={isRecording}
              >
                Record New
              </Button>
              <div className="text-xs text-fg-muted">
                {(recordedBlob.size / 1024 / 1024).toFixed(2)} MB
              </div>
            </div>
          )}

          <p className="text-xs text-fg-muted">
            Records at 60fps in WebM format
          </p>
        </div>
      </Card>
    </div>
    </>
  );
}
