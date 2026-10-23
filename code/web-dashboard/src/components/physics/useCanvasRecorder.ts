// SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
// SPDX-License-Identifier: Apache-2.0
'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

// How long a downloaded recording's object URL stays valid: revoking it right
// after click() can cancel the download before the browser has read the blob
const REVOKE_DELAY_MS = 60_000;

/**
 * Records a canvas to a WebM video with MediaRecorder. setCanvas registers the
 * canvas (the three.js renderer's, from inside <Canvas>).
 */
export function useCanvasRecorder() {
  const [isRecording, setIsRecording] = useState(false);
  const [isProcessing, setIsProcessing] = useState(false);
  const [recordedBlob, setRecordedBlob] = useState<Blob | null>(null);
  const [recordingDuration, setRecordingDuration] = useState(0);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const mediaStreamRef = useRef<MediaStream | null>(null);
  const recordedChunksRef = useRef<Blob[]>([]);
  const recordingStartTimeRef = useRef<number>(0);
  const recordingTimerRef = useRef<number | null>(null);

  const setCanvas = useCallback((canvas: HTMLCanvasElement) => {
    canvasRef.current = canvas;
  }, []);

  // Timer, stream tracks and recorder reference, after a stop or an error
  const releaseRecorder = useCallback(() => {
    if (recordingTimerRef.current !== null) {
      clearInterval(recordingTimerRef.current);
      recordingTimerRef.current = null;
    }
    if (mediaStreamRef.current) {
      mediaStreamRef.current.getTracks().forEach(track => track.stop());
      mediaStreamRef.current = null;
    }
    mediaRecorderRef.current = null;
  }, []);

  const startRecording = useCallback(() => {
    if (isRecording || isProcessing || mediaRecorderRef.current) {
      console.warn('Recording already in progress or processing');
      return;
    }

    if (!canvasRef.current) {
      console.error('Canvas not available for recording');
      alert('Canvas not ready. Please wait a moment and try again.');
      return;
    }

    if (!canvasRef.current.captureStream) {
      alert('Your browser does not support canvas recording. Please use Chrome, Firefox, or Edge.');
      return;
    }

    if (typeof MediaRecorder === 'undefined') {
      alert('Your browser does not support MediaRecorder API. Please update your browser.');
      return;
    }

    try {
      const stream = canvasRef.current.captureStream(60);

      if (stream.getVideoTracks().length === 0) {
        throw new Error('No video tracks available from canvas');
      }

      mediaStreamRef.current = stream;

      const mimeType = MediaRecorder.isTypeSupported('video/webm;codecs=vp9')
        ? 'video/webm;codecs=vp9'
        : MediaRecorder.isTypeSupported('video/webm;codecs=vp8')
        ? 'video/webm;codecs=vp8'
        : 'video/webm';

      const mediaRecorder = new MediaRecorder(stream, {
        mimeType,
        videoBitsPerSecond: 8000000, // 8 Mbps
      });

      recordedChunksRef.current = [];

      mediaRecorder.ondataavailable = (event) => {
        if (event.data.size > 0) {
          recordedChunksRef.current.push(event.data);
        }
      };

      mediaRecorder.onerror = (event) => {
        console.error('MediaRecorder error:', event);
        alert('Recording error occurred. Please try again.');
        setIsRecording(false);
        setIsProcessing(false);
        releaseRecorder();
      };

      mediaRecorder.onstop = () => {
        const blob = new Blob(recordedChunksRef.current, { type: mimeType });
        setRecordedBlob(blob);
        setIsProcessing(false);
        releaseRecorder();
      };

      mediaRecorder.start(100); // a data chunk every 100 ms
      mediaRecorderRef.current = mediaRecorder;
      setIsRecording(true);
      setRecordedBlob(null);
      recordingStartTimeRef.current = Date.now();
      setRecordingDuration(0);

      recordingTimerRef.current = window.setInterval(() => {
        setRecordingDuration(Math.floor((Date.now() - recordingStartTimeRef.current) / 1000));
      }, 1000);
    } catch (error) {
      console.error('Failed to start recording:', error);
      alert(`Failed to start recording: ${error instanceof Error ? error.message : 'Unknown error'}`);
      setIsRecording(false);
      setIsProcessing(false);
      releaseRecorder();
    }
  }, [isRecording, isProcessing, releaseRecorder]);

  const stopRecording = useCallback(() => {
    if (mediaRecorderRef.current && mediaRecorderRef.current.state !== 'inactive') {
      setIsRecording(false);
      setIsProcessing(true);
      mediaRecorderRef.current.stop();
    }
  }, []);

  const downloadRecording = useCallback(() => {
    if (!recordedBlob) return;

    const url = URL.createObjectURL(recordedBlob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `nbody-simulation-${Date.now()}.webm`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), REVOKE_DELAY_MS);
  }, [recordedBlob]);

  // Drop the recorded video to record a new one
  const discardRecording = useCallback(() => {
    if (!isRecording) {
      setRecordedBlob(null);
      setRecordingDuration(0);
    }
  }, [isRecording]);

  useEffect(() => {
    return () => {
      if (recordingTimerRef.current !== null) {
        clearInterval(recordingTimerRef.current);
      }
      if (mediaRecorderRef.current && mediaRecorderRef.current.state !== 'inactive') {
        mediaRecorderRef.current.stop();
      }
      if (mediaStreamRef.current) {
        mediaStreamRef.current.getTracks().forEach(track => track.stop());
      }
    };
  }, []);

  return {
    isRecording,
    isProcessing,
    recordedBlob,
    recordingDuration,
    setCanvas,
    startRecording,
    stopRecording,
    downloadRecording,
    discardRecording,
  };
}
