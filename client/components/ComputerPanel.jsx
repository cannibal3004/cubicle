'use client';

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  FiActivity,
  FiAlertCircle,
  FiArrowLeft,
  FiEye,
  FiHardDrive,
  FiMaximize2,
  FiMonitor,
  FiMousePointer,
  FiPause,
  FiPlay,
  FiRefreshCw,
  FiSquare,
  FiUserCheck,
  FiX,
} from 'react-icons/fi';

import {
  computerVncUrl,
  fetchComputerScreenshot,
  fetchComputerStatus,
  pauseComputer,
  resetComputer,
  setComputerControl,
  startComputer,
  stopComputer,
} from '../lib/api';
import VncViewer from './VncViewer';
import ComputerFiles from './ComputerFiles';

const ACTIVE_STATES = new Set(['running', 'paused']);

function stateClasses(state) {
  if (state === 'running') return 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30';
  if (state === 'paused') return 'bg-amber-500/20 text-amber-300 border-amber-500/30';
  if (state === 'starting' || state === 'resetting') return 'bg-blue-500/20 text-blue-300 border-blue-500/30';
  if (state === 'error') return 'bg-red-500/20 text-red-300 border-red-500/30';
  return 'bg-slate-800 text-slate-300 border-slate-700';
}
function prettyState(state) {
  return (state || 'stopped').replace(/_/g, ' ');
}

const TABS = [
  ['display', 'Display'],
  ['files', 'Files'],
  ['activity', 'Activity'],
];

// The bot's computer: a control bar, then one big area with the live
// desktop, the file browser or the activity log. Clicking the desktop (or
// the maximize button) fills the window with it, with the control buttons
// underneath; a takeover request from the chat opens straight into that.
export default function ComputerPanel({ bot, onBackToChat, onHandBack, initial }) {
  const [computer, setComputer] = useState(null);
  const [screen, setScreen] = useState(null);
  const [activeTab, setActiveTab] = useState(initial?.tab || 'display');
  const [maximized, setMaximized] = useState(Boolean(initial?.maximize));
  const [loading, setLoading] = useState(true);
  const [loadingAction, setLoadingAction] = useState('');
  const [error, setError] = useState('');
  const [vncStatus, setVncStatus] = useState('connecting');

  const botId = bot?.id;
  const state = computer?.state || 'stopped';
  const isActive = ACTIVE_STATES.has(state);
  const capabilities = useMemo(() => computer?.capabilities || [], [computer]);
  // Live desktop over VNC is available from the Docker runtime; the fake
  // adapter only reports frame metadata, so it keeps the polling view.
  const hasVnc = isActive && capabilities.includes('vnc');
  const userHasControl = computer?.controlled_by === 'user';
  const takeover = computer?.takeover_request || null;
  const width = computer?.width || 1920;
  const height = computer?.height || 1200;

  // The chat can reopen the page on a given tab, or maximized for a takeover.
  useEffect(() => {
    if (!initial) return;
    if (initial.tab) setActiveTab(initial.tab);
    if (initial.maximize !== undefined) setMaximized(Boolean(initial.maximize));
  }, [initial?.nonce]); // eslint-disable-line react-hooks/exhaustive-deps

  const refresh = useCallback(async () => {
    if (!botId) return;
    try {
      const statusPayload = await fetchComputerStatus(botId);
      const nextComputer = statusPayload.status;
      setComputer(nextComputer);
      const active = ACTIVE_STATES.has(nextComputer.state);
      const live = active && (nextComputer.capabilities || []).includes('vnc');
      if (active && !live) {
        const screenPayload = await fetchComputerScreenshot(botId);
        setScreen(screenPayload.result || null);
      } else if (!active) {
        setScreen(null);
      }
      setError('');
    } catch (err) {
      setError(err.message || 'Computer provider is unavailable.');
    } finally {
      setLoading(false);
    }
  }, [botId]);

  useEffect(() => {
    let disposed = false;
    setComputer(null);
    setScreen(null);
    setError('');
    setLoading(true);
    refresh();
    const interval = window.setInterval(() => {
      if (!disposed) refresh();
    }, 4000);
    return () => {
      disposed = true;
      window.clearInterval(interval);
    };
  }, [botId, refresh]);

  // Escape leaves the maximized view.
  useEffect(() => {
    if (!maximized) return undefined;
    const onKey = (event) => {
      if (event.key === 'Escape') setMaximized(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [maximized]);

  const runAction = async (name, operation) => {
    if (!botId) return;
    setLoadingAction(name);
    setError('');
    try {
      await operation(botId);
      await refresh();
    } catch (err) {
      setError(err.message || `Computer ${name} failed.`);
    } finally {
      setLoadingAction('');
    }
  };

  const takeControl = () => runAction('control', (id) => setComputerControl(id, 'user'));

  // Handing back after the bot asked for help also tells the bot to carry on.
  const handBack = async () => {
    const hadTakeoverRequest = Boolean(takeover);
    await runAction('control', (id) => setComputerControl(id, 'bot'));
    if (hadTakeoverRequest) {
      setMaximized(false);
      if (onHandBack) onHandBack();
    }
  };

  if (!bot) {
    return (
      <div className="flex-1 flex items-center justify-center bg-slate-950/80 p-6 text-sm text-slate-400">
        Select a bot to view its computer.
      </div>
    );
  }

  const actionBusy = Boolean(loadingAction);
  const vncUrl = botId ? computerVncUrl(botId) : '';
  const viewerKey = `${computer?.computer_id}-${computer?.generation}-${state}-${maximized ? 'max' : 'inline'}`;

  const controlButton = (large = false) =>
    hasVnc ? (
      userHasControl ? (
        <button
          onClick={handBack}
          disabled={actionBusy}
          className={`flex items-center gap-1.5 rounded-xl font-semibold border bg-purple-600 text-white border-purple-500 hover:bg-purple-500 disabled:opacity-50 transition ${large ? 'px-4 py-2 text-sm' : 'px-3 py-1.5 text-xs'}`}
        >
          <FiMousePointer /> {loadingAction === 'control' ? 'Handing back…' : takeover ? "I'm done, hand back to the bot" : 'Stop controlling'}
        </button>
      ) : (
        <button
          onClick={takeControl}
          disabled={actionBusy}
          className={`flex items-center gap-1.5 rounded-xl font-semibold border bg-purple-500/20 text-purple-200 border-purple-500/30 hover:bg-purple-500/30 disabled:opacity-50 transition ${large ? 'px-4 py-2 text-sm' : 'px-3 py-1.5 text-xs'}`}
        >
          <FiMousePointer /> {loadingAction === 'control' ? 'Taking control…' : 'Take control'}
        </button>
      )
    ) : null;

  const liveDesktop = (
    <div
      className={`w-full h-full rounded-xl border bg-black relative overflow-hidden shadow-2xl ${
        userHasControl ? 'border-purple-500/60 ring-1 ring-purple-500/40' : 'border-slate-800'
      }`}
    >
      <VncViewer key={viewerKey} url={vncUrl} viewOnly={!userHasControl} onStatus={setVncStatus} />
      <span className="absolute top-3 right-3 rounded bg-black/70 px-2 py-1 text-[10px] font-mono text-slate-300 pointer-events-none">
        {userHasControl ? 'you have control' : 'watching · bot has control'}
      </span>
    </div>
  );

  const display = hasVnc ? (
    // While the bot has control the viewer ignores the mouse, so a click enlarges it.
    <div
      className={`w-full h-full ${userHasControl ? '' : 'cursor-zoom-in'}`}
      onClick={() => {
        if (!userHasControl) setMaximized(true);
      }}
      title={userHasControl ? '' : 'Click to enlarge'}
    >
      {liveDesktop}
    </div>
  ) : screen?.available ? (
    screen.data ? (
      <div className="w-full h-full rounded-xl border border-slate-800 bg-black flex items-center justify-center relative overflow-hidden shadow-2xl">
        <img src={`data:image/${screen.format || 'jpeg'};base64,${screen.data}`} alt={`Computer frame ${screen.frame_id || ''}`} className="max-w-full max-h-full object-contain" />
        <span className="absolute top-3 right-3 rounded bg-black/70 px-2 py-1 text-[10px] font-mono text-slate-300">{screen.frame_id}</span>
      </div>
    ) : (
      <div className="w-full h-full rounded-xl border border-slate-800/80 bg-slate-950 p-4 flex flex-col justify-between font-mono text-xs">
        <div className="flex items-center justify-between text-slate-400 border-b border-slate-800/60 pb-2">
          <span className="text-cyan-400">Provider frame metadata</span>
          <span>{screen.frame_id}</span>
        </div>
        <div className="space-y-2 my-auto">
          <p className="text-emerald-400">✓ Computer is {screen.state}</p>
          <p className="text-slate-400">Provider: {screen.provider}</p>
          <p className="text-amber-400">{screen.message}</p>
        </div>
      </div>
    )
  ) : (
    <div className="w-full h-full rounded-xl border border-dashed border-slate-800 flex flex-col items-center justify-center text-center p-8">
      <FiMonitor className="text-4xl text-slate-700 mb-4" />
      <p className="text-sm font-semibold text-slate-300">{state === 'starting' || state === 'resetting' ? `The computer is ${prettyState(state)}…` : 'The computer is off'}</p>
      <p className="text-xs text-slate-500 mt-2 max-w-md">
        Press Start to boot it, or let the bot start it when it needs it. Ubuntu with Chromium, a terminal, Node and Python;
        <span className="font-mono text-slate-400"> /workspace</span> and the home directory are kept between starts,
        <span className="font-mono text-slate-400"> /shared</span> is the shared folder. It has internet access: do not sign in to anything you would not type into a shared machine.
      </p>
    </div>
  );

  const activity = (
    <div className="w-full h-full rounded-xl border border-slate-800 bg-slate-950 p-5 font-mono text-xs space-y-2 overflow-auto">
      <p className="text-slate-500">[provider] {computer?.provider || 'fake'} adapter</p>
      <p className="text-slate-400">[state] {prettyState(state)}</p>
      <p className="text-slate-400">[health] {computer?.health || 'unknown'}</p>
      <p className="text-slate-400">[control] {computer?.controlled_by || 'bot'}</p>
      <p className="text-slate-400">[screen] {width}x{height}</p>
      <p className="text-slate-400">[generation] {computer?.generation ?? 0}</p>
      <p className="text-slate-400">[operation] {computer?.last_operation || 'none'}</p>
      <p className="text-slate-400">[browser] {computer?.url || 'no page'}</p>
      <p className="text-slate-400">[packages] {computer?.packages?.length ? computer.packages.join(', ') : 'none'}</p>
      <p className="text-slate-400">[capabilities] {capabilities.length ? capabilities.join(', ') : 'none yet'}</p>
      <p className="text-slate-500">[id] {computer?.computer_id || 'not created'}</p>
      <p className="text-blue-400">[note] All lifecycle calls are recorded by the action gateway.</p>
    </div>
  );

  return (
    <div className="flex-1 flex flex-col h-screen overflow-hidden bg-slate-950/80 p-4 gap-3">
      {/* Control bar */}
      <div className="flex items-center justify-between gap-3 glass-panel px-4 py-3 rounded-2xl border border-slate-800">
        <div className="flex items-center gap-3 min-w-0">
          <button
            onClick={onBackToChat}
            className="w-9 h-9 rounded-xl border border-slate-700 text-slate-400 hover:text-white hover:border-slate-500 flex items-center justify-center transition flex-shrink-0"
            aria-label="Back to chat"
          >
            <FiArrowLeft />
          </button>
          <div className="w-9 h-9 rounded-xl bg-purple-600/20 text-purple-400 border border-purple-500/30 flex items-center justify-center text-lg flex-shrink-0">
            <FiMonitor />
          </div>
          <div className="min-w-0">
            <h2 className="text-sm font-bold text-slate-100 flex items-center gap-2 flex-wrap">
              <span className="truncate">{bot.name || 'Agent'}&apos;s computer</span>
              <span className={`text-[10px] px-2 py-0.5 rounded font-mono border flex items-center gap-1 capitalize ${stateClasses(state)}`}>
                <FiActivity className={state === 'running' ? 'animate-pulse' : ''} /> {prettyState(state)}
              </span>
              {isActive && (
                <span className={`text-[10px] px-2 py-0.5 rounded font-mono border flex items-center gap-1 ${userHasControl ? 'bg-purple-500/20 text-purple-200 border-purple-500/30' : 'bg-slate-800 text-slate-300 border-slate-700'}`}>
                  {userHasControl ? <FiUserCheck /> : <FiEye />} {userHasControl ? 'you have control' : 'bot has control'}
                </span>
              )}
              <span className="text-[10px] font-mono text-slate-500">{width}x{height}{hasVnc ? ` · ${vncStatus}` : ''}</span>
            </h2>
          </div>
        </div>

        <div className="flex items-center gap-2 shrink-0">
          {controlButton()}
          {isActive ? (
            <button onClick={() => runAction('pause', pauseComputer)} disabled={actionBusy} className="flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-semibold border bg-amber-500/20 text-amber-300 border-amber-500/30 hover:bg-amber-500/30 disabled:opacity-50 transition">
              <FiPause /> {loadingAction === 'pause' ? 'Pausing…' : 'Pause'}
            </button>
          ) : (
            <button onClick={() => runAction('start', startComputer)} disabled={actionBusy || state === 'starting' || state === 'resetting'} className="flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-semibold border bg-emerald-500/20 text-emerald-300 border-emerald-500/30 hover:bg-emerald-500/30 disabled:opacity-50 transition">
              <FiPlay /> {loadingAction === 'start' ? 'Starting…' : 'Start'}
            </button>
          )}
          <button onClick={() => runAction('reset', resetComputer)} disabled={actionBusy} className="flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-semibold border bg-slate-800 text-slate-300 border-slate-700 hover:bg-slate-700 disabled:opacity-50 transition" title="Recreate the computer: packages are reinstalled, /workspace and the home directory are kept">
            <FiRefreshCw className={loadingAction === 'reset' ? 'animate-spin' : ''} /> Reset
          </button>
          <button onClick={() => runAction('stop', stopComputer)} disabled={actionBusy || state === 'stopped'} className="flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-semibold border bg-slate-800 text-slate-300 border-slate-700 hover:bg-slate-700 disabled:opacity-50 transition">
            <FiSquare /> Stop
          </button>
        </div>
      </div>

      {takeover && (
        <div className="flex items-center justify-between gap-3 rounded-xl border border-purple-500/30 bg-purple-500/10 px-4 py-2.5 text-xs text-purple-100">
          <div className="flex items-start gap-2 min-w-0">
            <FiUserCheck className="mt-0.5 flex-shrink-0 text-purple-300" />
            <div className="min-w-0">
              <p className="font-semibold">{bot.name || 'The bot'} asked you to take over: <span className="font-normal text-purple-200/90">{takeover.reason}</span></p>
              <p className="text-[10px] text-purple-300/70 mt-0.5">
                {userHasControl ? 'You have control. When you are done, press the button and the bot carries on.' : 'Take control, do the step, then hand back.'}
              </p>
            </div>
          </div>
          {!userHasControl && hasVnc && (
            <button onClick={() => { setMaximized(true); takeControl(); }} disabled={actionBusy} className="flex-shrink-0 flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-semibold bg-purple-600 text-white hover:bg-purple-500 disabled:opacity-50 transition">
              <FiMousePointer /> Take control
            </button>
          )}
        </div>
      )}

      {error && (
        <div className="flex items-center gap-2 rounded-xl border border-red-500/30 bg-red-500/10 px-4 py-2.5 text-xs text-red-200">
          <FiAlertCircle /> {error}
        </div>
      )}

      {/* Tabs and the one big area */}
      <div className="flex-1 flex flex-col min-h-0 glass-panel rounded-2xl border border-slate-800 overflow-hidden">
        <div className="flex items-center gap-1 px-3 pt-3 pb-2">
          {TABS.map(([tab, label]) => (
            <button
              key={tab}
              onClick={() => setActiveTab(tab)}
              className={`px-3 py-1.5 rounded-lg text-[11px] font-semibold transition ${activeTab === tab ? 'bg-blue-500/15 text-blue-300' : 'text-slate-500 hover:text-slate-300'}`}
            >
              {label}
            </button>
          ))}
          <span className="flex-1" />
          {activeTab === 'display' && hasVnc && (
            <button onClick={() => setMaximized(true)} className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-[11px] text-slate-400 hover:text-white hover:bg-slate-800 transition" title="Fill the window with the desktop">
              <FiMaximize2 /> Maximize
            </button>
          )}
        </div>
        <div className="flex-1 bg-slate-950 relative p-3 pt-0 min-h-0">
          {activeTab === 'display' && (loading && !computer ? <div className="w-full h-full flex items-center justify-center text-xs text-slate-500">Loading…</div> : maximized && hasVnc ? (
            <div className="w-full h-full rounded-xl border border-slate-800 flex items-center justify-center text-xs text-slate-500">Showing in the large view.</div>
          ) : display)}
          {activeTab === 'files' && <ComputerFiles bot={bot} />}
          {activeTab === 'activity' && activity}
        </div>
      </div>

      <div className="flex items-center gap-2 text-[10px] text-slate-500 px-1">
        <FiHardDrive /> Sandbox has internet access; do not sign in to anything you would not type into a shared machine.
      </div>

      {/* The large view: the desktop at up to 95% of the window, aspect kept, controls underneath. */}
      {maximized && hasVnc && (
        <div className="fixed inset-0 z-50 bg-black/95 flex flex-col items-center justify-center gap-3 p-4" onMouseDown={(event) => { if (event.target === event.currentTarget) setMaximized(false); }}>
          <div style={{ aspectRatio: `${width} / ${height}`, width: `min(95vw, calc((95vh - 72px) * ${width / height}))` }} className="max-w-[95vw]">
            {liveDesktop}
          </div>
          <div className="flex items-center gap-3">
            {takeover && !userHasControl && (
              <span className="text-xs text-purple-200 max-w-md truncate" title={takeover.reason}>{bot.name || 'The bot'} asked: {takeover.reason}</span>
            )}
            {controlButton(true)}
            <button onClick={() => setMaximized(false)} className="flex items-center gap-1.5 px-4 py-2 rounded-xl text-sm font-semibold border bg-slate-800 text-slate-200 border-slate-700 hover:bg-slate-700 transition" title="Escape">
              <FiX /> Close
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
