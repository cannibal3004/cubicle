'use client';

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  FiActivity,
  FiAlertCircle,
  FiArrowLeft,
  FiCpu,
  FiEye,
  FiHardDrive,
  FiMonitor,
  FiMousePointer,
  FiPause,
  FiPlay,
  FiRefreshCw,
  FiSquare,
  FiTerminal,
  FiUserCheck,
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

export default function ComputerPanel({ bot, onBackToChat, onHandBack }) {
  const [computer, setComputer] = useState(null);
  const [screen, setScreen] = useState(null);
  const [activeTab, setActiveTab] = useState('display');
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

  const handBack = async () => {
    const hadTakeoverRequest = Boolean(takeover);
    await runAction('control', (id) => setComputerControl(id, 'bot'));
    if (hadTakeoverRequest && onHandBack) onHandBack();
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

  return (
    <div className="flex-1 flex flex-col h-screen overflow-hidden bg-slate-950/80 p-6 space-y-4">
      <div className="flex items-center justify-between glass-panel p-4 rounded-2xl border border-slate-800">
        <div className="flex items-center gap-3 min-w-0">
          <button
            onClick={onBackToChat}
            className="w-9 h-9 rounded-xl border border-slate-700 text-slate-400 hover:text-white hover:border-slate-500 flex items-center justify-center transition"
            aria-label="Back to chat"
          >
            <FiArrowLeft />
          </button>
          <div className="w-10 h-10 rounded-xl bg-purple-600/20 text-purple-400 border border-purple-500/30 flex items-center justify-center text-xl">
            <FiMonitor />
          </div>
          <div className="min-w-0">
            <h2 className="text-sm font-bold text-slate-100 flex items-center gap-2">
              Computer
              <span className={`text-[10px] px-2 py-0.5 rounded font-mono border flex items-center gap-1 capitalize ${stateClasses(state)}`}>
                <FiActivity className={state === 'running' ? 'animate-pulse' : ''} /> {prettyState(state)}
              </span>
              {isActive && (
                <span
                  className={`text-[10px] px-2 py-0.5 rounded font-mono border flex items-center gap-1 ${
                    userHasControl
                      ? 'bg-purple-500/20 text-purple-200 border-purple-500/30'
                      : 'bg-slate-800 text-slate-300 border-slate-700'
                  }`}
                >
                  {userHasControl ? <FiUserCheck /> : <FiEye />} {userHasControl ? 'you have control' : 'bot has control'}
                </span>
              )}
            </h2>
            <p className="text-xs text-slate-400 truncate">
              Sandboxed desktop for <span className="text-slate-200 font-semibold">{bot.name || 'Agent'}</span>. Watch it live, or take control.
            </p>
          </div>
        </div>

        <div className="flex items-center gap-2 shrink-0">
          {hasVnc && (
            userHasControl ? (
              <button
                onClick={handBack}
                disabled={actionBusy}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-semibold border bg-purple-600 text-white border-purple-500 hover:bg-purple-500 disabled:opacity-50 transition"
              >
                <FiMousePointer /> {loadingAction === 'control' ? 'Handing back…' : 'Hand back to bot'}
              </button>
            ) : (
              <button
                onClick={takeControl}
                disabled={actionBusy}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-semibold border bg-purple-500/20 text-purple-200 border-purple-500/30 hover:bg-purple-500/30 disabled:opacity-50 transition"
              >
                <FiMousePointer /> {loadingAction === 'control' ? 'Taking control…' : 'Take control'}
              </button>
            )
          )}
          {isActive ? (
            <button
              onClick={() => runAction('pause', pauseComputer)}
              disabled={actionBusy}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-semibold border bg-amber-500/20 text-amber-300 border-amber-500/30 hover:bg-amber-500/30 disabled:opacity-50 transition"
            >
              <FiPause /> {loadingAction === 'pause' ? 'Pausing…' : 'Pause'}
            </button>
          ) : (
            <button
              onClick={() => runAction('start', startComputer)}
              disabled={actionBusy || state === 'starting' || state === 'resetting'}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-semibold border bg-emerald-500/20 text-emerald-300 border-emerald-500/30 hover:bg-emerald-500/30 disabled:opacity-50 transition"
            >
              <FiPlay /> {loadingAction === 'start' ? 'Starting…' : 'Start'}
            </button>
          )}
          <button
            onClick={() => runAction('reset', resetComputer)}
            disabled={actionBusy}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-semibold border bg-slate-800 text-slate-300 border-slate-700 hover:bg-slate-700 disabled:opacity-50 transition"
          >
            <FiRefreshCw className={loadingAction === 'reset' ? 'animate-spin' : ''} /> Reset
          </button>
          <button
            onClick={() => runAction('stop', stopComputer)}
            disabled={actionBusy || state === 'stopped'}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-semibold border bg-slate-800 text-slate-300 border-slate-700 hover:bg-slate-700 disabled:opacity-50 transition"
          >
            <FiSquare /> Stop
          </button>
        </div>
      </div>

      {takeover && (
        <div className="flex items-center justify-between gap-3 rounded-xl border border-purple-500/30 bg-purple-500/10 px-4 py-3 text-xs text-purple-100">
          <div className="flex items-start gap-2 min-w-0">
            <FiUserCheck className="mt-0.5 flex-shrink-0 text-purple-300" />
            <div className="min-w-0">
              <p className="font-semibold">{bot.name || 'The bot'} asked you to take over.</p>
              <p className="text-purple-200/90 mt-0.5 break-words">{takeover.reason}</p>
              <p className="text-[10px] text-purple-300/70 mt-1">
                {userHasControl
                  ? 'You have control. When you are done, press Hand back to bot and the chat will prompt the bot to continue.'
                  : 'Press Take control, then use the desktop below with your mouse and keyboard.'}
              </p>
            </div>
          </div>
          {!userHasControl && hasVnc && (
            <button
              onClick={takeControl}
              disabled={actionBusy}
              className="flex-shrink-0 flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-semibold bg-purple-600 text-white hover:bg-purple-500 disabled:opacity-50 transition"
            >
              <FiMousePointer /> Take control
            </button>
          )}
        </div>
      )}

      {error && (
        <div className="flex items-center gap-2 rounded-xl border border-red-500/30 bg-red-500/10 px-4 py-3 text-xs text-red-200">
          <FiAlertCircle /> {error}
        </div>
      )}

      <div className="flex-1 grid grid-cols-3 gap-6 min-h-0">
        <div className="col-span-2 glass-panel rounded-2xl border border-slate-800 flex flex-col overflow-hidden shadow-2xl relative">
          <div className="p-3 border-b border-slate-800 bg-slate-900/80 flex items-center justify-between">
            <div className="flex items-center gap-2 text-xs font-semibold text-slate-300">
              <span className={`w-2.5 h-2.5 rounded-full ${isActive ? 'bg-emerald-500 animate-pulse' : 'bg-slate-600'}`} />
              {hasVnc ? 'Live desktop' : 'Screen state'} · {computer?.width || 1280}x{computer?.height || 720}
              {hasVnc && <span className="text-slate-500 font-normal">· {vncStatus === 'connected' ? 'streaming' : vncStatus}</span>}
            </div>
            <span className="text-[10px] font-mono text-slate-500">
              {computer?.computer_id || 'not-created'}
            </span>
          </div>

          <div className="flex items-center gap-1 px-3 pt-3">
            {[
              ['display', 'Display'],
              ['files', 'Files'],
              ['activity', 'Activity'],
            ].map(([tab, label]) => (
              <button
                key={tab}
                onClick={() => setActiveTab(tab)}
                className={`px-3 py-1.5 rounded-lg text-[11px] font-semibold transition ${activeTab === tab ? 'bg-blue-500/15 text-blue-300' : 'text-slate-500 hover:text-slate-300'}`}
              >
                {label}
              </button>
            ))}
          </div>

          <div className="flex-1 bg-slate-950 flex items-center justify-center relative p-4 min-h-0">
            {activeTab === 'display' ? (
              hasVnc ? (
                <div
                  className={`w-full h-full rounded-xl border bg-black relative overflow-hidden shadow-2xl ${
                    userHasControl ? 'border-purple-500/60 ring-1 ring-purple-500/40' : 'border-slate-800'
                  }`}
                >
                  <VncViewer
                    key={`${computer?.computer_id}-${computer?.generation}-${state}`}
                    url={vncUrl}
                    viewOnly={!userHasControl}
                    onStatus={setVncStatus}
                  />
                  <span className="absolute top-3 right-3 rounded bg-black/70 px-2 py-1 text-[10px] font-mono text-slate-300 pointer-events-none">
                    {userHasControl ? 'you have control' : 'watching · bot has control'}
                  </span>
                </div>
              ) : screen?.available ? (
                screen.data ? (
                  <div className="w-full h-full rounded-xl border border-slate-800 bg-black flex items-center justify-center relative overflow-hidden shadow-2xl">
                    <img
                      src={`data:image/${screen.format || 'jpeg'};base64,${screen.data}`}
                      alt={`Computer frame ${screen.frame_id || ''}`}
                      className="max-w-full max-h-full object-contain"
                    />
                    <span className="absolute top-3 right-3 rounded bg-black/70 px-2 py-1 text-[10px] font-mono text-slate-300">
                      {screen.frame_id}
                    </span>
                  </div>
                ) : (
                  <div className="w-full h-full rounded-xl border border-slate-800/80 bg-gradient-to-br from-slate-900 via-slate-950 to-blue-950/40 p-4 flex flex-col justify-between shadow-2xl relative overflow-hidden">
                    <div className="flex items-center justify-between text-xs text-slate-400 border-b border-slate-800/60 pb-2">
                      <span className="font-mono text-cyan-400">Provider frame metadata</span>
                      <span>{screen.frame_id}</span>
                    </div>
                    <div className="font-mono text-xs space-y-2 my-auto">
                      <p className="text-emerald-400">✓ Computer is {screen.state}</p>
                      <p className="text-slate-400">Provider: {screen.provider}</p>
                      <p className="text-slate-400">Generation: {screen.generation}</p>
                      <p className="text-amber-400">{screen.message}</p>
                      <p className="text-slate-500 animate-pulse">Polling for the next frame…</p>
                    </div>
                    <div className="flex items-center justify-between text-[10px] text-slate-500 pt-2 border-t border-slate-800/60">
                      <span>Actual pixels require the Docker runtime.</span>
                      <span>{screen.width}x{screen.height}</span>
                    </div>
                  </div>
                )
              ) : (
                <div className="w-full h-full rounded-xl border border-dashed border-slate-800 flex flex-col items-center justify-center text-center p-8">
                  <FiMonitor className="text-4xl text-slate-700 mb-4" />
                  <p className="text-sm font-semibold text-slate-300">The computer is off</p>
                  <p className="text-xs text-slate-500 mt-2 max-w-sm">
                    Press Start to boot the sandbox desktop. The bot can also start it itself when it needs it.
                  </p>
                </div>
              )
            ) : activeTab === 'files' ? (
              <ComputerFiles bot={bot} />
            ) : (
              <div className="w-full h-full rounded-xl border border-slate-800 bg-slate-950 p-5 font-mono text-xs space-y-2 overflow-auto">
                <p className="text-slate-500">[provider] {computer?.provider || 'fake'} adapter</p>
                <p className="text-slate-400">[state] {prettyState(state)}</p>
                <p className="text-slate-400">[health] {computer?.health || 'unknown'}</p>
                <p className="text-slate-400">[control] {computer?.controlled_by || 'bot'}</p>
                <p className="text-slate-400">[operation] {computer?.last_operation || 'none'}</p>
                <p className="text-slate-400">[browser] {computer?.url || 'no page'}</p>
                <p className="text-blue-400">[note] All lifecycle calls are recorded by the action gateway.</p>
              </div>
            )}
          </div>
        </div>

        <div className="space-y-4 flex flex-col">
          <div className="glass-panel p-4 rounded-2xl border border-slate-800 space-y-3">
            <h3 className="text-xs font-bold text-slate-200 uppercase tracking-wider flex items-center gap-2">
              <FiCpu className="text-blue-400" /> Status
            </h3>
            <div className="space-y-2 text-xs">
              <div className="flex justify-between text-slate-400"><span>Provider</span><span className="text-slate-200 font-mono">{computer?.provider || 'fake'}</span></div>
              <div className="flex justify-between text-slate-400"><span>State</span><span className="text-slate-200 capitalize">{prettyState(state)}</span></div>
              <div className="flex justify-between text-slate-400"><span>Health</span><span className="text-slate-200 capitalize">{computer?.health || 'unknown'}</span></div>
              <div className="flex justify-between text-slate-400"><span>Control</span><span className="text-slate-200 capitalize">{computer?.controlled_by || 'bot'}</span></div>
              <div className="flex justify-between text-slate-400"><span>Generation</span><span className="text-slate-200 font-mono">{computer?.generation ?? 0}</span></div>
              <div className="flex justify-between text-slate-400"><span>Last operation</span><span className="text-slate-200 font-mono">{computer?.last_operation || 'none'}</span></div>
              <div className="flex justify-between gap-3 text-slate-400">
                <span className="shrink-0">Packages</span>
                <span className="text-slate-200 font-mono text-right break-words min-w-0" title="Ubuntu packages this computer installed; restored at every start">
                  {computer?.packages?.length ? computer.packages.join(', ') : 'none'}
                </span>
              </div>
            </div>
          </div>

          <div className="glass-panel p-4 rounded-2xl border border-slate-800 space-y-3 flex-1 flex flex-col min-h-0">
            <h3 className="text-xs font-bold text-slate-200 uppercase tracking-wider flex items-center gap-2">
              <FiTerminal className="text-purple-400" /> What is inside
            </h3>
            <div className="text-[11px] text-slate-400 leading-relaxed space-y-1.5">
              <p>Ubuntu with a Plank dock: Chromium, Files, Terminal. Node, Python with pip, git, curl, jq.</p>
              <p><span className="font-mono text-slate-300">/workspace</span> and the home directory are kept between starts; <span className="font-mono text-slate-300">/tmp</span> is wiped. <span className="font-mono text-slate-300">/shared</span> is the shared folder on the host, when one is configured.</p>
              <p>While you have control the bot cannot click, type, or navigate; it can still take screenshots.</p>
            </div>
            {loading ? (
              <p className="text-xs text-slate-500">Loading…</p>
            ) : (
              <div className="flex-1 bg-slate-950 p-3 rounded-xl border border-slate-800 space-y-2 overflow-y-auto">
                {capabilities.map((capability) => (
                  <div key={capability} className="flex items-center justify-between text-[11px] font-mono">
                    <span className="text-slate-400">{capability}</span>
                    <span className="text-emerald-400">declared</span>
                  </div>
                ))}
                {!capabilities.length && <p className="text-[11px] text-slate-600">No computer has been created yet.</p>}
              </div>
            )}
            <div className="flex items-center gap-2 text-[10px] text-slate-500">
              <FiHardDrive /> Sandbox has internet access; do not sign in to anything you would not type into a shared machine.
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
