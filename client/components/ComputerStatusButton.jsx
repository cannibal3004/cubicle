'use client';

import React, { useEffect, useRef, useState } from 'react';
import { FiMonitor } from 'react-icons/fi';
import { fetchComputerStatus } from '../lib/api';

const DOT = {
  running: 'bg-emerald-400',
  paused: 'bg-amber-400',
  starting: 'bg-cyan-400 animate-pulse',
  resetting: 'bg-cyan-400 animate-pulse',
  error: 'bg-red-400',
};

// The computer button in the chat header. Hovering it shows the computer's
// status (fetched on hover, refreshed while the pointer stays); clicking
// opens the computer page.
export default function ComputerStatusButton({ bot, onClick }) {
  const [hover, setHover] = useState(false);
  const [status, setStatus] = useState(null);
  const [error, setError] = useState('');
  const timer = useRef(null);
  const botId = bot?.id;

  useEffect(() => {
    if (!hover || !botId) return undefined;
    let disposed = false;
    const load = async () => {
      try {
        const payload = await fetchComputerStatus(botId);
        if (!disposed) {
          setStatus(payload.status || null);
          setError('');
        }
      } catch (err) {
        if (!disposed) setError(err?.message || 'No status.');
      }
    };
    load();
    timer.current = window.setInterval(load, 4000);
    return () => {
      disposed = true;
      window.clearInterval(timer.current);
    };
  }, [hover, botId]);

  const state = status?.state || 'stopped';
  const dot = DOT[state] || 'bg-zinc-600';

  return (
    <div className="relative" onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}>
      <button
        suppressHydrationWarning={true}
        onClick={onClick}
        className="relative p-1.5 rounded-lg text-zinc-400 hover:text-white hover:bg-[#1f1f23] transition"
        title="Open the bot's computer"
      >
        <FiMonitor className="text-base" />
        {status && state !== 'stopped' && <span className={`absolute top-1 right-1 w-1.5 h-1.5 rounded-full ${dot}`} />}
      </button>
      {hover && botId && (
        <div className="absolute right-0 mt-1 w-64 rounded-xl bg-[#111114] border border-[#26262b] shadow-2xl z-50 p-3 text-[11px] text-zinc-300">
          {!status && !error && <p className="text-zinc-500">Loading…</p>}
          {error && <p className="text-rose-300">{error}</p>}
          {status && (
            <div className="space-y-1">
              <div className="flex items-center justify-between">
                <span className="text-zinc-500">State</span>
                <span className="flex items-center gap-1.5 capitalize text-zinc-100">
                  <span className={`w-1.5 h-1.5 rounded-full ${dot}`} /> {state.replace(/_/g, ' ')}
                </span>
              </div>
              <div className="flex items-center justify-between"><span className="text-zinc-500">Health</span><span className="capitalize">{status.health || 'unknown'}</span></div>
              <div className="flex items-center justify-between"><span className="text-zinc-500">Control</span><span className="capitalize">{status.controlled_by || 'bot'}</span></div>
              <div className="flex items-center justify-between"><span className="text-zinc-500">Screen</span><span className="font-mono">{status.width}x{status.height}</span></div>
              <div className="flex items-center justify-between"><span className="text-zinc-500">Last operation</span><span className="font-mono truncate max-w-[9rem]">{status.last_operation || 'none'}</span></div>
              {status.takeover_request && <p className="text-purple-300 pt-1">Asked you to take over: {status.takeover_request.reason}</p>}
              <div className="pt-1 border-t border-[#26262b]">
                <span className="text-zinc-500">Packages </span>
                <span className="font-mono break-words">{status.packages?.length ? status.packages.join(', ') : 'none'}</span>
              </div>
              <p className="text-zinc-600 pt-1">Click to open the computer.</p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
