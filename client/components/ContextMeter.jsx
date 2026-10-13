'use client';

import React from 'react';

function formatTokens(n) {
  if (!Number.isFinite(n)) return '?';
  if (n < 1000) return String(n);
  if (n < 100_000) return `${(n / 1000).toFixed(1)}k`;
  return `${Math.round(n / 1000)}k`;
}

// How full the model's context is, from the usage the provider reported on
// the latest turn. `usage` is {prompt_tokens, completion_tokens, context_window}.
export default function ContextMeter({ usage, contextWindow, model }) {
  if (!usage || !Number.isFinite(usage.prompt_tokens)) return null;
  const used = (usage.prompt_tokens || 0) + (usage.completion_tokens || 0);
  const window = usage.context_window || contextWindow || null;
  const ratio = window ? Math.min(1, used / window) : null;
  const tone = ratio === null ? 'bg-zinc-500' : ratio >= 0.9 ? 'bg-rose-500' : ratio >= 0.75 ? 'bg-amber-400' : 'bg-emerald-500';
  const title = window
    ? `Context: ${used.toLocaleString()} of ${window.toLocaleString()} tokens after the last turn (${Math.round(ratio * 100)}%)${model ? ` for ${model}` : ''}`
    : `Context: ${used.toLocaleString()} tokens after the last turn; the model's window is unknown`;

  return (
    <div className="flex items-center gap-2 text-[10px] text-zinc-500 select-none" title={title}>
      <div className="w-16 h-1.5 rounded-full bg-[#26262b] overflow-hidden">
        <div className={`h-full ${tone} transition-all`} style={{ width: `${ratio === null ? 100 : Math.max(2, ratio * 100)}%` }} />
      </div>
      <span className="font-mono tabular-nums">
        {formatTokens(used)}
        {window ? ` / ${formatTokens(window)}` : ''}
      </span>
    </div>
  );
}
