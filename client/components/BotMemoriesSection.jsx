'use client';

import React, { useCallback, useEffect, useState } from 'react';
import { FiCheck, FiEdit2, FiSearch, FiTrash2 } from 'react-icons/fi';
import { deleteBotMemory, fetchBotMemories, updateBotMemory } from '../lib/api';

function day(iso) {
  return String(iso || '').slice(0, 10);
}

// What a bot's automatic memory kept: the current facts, newest first, and on request the ones a newer
// fact replaced or that were forgotten. Search, correct and delete; saved as you go.
export default function BotMemoriesSection({ botId }) {
  const [data, setData] = useState(null);
  const [query, setQuery] = useState('');
  const [history, setHistory] = useState(false);
  const [editing, setEditing] = useState(null); // { id, subject, text }
  const [confirming, setConfirming] = useState('');
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    try {
      setData(await fetchBotMemories(botId, { q: query.trim(), history }));
      setError('');
    } catch (err) {
      setError(err?.message || 'Could not load the memories.');
    }
  }, [botId, query, history]);

  useEffect(() => {
    const timer = window.setTimeout(load, query ? 250 : 0);
    return () => window.clearTimeout(timer);
  }, [load, query]);

  const save = async () => {
    try {
      await updateBotMemory(botId, editing.id, { subject: editing.subject, text: editing.text });
      setEditing(null);
      load();
    } catch (err) {
      setError(err?.message || 'Could not save the memory.');
    }
  };

  const remove = async (id) => {
    try {
      await deleteBotMemory(botId, id);
      setConfirming('');
      load();
    } catch (err) {
      setError(err?.message || 'Could not delete the memory.');
    }
  };

  if (!data && !error) return <p className="text-[11px] text-zinc-500">Loading memories…</p>;
  const memories = data?.memories || [];
  const byId = Object.fromEntries(memories.map((m) => [m.id, m]));
  const inputClass = 'bg-[#1b1b1f] border border-[#2e2e34] rounded-lg px-2.5 py-1.5 text-[11px] text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-zinc-400';

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <div className="relative flex-1">
          <FiSearch className="absolute left-2.5 top-1/2 -translate-y-1/2 text-[11px] text-zinc-500" />
          <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder={`Search ${data?.current ?? 0} memories`} className={`${inputClass} w-full pl-7`} />
        </div>
        <label className="flex items-center gap-1.5 text-[11px] text-zinc-400 whitespace-nowrap">
          <input type="checkbox" checked={history} onChange={(e) => setHistory(e.target.checked)} className="accent-cyan-400" />
          Replaced and forgotten too
        </label>
      </div>
      {error && <p className="text-[11px] text-rose-400">{error}</p>}
      {!memories.length && (
        <p className="text-[11px] text-zinc-600">{query ? 'Nothing matches.' : 'Nothing kept yet. Facts appear here as the bot talks with you.'}</p>
      )}
      <div className="space-y-1 max-h-[28rem] overflow-y-auto pr-1">
        {memories.map((memory) => {
          const current = memory.state === 'current';
          const isEditing = editing?.id === memory.id;
          return (
            <div key={memory.id} className={`group rounded-xl border px-3 py-2 ${current ? 'border-[#2e2e34] bg-[#141417]' : 'border-[#222226] bg-[#111113]'}`}>
              {isEditing ? (
                <div className="space-y-1.5">
                  <input value={editing.subject} onChange={(e) => setEditing({ ...editing, subject: e.target.value })} maxLength={80} className={`${inputClass} w-full`} placeholder="What it is about" />
                  <textarea value={editing.text} onChange={(e) => setEditing({ ...editing, text: e.target.value })} maxLength={600} rows={3} className={`${inputClass} w-full resize-y`} />
                  <div className="flex items-center gap-2">
                    <button type="button" onClick={save} disabled={!editing.text.trim()} className="px-2 py-1 rounded-lg text-[11px] bg-zinc-100 text-black hover:bg-white disabled:opacity-40 flex items-center gap-1">
                      <FiCheck className="text-[10px]" /> Save
                    </button>
                    <button type="button" onClick={() => setEditing(null)} className="px-2 py-1 rounded-lg text-[11px] text-zinc-400 hover:text-white">
                      Cancel
                    </button>
                  </div>
                </div>
              ) : (
                <div className="flex items-start gap-2">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="text-[10px] font-mono text-cyan-300/80 bg-cyan-500/10 rounded px-1.5 py-0.5">{memory.subject}</span>
                      <span className="text-[10px] text-zinc-600 font-mono">{day(memory.created_at)}</span>
                      {memory.edited && <span className="text-[10px] text-zinc-500">edited</span>}
                      {memory.state === 'replaced' && <span className="text-[10px] text-amber-300/80">replaced {day(memory.replaced_at)}</span>}
                      {memory.state === 'forgotten' && <span className="text-[10px] text-zinc-500">forgotten {day(memory.forgotten_at)}</span>}
                    </div>
                    <p className={`mt-0.5 text-xs leading-snug ${current ? 'text-zinc-200' : 'text-zinc-500 line-through decoration-zinc-600'}`}>{memory.text}</p>
                    {memory.replaces && byId[memory.replaces] && (
                      <p className="mt-0.5 text-[10px] text-zinc-500">Before: {byId[memory.replaces].text}</p>
                    )}
                    {memory.source?.thread_id && (
                      <a href={`/c/${encodeURIComponent(memory.source.thread_id)}`} className="text-[10px] text-zinc-500 hover:text-cyan-300 underline-offset-2 hover:underline">
                        from this conversation
                      </a>
                    )}
                  </div>
                  {confirming === memory.id ? (
                    <span className="flex items-center gap-1 text-[10px] shrink-0">
                      <span className="text-rose-300">Delete?</span>
                      <button type="button" onClick={() => remove(memory.id)} className="px-1.5 py-0.5 rounded bg-rose-500/20 text-rose-200 hover:bg-rose-500/40">Yes</button>
                      <button type="button" onClick={() => setConfirming('')} className="px-1.5 py-0.5 rounded text-zinc-400 hover:text-white">No</button>
                    </span>
                  ) : (
                    <span className="flex items-center gap-0.5 shrink-0 opacity-0 group-hover:opacity-100 transition">
                      {current && (
                        <button type="button" onClick={() => setEditing({ id: memory.id, subject: memory.subject, text: memory.text })} className="p-1 rounded text-zinc-500 hover:text-white" title="Correct it">
                          <FiEdit2 className="text-[11px]" />
                        </button>
                      )}
                      <button type="button" onClick={() => setConfirming(memory.id)} className="p-1 rounded text-zinc-500 hover:text-rose-300" title="Delete it for good">
                        <FiTrash2 className="text-[11px]" />
                      </button>
                    </span>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
