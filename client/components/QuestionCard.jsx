'use client';

import React, { useState } from 'react';
import { FiHelpCircle, FiSend, FiCheck } from 'react-icons/fi';

// A question the bot asked mid-reply. Options are one click each; "Other"
// (when the bot allows it, always when there are no options) takes typed
// text. The turn waits until the answer goes back.
export default function QuestionCard({ question, onAnswer }) {
  const options = Array.isArray(question.options) ? question.options : [];
  const allowOther = question.allowOther !== false || options.length === 0;
  const [typing, setTyping] = useState(options.length === 0);
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [answered, setAnswered] = useState(null); // the answer text once sent
  const [error, setError] = useState('');

  const send = async (answer, optionIndex) => {
    setBusy(true);
    setError('');
    try {
      await onAnswer(question.requestId, answer, optionIndex);
      setAnswered(answer);
    } catch (err) {
      setError(err?.message || 'Could not send the answer.');
    } finally {
      setBusy(false);
    }
  };

  const submitTyped = (event) => {
    event.preventDefault();
    if (!text.trim() || busy) return;
    send(text.trim(), null);
  };

  return (
    <div className="my-3 p-4 rounded-2xl glass-panel border border-sky-500/30 bg-slate-900/80 shadow-xl max-w-xl">
      <div className="flex items-center gap-2 text-sky-300 border-b border-slate-800/80 pb-2 mb-3">
        <FiHelpCircle className={`text-lg ${answered ? '' : 'animate-pulse'}`} />
        <span className="text-xs font-bold uppercase tracking-wider">The bot has a question</span>
      </div>

      <p className="text-xs text-slate-100 whitespace-pre-wrap break-words [overflow-wrap:anywhere]">{question.question || question.summary}</p>

      {answered ? (
        <div className="flex items-center gap-2 mt-3 pt-2 border-t border-slate-800/60 text-xs text-emerald-300">
          <FiCheck /> <span className="truncate">You answered: {answered}</span>
        </div>
      ) : (
        <div className="mt-3 pt-2 border-t border-slate-800/60 space-y-2">
          {options.length > 0 && (
            <div className="flex flex-wrap gap-2">
              {options.map((option, index) => (
                <button
                  key={`${index}-${option}`}
                  type="button"
                  disabled={busy}
                  onClick={() => send(option, index)}
                  className="px-3 py-1.5 rounded-xl bg-slate-800 hover:bg-sky-500/20 text-slate-100 hover:text-white border border-slate-700 hover:border-sky-500/40 text-xs font-medium transition disabled:opacity-50 text-left"
                >
                  {option}
                </button>
              ))}
              {allowOther && !typing && (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => setTyping(true)}
                  className="px-3 py-1.5 rounded-xl text-slate-400 hover:text-white border border-dashed border-slate-700 text-xs transition disabled:opacity-50"
                >
                  Other…
                </button>
              )}
            </div>
          )}
          {allowOther && typing && (
            <form onSubmit={submitTyped} className="flex items-end gap-2">
              <textarea
                autoFocus
                rows={2}
                value={text}
                onChange={(e) => setText(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey) submitTyped(e);
                }}
                placeholder="Type your answer (Enter sends, Shift+Enter adds a line)"
                className="flex-1 bg-[#222226] border border-[#2e2e34] rounded-xl px-3 py-2 text-xs text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-sky-400 resize-y"
              />
              <button
                type="submit"
                disabled={busy || !text.trim()}
                className="flex items-center gap-1.5 px-3 py-2 rounded-xl bg-sky-600 hover:bg-sky-500 text-white text-xs font-semibold transition disabled:opacity-50"
              >
                <FiSend className="text-sm" /> Send
              </button>
            </form>
          )}
          {error && <p className="text-[10px] text-rose-400">{error}</p>}
        </div>
      )}
    </div>
  );
}
