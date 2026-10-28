'use client';

import React, { useEffect, useRef, useState } from 'react';
import { FiCheck, FiCpu } from 'react-icons/fi';

export const THINKING_LEVELS = [
  { value: 'auto', label: 'Auto', help: 'Chosen per message: light for quick asks, deep for real work' },
  { value: 'none', label: 'Off', help: 'No thinking: fastest' },
  { value: 'low', label: 'Low' },
  { value: 'medium', label: 'Medium' },
  { value: 'high', label: 'High' },
  { value: 'xhigh', label: 'Extra high' },
];

export function thinkingLabel(value) {
  return THINKING_LEVELS.find((l) => l.value === value)?.label || value;
}

// How hard the model thinks in this conversation. "Default" follows the bot's
// setting, which follows App Settings. Changing it makes the next reply read
// the conversation afresh (the model server caches each level separately).
export default function ThinkingPicker({ conversation, bot, onChange }) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    const close = (event) => {
      if (rootRef.current && !rootRef.current.contains(event.target)) setOpen(false);
    };
    document.addEventListener('mousedown', close);
    return () => document.removeEventListener('mousedown', close);
  }, [open]);

  const own = conversation?.thinking || '';
  const effective = own || bot?.thinking || '';
  const last = conversation?.thinking_last || '';
  const label = effective === 'auto'
    ? `Auto${last ? ` · ${thinkingLabel(last).toLowerCase()}` : ''}`
    : effective
      ? thinkingLabel(effective)
      : `Default${last ? ` · ${thinkingLabel(last).toLowerCase()}` : ''}`;
  const botLabel = bot?.thinking ? thinkingLabel(bot.thinking) : 'App Settings';

  const pick = (value) => {
    setOpen(false);
    if (value !== own) onChange(value);
  };

  return (
    <div className="relative" ref={rootRef}>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex items-center gap-1.5 px-2 py-1 rounded-lg text-[11px] text-zinc-400 hover:text-white hover:bg-[#1f1f23] transition"
        title="How hard the model thinks in this conversation (/think)"
      >
        <FiCpu className="text-xs" />
        <span>{label}</span>
      </button>
      {open && (
        <div className="absolute right-0 top-full mt-1 z-50 w-64 rounded-xl border border-[#2b2b32] bg-[#141416] shadow-2xl py-1 text-xs">
          <div className="px-3 py-1.5 text-[10px] uppercase tracking-wider text-zinc-500">Thinking in this conversation</div>
          {[{ value: '', label: `Default (${botLabel})`, help: "Follow the bot's setting" }, ...THINKING_LEVELS].map((level) => (
            <button
              key={level.value || 'default'}
              type="button"
              onClick={() => pick(level.value)}
              className="w-full flex items-start gap-2 px-3 py-1.5 text-left hover:bg-[#1f1f25] transition"
            >
              <span className="w-3 mt-0.5 text-zinc-300">{own === level.value ? <FiCheck className="text-[11px]" /> : null}</span>
              <span className="flex-1">
                <span className="text-zinc-200">{level.label}</span>
                {level.help && <span className="block text-[10px] text-zinc-500">{level.help}</span>}
              </span>
            </button>
          ))}
          <p className="px-3 pt-1.5 pb-1 text-[10px] leading-snug text-zinc-500 border-t border-[#232328] mt-1">
            A change makes the next reply re-read the conversation once, so it may start a little slower.
          </p>
        </div>
      )}
    </div>
  );
}
