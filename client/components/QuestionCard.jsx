'use client';

import React, { useState } from 'react';
import { FiHelpCircle, FiSend, FiCheck } from 'react-icons/fi';

// An option is a label, or { label, description }.
const labelOf = (option) => (typeof option === 'string' ? option : option?.label || '');
const descriptionOf = (option) => (typeof option === 'string' ? '' : option?.description || '');

// The card's questions: `questions` when the bot asked several (or a newer server sent one that
// way), else the single question with its options.
function questionsOf(question) {
  if (Array.isArray(question.questions) && question.questions.length) return question.questions;
  return [{ question: question.question || question.summary, header: '', options: question.options || [], allowOther: question.allowOther }];
}

function OptionButton({ option, selected, disabled, onClick }) {
  const description = descriptionOf(option);
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className={`px-3 py-1.5 rounded-xl border text-xs font-medium transition disabled:opacity-50 text-left ${
        selected ? 'bg-sky-500/25 border-sky-400/70 text-white' : 'bg-slate-800 hover:bg-sky-500/20 text-slate-100 hover:text-white border-slate-700 hover:border-sky-500/40'
      }`}
    >
      {labelOf(option)}
      {description && <span className="block text-[10px] font-normal text-slate-400 mt-0.5">{description}</span>}
    </button>
  );
}

// A question the bot asked mid-reply. With one question, an option is one click; "Other" (when the
// bot allows it, always when there are no options) takes typed text. With several, each is answered
// (an option picked or text typed) and the card sends them together. The turn waits until it does.
export default function QuestionCard({ question, onAnswer }) {
  const questions = questionsOf(question);
  const several = questions.length > 1;
  // Per question: { index } for an option, { text } for typing.
  const [picks, setPicks] = useState(() => questions.map((q) => ((q.options || []).length ? {} : { typing: true, text: '' })));
  const [busy, setBusy] = useState(false);
  const [answered, setAnswered] = useState(null); // what was sent, for the card once done
  const [error, setError] = useState('');

  const pick = (n, change) => setPicks((all) => all.map((p, i) => (i === n ? { ...change } : p)));

  const send = async (answers) => {
    setBusy(true);
    setError('');
    try {
      if (several) await onAnswer(question.requestId, '', null, answers);
      else await onAnswer(question.requestId, answers[0].answer, answers[0].option_index);
      setAnswered(answers.map((a) => a.answer));
    } catch (err) {
      setError(err?.message || 'Could not send the answer.');
    } finally {
      setBusy(false);
    }
  };

  const answerOf = (q, p) => {
    if (Number.isInteger(p.index)) return { answer: labelOf(q.options[p.index]), option_index: p.index };
    if (p.typing && p.text?.trim()) return { answer: p.text.trim(), option_index: null };
    return null;
  };
  const answers = questions.map((q, n) => answerOf(q, picks[n]));
  const complete = answers.every(Boolean);

  return (
    <div className="my-3 p-4 rounded-2xl glass-panel border border-sky-500/30 bg-slate-900/80 shadow-xl max-w-xl">
      <div className="flex items-center gap-2 text-sky-300 border-b border-slate-800/80 pb-2 mb-3">
        <FiHelpCircle className={`text-lg ${answered ? '' : 'animate-pulse'}`} />
        <span className="text-xs font-bold uppercase tracking-wider">{several ? `The bot has ${questions.length} questions` : 'The bot has a question'}</span>
      </div>

      <div className="space-y-4">
        {questions.map((q, n) => {
          const options = q.options || [];
          const allowOther = q.allowOther !== false || options.length === 0;
          const p = picks[n] || {};
          return (
            <div key={n} className={several ? 'space-y-2' : ''}>
              {q.header && <span className="inline-block mb-1 text-[10px] font-mono uppercase tracking-wider text-sky-300/80 bg-sky-500/10 rounded px-1.5 py-0.5">{q.header}</span>}
              <p className="text-xs text-slate-100 whitespace-pre-wrap break-words [overflow-wrap:anywhere]">{q.question}</p>
              {answered ? (
                <div className="flex items-center gap-2 mt-2 text-xs text-emerald-300">
                  <FiCheck /> <span className="truncate">You answered: {answered[n]}</span>
                </div>
              ) : (
                <div className={`${several ? '' : 'mt-3 pt-2 border-t border-slate-800/60'} space-y-2`}>
                  {options.length > 0 && (
                    <div className="flex flex-wrap gap-2 mt-2">
                      {options.map((option, index) => (
                        <OptionButton
                          key={`${index}-${labelOf(option)}`}
                          option={option}
                          selected={several && p.index === index}
                          disabled={busy}
                          onClick={() => (several ? pick(n, { index }) : send([{ answer: labelOf(option), option_index: index }]))}
                        />
                      ))}
                      {allowOther && !p.typing && (
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => pick(n, { typing: true, text: '' })}
                          className="px-3 py-1.5 rounded-xl text-slate-400 hover:text-white border border-dashed border-slate-700 text-xs transition disabled:opacity-50"
                        >
                          Other…
                        </button>
                      )}
                    </div>
                  )}
                  {allowOther && p.typing && (
                    <form
                      onSubmit={(event) => {
                        event.preventDefault();
                        if (!several && p.text?.trim() && !busy) send([{ answer: p.text.trim(), option_index: null }]);
                      }}
                      className="flex items-end gap-2"
                    >
                      <textarea
                        autoFocus={n === 0 || options.length > 0}
                        rows={2}
                        value={p.text || ''}
                        onChange={(e) => pick(n, { typing: true, text: e.target.value })}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter' && !e.shiftKey && !several) {
                            e.preventDefault();
                            if (p.text?.trim() && !busy) send([{ answer: p.text.trim(), option_index: null }]);
                          }
                        }}
                        placeholder={several ? 'Type your answer' : 'Type your answer (Enter sends, Shift+Enter adds a line)'}
                        className="flex-1 bg-[#222226] border border-[#2e2e34] rounded-xl px-3 py-2 text-xs text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-sky-400 resize-y"
                      />
                      {!several && (
                        <button
                          type="submit"
                          disabled={busy || !p.text?.trim()}
                          className="flex items-center gap-1.5 px-3 py-2 rounded-xl bg-sky-600 hover:bg-sky-500 text-white text-xs font-semibold transition disabled:opacity-50"
                        >
                          <FiSend className="text-sm" /> Send
                        </button>
                      )}
                    </form>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {several && !answered && (
        <div className="flex items-center justify-end gap-3 mt-4 pt-3 border-t border-slate-800/60">
          {!complete && <span className="text-[10px] text-slate-500">{answers.filter(Boolean).length} of {questions.length} answered</span>}
          <button
            type="button"
            disabled={busy || !complete}
            onClick={() => send(answers)}
            className="flex items-center gap-1.5 px-3 py-2 rounded-xl bg-sky-600 hover:bg-sky-500 text-white text-xs font-semibold transition disabled:opacity-50"
          >
            <FiSend className="text-sm" /> Send answers
          </button>
        </div>
      )}
      {error && <p className="mt-2 text-[10px] text-rose-400">{error}</p>}
    </div>
  );
}
