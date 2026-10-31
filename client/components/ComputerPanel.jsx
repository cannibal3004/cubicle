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
  FiSend,
  FiSquare,
  FiUserCheck,
  FiVideo,
  FiX,
} from 'react-icons/fi';

import {
  computerVncUrl,
  fetchComputerRecording,
  fetchComputerScreenshot,
  fetchComputerStatus,
  pauseComputer,
  resetComputer,
  setComputerControl,
  startComputer,
  startComputerRecording,
  stopComputer,
  stopComputerRecording,
} from '../lib/api';
import VncViewer from './VncViewer';
import ComputerFiles from './ComputerFiles';

const ACTIVE_STATES = new Set(['running', 'paused']);

function stateClasses(state) {
  if (state === 'running') return 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30';
  if (state === 'paused') return 'bg-amber-500/20 text-amber-300 border-amber-500/30';
  if (state === 'starting' || state === 'resetting') return 'bg-cyan-500/20 text-cyan-300 border-cyan-500/30';
  if (state === 'error') return 'bg-red-500/20 text-red-300 border-red-500/30';
  return 'bg-zinc-800 text-zinc-300 border-zinc-700';
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
export default function ComputerPanel({ bot, onBackToChat, onHandBack, onDemonstrated, initial }) {
  const [computer, setComputer] = useState(null);
  const [screen, setScreen] = useState(null);
  const [activeTab, setActiveTab] = useState(initial?.tab || 'display');
  const [maximized, setMaximized] = useState(Boolean(initial?.maximize));
  const [loading, setLoading] = useState(true);
  const [loadingAction, setLoadingAction] = useState('');
  const [error, setError] = useState('');
  const [vncStatus, setVncStatus] = useState('connecting');
  // A demonstration: `recording` while the user's actions are being recorded
  // ({ steps }), then `finished` (the saved recording) until it is sent.
  const [recording, setRecording] = useState(null);
  const [finished, setFinished] = useState(null);
  const [note, setNote] = useState('');

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

  // The step count while recording, and a recording in progress found again after a reload.
  useEffect(() => {
    if (!botId || !isActive) return undefined;
    let disposed = false;
    const check = async () => {
      try {
        const status = await fetchComputerRecording(botId);
        if (!disposed) setRecording(status.active ? { steps: status.steps || 0 } : null);
      } catch {
        // the runtime may still be starting
      }
    };
    check();
    const interval = recording ? window.setInterval(check, 2000) : null;
    return () => {
      disposed = true;
      if (interval) window.clearInterval(interval);
    };
  }, [botId, isActive, Boolean(recording)]); // eslint-disable-line react-hooks/exhaustive-deps

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

  // Show the bot: take control and record what you do in the browser.
  const showTheBot = async () => {
    if (!botId) return;
    setLoadingAction('record');
    setError('');
    try {
      await startComputerRecording(botId);
      setRecording({ steps: 0 });
      setMaximized(true);
      await refresh();
    } catch (err) {
      setError(err.message || 'Could not start recording.');
    } finally {
      setLoadingAction('');
    }
  };

  // Done showing: the recording is saved, the bot gets the desktop back, and
  // the user says what it was before it goes to the bot as a message.
  const finishShowing = async () => {
    if (!botId) return;
    setLoadingAction('record');
    setError('');
    try {
      const result = await stopComputerRecording(botId);
      setRecording(null);
      await setComputerControl(botId, 'bot');
      await refresh();
      setMaximized(false);
      setNote('');
      setFinished(result.recording || null);
    } catch (err) {
      setError(err.message || 'Could not finish the recording.');
    } finally {
      setLoadingAction('');
    }
  };

  const sendDemonstration = () => {
    if (!finished) return;
    const text = note.trim() || 'I showed you how to do something on your computer.';
    setFinished(null);
    if (onDemonstrated) onDemonstrated({ recordingId: finished.id, text });
  };

  if (!bot) {
    return (
      <div className="flex-1 flex items-center justify-center bg-zinc-950/80 p-6 text-sm text-zinc-400">
        Select a bot to view its computer.
      </div>
    );
  }

  const actionBusy = Boolean(loadingAction);
  const vncUrl = botId ? computerVncUrl(botId) : '';
  const viewerKey = `${computer?.computer_id}-${computer?.generation}-${state}-${maximized ? 'max' : 'inline'}`;

  const controlButton = (large = false) =>
    hasVnc ? (
      recording ? (
        <button
          onClick={finishShowing}
          disabled={actionBusy}
          className={`flex items-center gap-1.5 rounded-xl font-semibold border bg-red-600 text-white border-red-500 hover:bg-red-500 disabled:opacity-50 transition ${large ? 'px-4 py-2 text-sm' : 'px-3 py-1.5 text-xs'}`}
          title="Stop recording, hand the desktop back and send what you showed to the bot"
        >
          <FiVideo /> {loadingAction === 'record' ? 'Finishing…' : `I'm done showing${recording.steps ? ` (${recording.steps} step${recording.steps === 1 ? '' : 's'})` : ''}`}
        </button>
      ) : userHasControl ? (
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

  // Only offered while the bot has the desktop: recording starts by taking control.
  const showButton = (large = false) =>
    hasVnc && !userHasControl && !recording ? (
      <button
        onClick={showTheBot}
        disabled={actionBusy}
        className={`flex items-center gap-1.5 rounded-xl font-semibold border bg-red-500/15 text-red-200 border-red-500/30 hover:bg-red-500/25 disabled:opacity-50 transition ${large ? 'px-4 py-2 text-sm' : 'px-3 py-1.5 text-xs'}`}
        title="Take control and record what you do in the browser; the bot learns the steps"
      >
        <FiVideo /> {loadingAction === 'record' ? 'Starting…' : 'Show the bot'}
      </button>
    ) : null;

  // `enlarge` is the click handler while the bot has control: inline, a click
  // on the desktop opens the large view; in the large view there is none.
  const liveDesktop = (enlarge) => (
    <div
      className={`w-full h-full rounded-xl border bg-black relative overflow-hidden shadow-2xl ${
        userHasControl ? 'border-purple-500/60 ring-1 ring-purple-500/40' : 'border-zinc-800'
      }`}
    >
      <VncViewer key={viewerKey} url={vncUrl} viewOnly={!userHasControl} onStatus={setVncStatus} onClick={enlarge} />
      <span className={`absolute top-3 right-3 rounded px-2 py-1 text-[10px] font-mono pointer-events-none flex items-center gap-1.5 ${recording ? 'bg-red-600/90 text-white' : 'bg-black/70 text-zinc-300'}`}>
        {recording && <span className="w-2 h-2 rounded-full bg-white animate-pulse" />}
        {recording ? `recording · ${recording.steps} step${recording.steps === 1 ? '' : 's'}` : userHasControl ? 'you have control' : 'watching · bot has control'}
      </span>
    </div>
  );

  const display = hasVnc ? (
    liveDesktop(() => setMaximized(true))
  ) : screen?.available ? (
    screen.data ? (
      <div className="w-full h-full rounded-xl border border-zinc-800 bg-black flex items-center justify-center relative overflow-hidden shadow-2xl">
        <img src={`data:image/${screen.format || 'jpeg'};base64,${screen.data}`} alt={`Computer frame ${screen.frame_id || ''}`} className="max-w-full max-h-full object-contain" />
        <span className="absolute top-3 right-3 rounded bg-black/70 px-2 py-1 text-[10px] font-mono text-zinc-300">{screen.frame_id}</span>
      </div>
    ) : (
      <div className="w-full h-full rounded-xl border border-zinc-800/80 bg-zinc-950 p-4 flex flex-col justify-between font-mono text-xs">
        <div className="flex items-center justify-between text-zinc-400 border-b border-zinc-800/60 pb-2">
          <span className="text-cyan-400">Provider frame metadata</span>
          <span>{screen.frame_id}</span>
        </div>
        <div className="space-y-2 my-auto">
          <p className="text-emerald-400">✓ Computer is {screen.state}</p>
          <p className="text-zinc-400">Provider: {screen.provider}</p>
          <p className="text-amber-400">{screen.message}</p>
        </div>
      </div>
    )
  ) : (
    <div className="w-full h-full rounded-xl border border-dashed border-zinc-800 flex flex-col items-center justify-center text-center p-8">
      <FiMonitor className="text-4xl text-zinc-700 mb-4" />
      <p className="text-sm font-semibold text-zinc-300">{state === 'starting' || state === 'resetting' ? `The computer is ${prettyState(state)}…` : 'The computer is off'}</p>
      <p className="text-xs text-zinc-500 mt-2 max-w-md">
        Press Start to boot it, or let the bot start it when it needs it. Ubuntu with Chromium, a terminal, Node and Python;
        <span className="font-mono text-zinc-400"> /workspace</span> and the home directory are kept between starts,
        <span className="font-mono text-zinc-400"> /shared</span> is the shared folder. It has internet access: do not sign in to anything you would not type into a shared machine.
      </p>
    </div>
  );

  const activity = (
    <div className="w-full h-full rounded-xl border border-zinc-800 bg-zinc-950 p-5 font-mono text-xs space-y-2 overflow-auto">
      <p className="text-zinc-500">[provider] {computer?.provider || 'fake'} adapter</p>
      <p className="text-zinc-400">[state] {prettyState(state)}</p>
      <p className="text-zinc-400">[health] {computer?.health || 'unknown'}</p>
      <p className="text-zinc-400">[control] {computer?.controlled_by || 'bot'}</p>
      <p className="text-zinc-400">[screen] {width}x{height}</p>
      <p className="text-zinc-400">[generation] {computer?.generation ?? 0}</p>
      <p className="text-zinc-400">[operation] {computer?.last_operation || 'none'}</p>
      <p className="text-zinc-400">[browser] {computer?.url || 'no page'}</p>
      <p className="text-zinc-400">[packages] {computer?.packages?.length ? computer.packages.join(', ') : 'none'}</p>
      <p className="text-zinc-400">[capabilities] {capabilities.length ? capabilities.join(', ') : 'none yet'}</p>
      <p className="text-zinc-500">[id] {computer?.computer_id || 'not created'}</p>
      <p className="text-cyan-400">[note] All lifecycle calls are recorded by the action gateway.</p>
    </div>
  );

  return (
    <div className="flex-1 flex flex-col h-screen overflow-hidden bg-zinc-950/80 p-4 gap-3">
      {/* Control bar */}
      <div className="flex items-center justify-between gap-3 glass-panel px-4 py-3 rounded-2xl border border-zinc-800">
        <div className="flex items-center gap-3 min-w-0">
          <button
            onClick={onBackToChat}
            className="w-9 h-9 rounded-xl border border-zinc-700 text-zinc-400 hover:text-white hover:border-zinc-500 flex items-center justify-center transition flex-shrink-0"
            aria-label="Back to chat"
          >
            <FiArrowLeft />
          </button>
          <div className="w-9 h-9 rounded-xl bg-purple-600/20 text-purple-400 border border-purple-500/30 flex items-center justify-center text-lg flex-shrink-0">
            <FiMonitor />
          </div>
          <div className="min-w-0">
            <h2 className="text-sm font-bold text-zinc-100 flex items-center gap-2 flex-wrap">
              <span className="truncate">{bot.name || 'Agent'}&apos;s computer</span>
              <span className={`text-[10px] px-2 py-0.5 rounded font-mono border flex items-center gap-1 capitalize ${stateClasses(state)}`}>
                <FiActivity className={state === 'running' ? 'animate-pulse' : ''} /> {prettyState(state)}
              </span>
              {isActive && (
                <span className={`text-[10px] px-2 py-0.5 rounded font-mono border flex items-center gap-1 ${userHasControl ? 'bg-purple-500/20 text-purple-200 border-purple-500/30' : 'bg-zinc-800 text-zinc-300 border-zinc-700'}`}>
                  {userHasControl ? <FiUserCheck /> : <FiEye />} {userHasControl ? 'you have control' : 'bot has control'}
                </span>
              )}
              <span className="text-[10px] font-mono text-zinc-500">{width}x{height}{hasVnc ? ` · ${vncStatus}` : ''}</span>
            </h2>
          </div>
        </div>

        <div className="flex items-center gap-2 shrink-0">
          {showButton()}
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
          <button
            onClick={() => {
              const ok = window.confirm(
                `Reset ${bot.name || 'this bot'}'s computer?\n\nThis wipes the home directory (SSH keys, virtualenvs, settings, background job logs) and forgets the packages it installed. Only /workspace and the shared folder are kept.\n\nTo restart it, or to move it onto a new computer image, use Stop and then Start instead: that keeps everything.`
              );
              if (ok) runAction('reset', resetComputer);
            }}
            disabled={actionBusy}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-semibold border bg-zinc-800 text-zinc-300 border-zinc-700 hover:bg-red-500/20 hover:text-red-200 hover:border-red-500/40 disabled:opacity-50 transition"
            title="Start over: wipes the home directory and the installed packages, keeps /workspace. Stop then Start keeps everything."
          >
            <FiRefreshCw className={loadingAction === 'reset' ? 'animate-spin' : ''} /> Reset
          </button>
          <button onClick={() => runAction('stop', stopComputer)} disabled={actionBusy || state === 'stopped'} title="Shut the computer down; Start brings it back on the current image with its files, home directory and packages" className="flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-semibold border bg-zinc-800 text-zinc-300 border-zinc-700 hover:bg-zinc-700 disabled:opacity-50 transition">
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
      <div className="flex-1 flex flex-col min-h-0 glass-panel rounded-2xl border border-zinc-800 overflow-hidden">
        <div className="flex items-center gap-1 px-3 pt-3 pb-2">
          {TABS.map(([tab, label]) => (
            <button
              key={tab}
              onClick={() => setActiveTab(tab)}
              className={`px-3 py-1.5 rounded-lg text-[11px] font-semibold transition ${activeTab === tab ? 'bg-[#27272a] text-white' : 'text-zinc-500 hover:text-zinc-300'}`}
            >
              {label}
            </button>
          ))}
          <span className="flex-1" />
          {activeTab === 'display' && hasVnc && (
            <button onClick={() => setMaximized(true)} className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-[11px] text-zinc-400 hover:text-white hover:bg-zinc-800 transition" title="Fill the window with the desktop">
              <FiMaximize2 /> Maximize
            </button>
          )}
        </div>
        <div className="flex-1 bg-zinc-950 relative p-3 pt-0 min-h-0">
          {activeTab === 'display' && (loading && !computer ? <div className="w-full h-full flex items-center justify-center text-xs text-zinc-500">Loading…</div> : maximized && hasVnc ? (
            <div className="w-full h-full rounded-xl border border-zinc-800 flex items-center justify-center text-xs text-zinc-500">Showing in the large view.</div>
          ) : display)}
          {activeTab === 'files' && <ComputerFiles bot={bot} initialPath={initial?.path ? { path: initial.path, nonce: initial.nonce } : null} />}
          {activeTab === 'activity' && activity}
        </div>
      </div>

      <div className="flex items-center gap-2 text-[10px] text-zinc-500 px-1">
        <FiHardDrive /> Sandbox has internet access; do not sign in to anything you would not type into a shared machine.
      </div>

      {/* A finished demonstration: say what it was, then it goes to the bot with the steps. */}
      {finished && (
        <div className="fixed inset-0 z-[60] bg-black/80 flex items-center justify-center p-4">
          <div className="w-full max-w-lg glass-panel rounded-2xl border border-zinc-700 p-5 flex flex-col gap-3">
            <div className="flex items-center gap-2 text-sm font-semibold text-zinc-100">
              <FiVideo className="text-red-300" /> Tell {bot.name || 'the bot'} what you showed
            </div>
            <p className="text-xs text-zinc-400">
              {finished.steps} step{finished.steps === 1 ? '' : 's'} and {finished.keyframes} screen{finished.keyframes === 1 ? '' : 's'} were recorded. They go to the bot with your message, and it writes them up as a skill of its own so it can do it next time.
            </p>
            <textarea
              value={note}
              onChange={(e) => setNote(e.target.value)}
              rows={2}
              autoFocus
              placeholder="What it was, e.g. 'This is how to file a PTO request in Workday'"
              className="w-full bg-[#0f0f12] border border-[#2a2a2f] rounded-xl px-3 py-2 text-xs text-zinc-100 placeholder-zinc-600 focus:outline-none focus:border-zinc-500 resize-none"
            />
            {finished.text && (
              <pre className="max-h-40 overflow-auto rounded-xl border border-zinc-800 bg-zinc-950 p-3 text-[10px] font-mono text-zinc-400 whitespace-pre-wrap">{finished.text}</pre>
            )}
            <div className="flex items-center justify-end gap-2">
              <button onClick={() => setFinished(null)} className="px-3 py-1.5 rounded-xl text-xs font-semibold border bg-zinc-800 text-zinc-300 border-zinc-700 hover:bg-zinc-700 transition">
                Discard
              </button>
              <button onClick={sendDemonstration} className="flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-semibold bg-purple-600 text-white hover:bg-purple-500 transition">
                <FiSend /> Send to the bot
              </button>
            </div>
          </div>
        </div>
      )}

      {/* The large view: the desktop at up to 95% of the window, aspect kept, controls underneath. */}
      {maximized && hasVnc && (
        <div className="fixed inset-0 z-50 bg-black/95 flex flex-col items-center justify-center gap-3 p-4" onMouseDown={(event) => { if (event.target === event.currentTarget) setMaximized(false); }}>
          <div style={{ aspectRatio: `${width} / ${height}`, width: `min(95vw, calc((95vh - 72px) * ${width / height}))` }} className="max-w-[95vw]">
            {liveDesktop(null)}
          </div>
          <div className="flex items-center gap-3">
            {takeover && !userHasControl && (
              <span className="text-xs text-purple-200 max-w-md truncate" title={takeover.reason}>{bot.name || 'The bot'} asked: {takeover.reason}</span>
            )}
            {showButton(true)}
            {controlButton(true)}
            <button onClick={() => setMaximized(false)} className="flex items-center gap-1.5 px-4 py-2 rounded-xl text-sm font-semibold border bg-zinc-800 text-zinc-200 border-zinc-700 hover:bg-zinc-700 transition" title="Escape">
              <FiX /> Close
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
