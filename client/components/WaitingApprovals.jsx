'use client';

import React, { useState } from 'react';
import { FiCheck, FiChevronDown, FiChevronRight, FiX } from 'react-icons/fi';
import { respondApproval } from '../lib/api';

// Approval cards from teammates' conversations on shared bots that this
// person may answer (a bot's owner and bot admins, or everyone, as the owner
// chose). Those conversations are private to the teammate, so the cards come
// here instead.
export default function WaitingApprovals({ waiting }) {
  const [answered, setAnswered] = useState({});
  const [open, setOpen] = useState({});
  const [error, setError] = useState('');
  const shown = (waiting || []).filter((card) => !answered[card.request_id]);
  if (!shown.length) return null;

  const answer = async (card, action) => {
    setError('');
    setAnswered((prev) => ({ ...prev, [card.request_id]: action }));
    try {
      await respondApproval(card.request_id, action);
    } catch (err) {
      setError('That card was already answered or has expired.');
    }
  };

  return (
    <div className="rounded-xl border border-amber-500/30 bg-amber-500/5 p-2 space-y-2">
      <div className="px-1 text-[11px] font-medium text-amber-300">Waiting for your approval</div>
      {shown.map((card) => (
        <div key={card.request_id} className="rounded-lg bg-[#1c1c1f] border border-[#2c2c30] p-2 space-y-1.5">
          <div className="text-[10px] text-zinc-500 truncate">
            {card.asked_by_name} · {card.bot_name}
          </div>
          <button
            type="button"
            onClick={() => setOpen((prev) => ({ ...prev, [card.request_id]: !prev[card.request_id] }))}
            className="w-full text-left flex items-start gap-1 text-[11px] text-zinc-200"
          >
            {open[card.request_id] ? <FiChevronDown className="mt-0.5 flex-shrink-0" /> : <FiChevronRight className="mt-0.5 flex-shrink-0" />}
            <span className={open[card.request_id] ? 'break-words' : 'line-clamp-2 break-words'}>{card.summary || card.tool}</span>
          </button>
          {open[card.request_id] && card.arguments && Object.keys(card.arguments).length > 0 && (
            <pre className="max-h-40 overflow-auto rounded-md bg-black/30 p-1.5 text-[10px] text-zinc-400 whitespace-pre-wrap break-all select-text">
              {JSON.stringify(card.arguments, null, 2)}
            </pre>
          )}
          <div className="flex gap-1.5">
            <button
              type="button"
              onClick={() => answer(card, 'allow')}
              className="flex-1 flex items-center justify-center gap-1 rounded-md bg-zinc-100 text-black text-[11px] font-medium py-1 hover:bg-white transition"
            >
              <FiCheck /> Allow
            </button>
            <button
              type="button"
              onClick={() => answer(card, 'deny')}
              className="flex-1 flex items-center justify-center gap-1 rounded-md border border-[#2e2e34] text-[11px] text-zinc-300 py-1 hover:text-rose-300 hover:border-rose-500/40 transition"
            >
              <FiX /> Deny
            </button>
          </div>
        </div>
      ))}
      {error && <p className="px-1 text-[10px] text-rose-400">{error}</p>}
    </div>
  );
}
