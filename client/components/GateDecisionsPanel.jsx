'use client';

import React, { useEffect, useMemo, useState } from 'react';
import { FiAlertCircle, FiRefreshCw, FiZap } from 'react-icons/fi';
import { fetchGateDecisions } from '../lib/api';

function formatTimestamp(value) {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

const OUTCOME_TONE = {
  auto_approved: 'text-emerald-300 bg-emerald-500/10 border-emerald-500/20',
  ask: 'text-amber-300 bg-amber-500/10 border-amber-500/20',
  error: 'text-zinc-400 bg-zinc-500/10 border-zinc-500/20',
  skipped: 'text-zinc-400 bg-zinc-500/10 border-zinc-500/20',
};

// Whether the gate and the user agreed. Only meaningful once the user has
// answered a card the gate also scored.
function agreement(row) {
  if (!row.final_decision || row.decided_by !== 'user' || row.max_score == null) return null;
  const wouldApprove = row.max_score < row.threshold;
  if (row.final_decision === 'allow') return wouldApprove ? 'agree' : 'stricter';
  if (row.final_decision === 'deny') return wouldApprove ? 'miss' : 'agree';
  return null;
}

const AGREEMENT_LABEL = {
  agree: ['agreed with you', 'text-emerald-400'],
  stricter: ['would have asked anyway', 'text-zinc-400'],
  miss: ['would have approved what you denied', 'text-rose-300'],
};

// What the auto-approval gate was shown, what it concluded, and what was
// finally decided. The "miss" rows are the ones worth reading: actions the
// user denied that the model scored as low-risk.
export default function GateDecisionsPanel({ tabs = null }) {
  const [data, setData] = useState({ decisions: [], questions: {} });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [expanded, setExpanded] = useState(null);

  const load = async () => {
    setLoading(true);
    setError('');
    try {
      setData(await fetchGateDecisions(500));
    } catch (err) {
      setError(err.message || 'Could not load gate decisions');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const rows = useMemo(() => [...(data.decisions || [])].reverse(), [data]);
  const summary = useMemo(() => {
    const scored = rows.filter((r) => r.max_score != null);
    const auto = rows.filter((r) => r.outcome === 'auto_approved').length;
    const misses = rows.filter((r) => agreement(r) === 'miss').length;
    const agreed = rows.filter((r) => agreement(r) === 'agree').length;
    const judged = rows.filter((r) => agreement(r) !== null).length;
    return { scored: scored.length, auto, misses, agreed, judged };
  }, [rows]);

  return (
    <div className="flex-1 flex flex-col h-screen overflow-hidden bg-[#09090b] font-sans text-zinc-100">
      <div className="px-6 py-4 border-b border-[#18181c] flex items-center justify-between flex-shrink-0">
        <div>
          <div className="flex items-center gap-2">
            <FiZap className="text-emerald-400" />
            <h2 className="text-sm font-bold tracking-wide">Auto-approval decisions</h2>
          </div>
          <p className="text-[11px] text-zinc-500 mt-0.5">
            {summary.scored} scored · {summary.auto} auto-approved
            {summary.judged > 0 && ` · of ${summary.judged} you answered, it agreed on ${summary.agreed}`}
            {summary.misses > 0 && (
              <span className="text-rose-300"> · {summary.misses} it would have approved that you denied</span>
            )}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {tabs}
          <button type="button" onClick={load} className="p-2 rounded-lg text-zinc-400 hover:text-white hover:bg-[#1e1e22] transition" title="Refresh">
            <FiRefreshCw className={loading ? 'animate-spin' : ''} />
          </button>
        </div>
      </div>

      {error && (
        <div className="mx-6 mt-4 rounded-xl border border-rose-500/20 bg-rose-500/[0.07] px-4 py-3 text-xs text-rose-300 flex items-center gap-2">
          <FiAlertCircle /> {error}
        </div>
      )}

      <div className="flex-1 overflow-y-auto p-6">
        {loading && rows.length === 0 ? (
          <div className="py-16 text-center text-sm text-zinc-600">Loading…</div>
        ) : rows.length === 0 ? (
          <div className="rounded-2xl border border-dashed border-[#27272a] py-16 text-center">
            <FiZap className="mx-auto text-2xl text-zinc-700" />
            <p className="mt-3 text-sm text-zinc-500">Nothing scored yet.</p>
            <p className="mt-1 text-xs text-zinc-600">Turn Auto-approval to Shadow or On in App Settings and approvals will be scored here.</p>
          </div>
        ) : (
          <div className="rounded-2xl border border-[#1e1e22] overflow-hidden">
            {rows.map((row, index) => {
              const verdict = agreement(row);
              const open = expanded === row.request_id;
              return (
                <div key={row.request_id} className={`${index > 0 ? 'border-t border-[#1a1a1e]' : ''} bg-[#0d0d10]`}>
                  <button
                    type="button"
                    onClick={() => setExpanded(open ? null : row.request_id)}
                    className="w-full text-left px-4 py-3.5 hover:bg-[#111115] transition"
                  >
                    <div className="flex items-start justify-between gap-4">
                      <div className="min-w-0">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold ${OUTCOME_TONE[row.outcome] || OUTCOME_TONE.skipped}`}>
                            {row.outcome}
                          </span>
                          <span className="text-[10px] text-zinc-500">{row.mode}</span>
                          <span className="text-xs text-zinc-300 truncate">{row.tool}</span>
                          {row.rule && (
                            <span className="rounded-full border border-sky-500/30 bg-sky-500/10 px-2 py-0.5 text-[10px] font-semibold text-sky-300" title="Decided by a rule, not the model">
                              rule: {row.rule}
                            </span>
                          )}
                          {row.max_score != null && (
                            <span className="font-mono text-[10px] text-zinc-400">max {Number(row.max_score).toFixed(2)} / {row.threshold}</span>
                          )}
                        </div>
                        <p className="mt-1 text-[11px] text-zinc-400 truncate">{row.preview}</p>
                        <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1 text-[10px] text-zinc-600">
                          {row.final_decision && (
                            <span>
                              final: {row.final_decision} by {row.decided_by}
                            </span>
                          )}
                          {verdict && <span className={AGREEMENT_LABEL[verdict][1]}>{AGREEMENT_LABEL[verdict][0]}</span>}
                          {row.reason && !verdict && <span>{row.reason}</span>}
                        </div>
                      </div>
                      <time className="flex-shrink-0 text-[10px] text-zinc-600">{formatTimestamp(row.created_at)}</time>
                    </div>
                  </button>
                  {open && (
                    <div className="px-4 pb-4 grid gap-3 md:grid-cols-2 text-[11px]">
                      <div>
                        <div className="text-[10px] uppercase tracking-wide text-zinc-500 mb-1">Scores</div>
                        {Object.entries(row.scores || {}).length === 0 ? (
                          <p className="text-zinc-500">{row.reason}</p>
                        ) : (
                          Object.entries(row.scores).map(([key, value]) => (
                            <div key={key} className="flex items-center gap-2 py-0.5" title={data.questions?.[key] || key}>
                              <span className="w-24 text-zinc-400 truncate">{key}</span>
                              <div className="flex-1 h-1.5 rounded bg-[#1c1c20] overflow-hidden">
                                <div className={`h-full ${value >= row.threshold ? 'bg-amber-400' : 'bg-emerald-500'}`} style={{ width: `${Math.min(100, value * 100)}%` }} />
                              </div>
                              <span className="w-10 text-right font-mono text-zinc-300">{Number(value).toFixed(2)}</span>
                            </div>
                          ))
                        )}
                        {row.latency_ms != null && <p className="mt-1 text-[10px] text-zinc-600">{row.latency_ms} ms</p>}
                      </div>
                      <div>
                        <div className="text-[10px] uppercase tracking-wide text-zinc-500 mb-1">What the model saw</div>
                        <pre className="rounded-lg bg-[#141416] border border-[#1f1f24] p-2.5 text-[10px] text-zinc-400 whitespace-pre-wrap break-words max-h-56 overflow-y-auto">
                          {JSON.stringify(row.state, null, 2)}
                        </pre>
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
