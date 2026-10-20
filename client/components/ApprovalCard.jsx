'use client';

import React, { useState } from 'react';
import { FiShield, FiTerminal, FiCheck, FiX, FiAlertTriangle } from 'react-icons/fi';

// One line about what the auto-approval gate concluded, shown on the card
// while it is pending. `verdict` is the gate.scored event's payload.
function GateLine({ verdict }) {
  if (!verdict) return null;
  const score = typeof verdict.maxScore === 'number' ? verdict.maxScore.toFixed(2) : null;
  const tone =
    verdict.outcome === 'auto_approved'
      ? 'text-emerald-300'
      : verdict.outcome === 'error'
        ? 'text-zinc-500'
        : 'text-amber-300';
  const text =
    verdict.outcome === 'auto_approved'
      ? `Auto-approved: every risk score below ${verdict.threshold} (max ${score}).`
      : verdict.outcome === 'error'
        ? `Auto-approval unavailable (${verdict.reason}). Waiting for you.`
        : verdict.mode === 'shadow'
          ? `Shadow mode: scored ${score} at most, ${verdict.maxScore < verdict.threshold ? 'would have auto-approved' : 'would have asked'}. Waiting for you.`
          : `Not auto-approved: ${verdict.reason}. Waiting for you.`;
  return (
    <p className={`mt-2 text-[10px] leading-relaxed ${tone}`} title={Object.entries(verdict.scores || {}).map(([k, v]) => `${k}: ${v}`).join('\n')}>
      {text}
    </p>
  );
}

// The arguments worth showing in full: the command for a terminal call,
// otherwise every argument except the ids the server adds for itself.
const HIDDEN_ARGUMENTS = new Set(['bot_id', 'computer_id', 'thread_id']);

function ArgumentView({ approval }) {
  const args = approval.arguments && typeof approval.arguments === 'object' ? approval.arguments : {};
  if (typeof args.command === 'string') {
    return (
      <pre className="mt-2 max-h-64 overflow-auto rounded-lg bg-black/40 border border-slate-800 px-3 py-2 text-[11px] leading-relaxed font-mono text-slate-100 whitespace-pre-wrap break-all">
        {args.command}
      </pre>
    );
  }
  const rows = Object.entries(args).filter(([key, value]) => !HIDDEN_ARGUMENTS.has(key) && value !== undefined && value !== null && value !== '');
  if (!rows.length) return null;
  return (
    <dl className="mt-2 max-h-64 overflow-auto rounded-lg bg-black/40 border border-slate-800 px-3 py-2 text-[11px] font-mono space-y-1">
      {rows.map(([key, value]) => (
        <div key={key} className="flex gap-2">
          <dt className="text-slate-500 flex-shrink-0">{key}</dt>
          <dd className="text-slate-100 whitespace-pre-wrap break-all min-w-0">{typeof value === 'string' ? value : JSON.stringify(value, null, 1)}</dd>
        </div>
      ))}
    </dl>
  );
}

export default function ApprovalCard({ approval, onRespond, onAlwaysAllow }) {
  const [status, setStatus] = useState('pending'); // pending, allowed, denied
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState('');
  // The command shapes the user ticks to remember for this bot.
  const shapes = Array.isArray(approval.command_shapes) ? approval.command_shapes : [];
  const [remember, setRemember] = useState([]);
  const canRemember = Boolean(onAlwaysAllow) && shapes.length > 0;

  const handleAction = async (action) => {
    setIsSubmitting(true);
    setError('');
    try {
      if (action === 'allow' && remember.length && onAlwaysAllow) {
        // Saved first, so a rule that fails to save never lets the command through on its own.
        await onAlwaysAllow(remember);
      }
      if (onRespond) {
        await onRespond(approval.requestId, action);
      }
      setStatus(action === 'allow' ? 'allowed' : 'denied');
    } catch (err) {
      setError(err?.message || 'Could not submit this decision.');
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="my-3 p-4 rounded-2xl glass-panel border border-amber-500/30 bg-slate-900/80 shadow-xl max-w-xl">
      <div className="flex items-center justify-between border-b border-slate-800/80 pb-2 mb-3">
        <div className="flex items-center gap-2 text-amber-400">
          <FiShield className="text-lg animate-pulse" />
          <span className="text-xs font-bold uppercase tracking-wider">Permission Broker Request</span>
        </div>
        <span className="text-[10px] font-mono bg-amber-500/10 text-amber-300 px-2 py-0.5 rounded border border-amber-500/20">
          {approval.tool || 'terminal.execute'}
        </span>
      </div>

      <div className="flex items-start gap-3 my-2">
        <div className="w-8 h-8 rounded-lg bg-slate-800 flex items-center justify-center text-amber-400 border border-slate-700 flex-shrink-0">
          <FiTerminal className="text-base" />
        </div>
        <div className="min-w-0 flex-1">
          <p className="text-xs font-medium text-slate-200">{approval.summary}</p>
          <ArgumentView approval={approval} />
          {approval.command_parse_error && (
            <p className="mt-1.5 text-[10px] text-zinc-500">Cannot be remembered as a rule: the command has {approval.command_parse_error}.</p>
          )}
          <GateLine verdict={approval.gate} />
        </div>
      </div>

      {status === 'pending' && canRemember && (
        <div className="mt-3 pt-2 border-t border-slate-800/60">
          <p className="text-[10px] uppercase tracking-wide text-zinc-500 mb-1.5">Always allow for this bot</p>
          <div className="flex flex-wrap gap-1.5">
            {shapes.map((shape) => {
              const on = remember.includes(shape);
              return (
                <button
                  key={shape}
                  type="button"
                  onClick={() => setRemember((current) => (on ? current.filter((s) => s !== shape) : [...current, shape]))}
                  className={`px-2 py-1 rounded-lg border text-[11px] font-mono transition ${
                    on ? 'border-emerald-500/50 bg-emerald-500/15 text-emerald-200' : 'border-slate-700 bg-slate-800/60 text-slate-300 hover:border-slate-500'
                  }`}
                  title={on ? `Will run without asking from now on: ${shape} …` : `Remember ${shape} … as allowed for this bot`}
                >
                  {on ? '✓ ' : ''}{shape} …
                </button>
              );
            })}
          </div>
          <p className="mt-1.5 text-[10px] text-zinc-500">
            A ticked command runs without asking whenever the whole script is made of allowed commands. Each is a prefix: “git status”
            covers any git status, “cat” covers any cat. Edit the list in the bot editor under Tools.
          </p>
        </div>
      )}

      {status === 'pending' ? (
        <div className="flex items-center justify-end gap-2 mt-4 pt-2 border-t border-slate-800/60">
          <button
            disabled={isSubmitting}
            onClick={() => handleAction('deny')}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-xl bg-slate-800 hover:bg-rose-500/20 text-rose-400 hover:text-rose-300 border border-slate-700 hover:border-rose-500/40 text-xs font-semibold transition disabled:opacity-50"
          >
            <FiX className="text-sm" />
            <span>Deny</span>
          </button>
          <button
            disabled={isSubmitting}
            onClick={() => handleAction('allow')}
            className="flex items-center gap-1.5 px-4 py-1.5 rounded-xl bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-semibold shadow-lg shadow-emerald-600/30 transition disabled:opacity-50"
          >
            <FiCheck className="text-sm" />
            <span>{remember.length ? `Allow and remember ${remember.length}` : 'Allow'}</span>
          </button>
          {error && <span className="text-[10px] text-rose-400">{error}</span>}
        </div>
      ) : (
        <div className="flex items-center justify-between mt-3 pt-2 border-t border-slate-800/60 text-xs">
          <span className="text-slate-400">Status:</span>
          <span
            className={`font-semibold flex items-center gap-1 px-2 py-0.5 rounded ${
              status === 'allowed'
                ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30'
                : 'bg-rose-500/20 text-rose-400 border border-rose-500/30'
            }`}
          >
            {status === 'allowed' ? <FiCheck /> : <FiX />}
            {status === 'allowed' ? 'Approved' : 'Denied'}
          </span>
        </div>
      )}
    </div>
  );
}
