'use client';

import React, { useEffect, useRef, useState } from 'react';

// Live view of a sandbox desktop through noVNC. The WebSocket goes to the
// API, which checks the session and bridges to the sandbox; nothing here
// knows the sandbox token. `viewOnly` decides whether this browser's mouse
// and keyboard reach the desktop.
// `onClick` is only used while viewing: noVNC's canvas swallows mouse events
// even in view-only mode, so a transparent layer above it takes the click
// (and shows a normal pointer, which the canvas hides when the desktop
// sends no cursor shape).
export default function VncViewer({ url, viewOnly = true, onStatus, onClick }) {
  const containerRef = useRef(null);
  const rfbRef = useRef(null);
  const [status, setStatus] = useState('connecting');
  const [detail, setDetail] = useState('');

  useEffect(() => {
    let disposed = false;
    let rfb = null;
    setStatus('connecting');
    setDetail('');

    (async () => {
      // noVNC touches `window` at import time, so load it only in the browser.
      const { default: RFB } = await import('@novnc/novnc');
      if (disposed || !containerRef.current) return;
      rfb = new RFB(containerRef.current, url, { wsProtocols: [] });
      rfb.viewOnly = viewOnly;
      rfb.scaleViewport = true;
      rfb.resizeSession = false;
      rfb.clipViewport = false;
      // A dot where the pointer is when the desktop sends no cursor shape,
      // rather than no pointer at all while controlling it.
      rfb.showDotCursor = true;
      rfb.background = '#000';
      rfb.addEventListener('connect', () => {
        if (disposed) return;
        setStatus('connected');
        if (onStatus) onStatus('connected');
      });
      rfb.addEventListener('disconnect', (event) => {
        if (disposed) return;
        setStatus('disconnected');
        setDetail(event?.detail?.clean ? 'Connection closed.' : 'Connection lost.');
        if (onStatus) onStatus('disconnected');
      });
      rfbRef.current = rfb;
    })().catch((err) => {
      if (disposed) return;
      setStatus('error');
      setDetail(err?.message || 'Could not load the viewer.');
    });

    return () => {
      disposed = true;
      rfbRef.current = null;
      try {
        if (rfb) rfb.disconnect();
      } catch {
        // Already closed.
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [url]);

  useEffect(() => {
    if (rfbRef.current) rfbRef.current.viewOnly = viewOnly;
  }, [viewOnly]);

  return (
    <div className="relative w-full h-full">
      <div ref={containerRef} className="w-full h-full" style={{ outline: 'none' }} />
      {viewOnly && onClick && status === 'connected' && (
        <div
          className="absolute inset-0 cursor-zoom-in"
          onClick={onClick}
          role="button"
          aria-label="Enlarge the desktop"
          title="Click to enlarge"
        />
      )}
      {status !== 'connected' && (
        <div className="absolute inset-0 flex flex-col items-center justify-center bg-black/70 text-xs text-slate-300 gap-1">
          <span>{status === 'connecting' ? 'Connecting to the desktop…' : status === 'error' ? 'Viewer error' : 'Disconnected'}</span>
          {detail && <span className="text-[10px] text-slate-500">{detail}</span>}
        </div>
      )}
    </div>
  );
}
