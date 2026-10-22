'use client';

import React, { useEffect, useState } from 'react';
import { FiCheckSquare, FiChevronRight, FiPlus, FiX } from 'react-icons/fi';

// The bot's working list for the job at hand, in a panel beside the chat
// (open when the list has items, a narrow rail otherwise). The bot
// keeps it with the todo tools; the user can tick, reword, add and remove
// items here. Every change saves the whole list; the server records the
// edit in the thread so the model sees it.
const STATE_STYLES = {
  open: 'border-zinc-600',
  doing: 'border-cyan-400 bg-cyan-500/20',
  done: 'border-emerald-500 bg-emerald-500/30 text-emerald-200',
};

export default function TodoCard({ bot, onSave }) {
  const items = Array.isArray(bot?.todo) ? bot.todo : [];
  const [open, setOpen] = useState(items.length > 0);
  const [draft, setDraft] = useState('');
  const [editing, setEditing] = useState(null); // { id, text }
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    setEditing(null);
    setDraft('');
    setError(null);
    setOpen((Array.isArray(bot?.todo) ? bot.todo : []).length > 0);
  }, [bot?.id]); // eslint-disable-line react-hooks/exhaustive-deps
  // A list that fills (the bot wrote it) opens the panel; emptying it does not close it.
  const count = items.length;
  useEffect(() => {
    if (count > 0) setOpen(true);
  }, [count > 0]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!bot) return null;
  const done = items.filter((i) => i.state === 'done').length;
  const doing = items.find((i) => i.state === 'doing');

  const save = async (next) => {
    if (!onSave) return;
    setSaving(true);
    setError(null);
    try {
      await onSave(next);
    } catch (err) {
      setError(err?.message || 'Could not save the list.');
    } finally {
      setSaving(false);
    }
  };

  const toggle = (item) => save(items.map((i) => (i.id === item.id ? { ...i, state: i.state === 'done' ? 'open' : 'done' } : i)));
  const remove = (item) => save(items.filter((i) => i.id !== item.id));
  const clearDone = () => save(items.filter((i) => i.state !== 'done'));
  const add = () => {
    const text = draft.trim();
    if (!text) return;
    setDraft('');
    save([...items, { text, state: 'open' }]);
  };
  const commitEdit = () => {
    if (!editing) return;
    const text = editing.text.trim();
    const current = items.find((i) => i.id === editing.id);
    setEditing(null);
    if (!text || !current || text === current.text) return;
    save(items.map((i) => (i.id === editing.id ? { ...i, text } : i)));
  };

  const summary = items.length
    ? `${done} of ${items.length} done${doing ? ` · now: ${doing.text}` : ''}`
    : 'empty';

  if (!open) {
    return (
      <aside className="flex-shrink-0 w-9 border-l border-[#1f1f23] bg-[#0b0b0d] flex flex-col items-center pt-3 z-10" aria-label="To-do list">
        <button
          type="button"
          onClick={() => setOpen(true)}
          className="flex flex-col items-center gap-2 text-zinc-500 hover:text-zinc-200"
          title={`To-do · ${summary}`}
          aria-expanded={false}
        >
          <FiCheckSquare className="text-sm text-cyan-300/80" />
          <span className="text-[10px] font-mono" style={{ writingMode: 'vertical-rl' }}>
            {items.length ? `${done}/${items.length}` : 'to-do'}
          </span>
        </button>
      </aside>
    );
  }

  return (
    <aside className="flex-shrink-0 w-72 border-l border-[#1f1f23] bg-[#0b0b0d] flex flex-col min-h-0 z-10" aria-label="To-do list">
      <div className="flex flex-col min-h-0 flex-1 text-[11px] text-zinc-300">
        <div>
          <button
            type="button"
            onClick={() => setOpen(false)}
            className="w-full flex items-center gap-2 px-3 py-2 text-left font-mono text-zinc-400 hover:text-zinc-200 border-b border-[#1f1f23]"
            aria-expanded={open}
            title="Collapse"
          >
            <FiCheckSquare className="text-xs text-cyan-300/80" />
            <span className="text-zinc-200">To-do</span>
            <span className="truncate">· {summary}</span>
            {saving && <span className="ml-auto text-zinc-500">saving…</span>}
            <FiChevronRight className="text-xs ml-auto" />
          </button>

          {open && (
            <div className="px-3 py-2.5 space-y-1.5 overflow-y-auto max-h-[calc(100vh-7rem)]">
              {items.map((item) => (
                <div key={item.id} className="group flex items-start gap-2">
                  <button
                    type="button"
                    onClick={() => toggle(item)}
                    disabled={saving}
                    className={`mt-0.5 w-3.5 h-3.5 rounded border flex-shrink-0 flex items-center justify-center text-[9px] ${STATE_STYLES[item.state] || STATE_STYLES.open}`}
                    title={item.state === 'done' ? 'Mark open' : item.state === 'doing' ? 'In progress · mark done' : 'Mark done'}
                    aria-label={`${item.state}: ${item.text}`}
                  >
                    {item.state === 'done' ? '✓' : item.state === 'doing' ? '›' : ''}
                  </button>
                  {editing?.id === item.id ? (
                    <input
                      autoFocus
                      value={editing.text}
                      onChange={(e) => setEditing({ id: item.id, text: e.target.value })}
                      onBlur={commitEdit}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') commitEdit();
                        if (e.key === 'Escape') setEditing(null);
                      }}
                      maxLength={300}
                      className="flex-1 bg-[#18181b] border border-[#3f3f46] rounded px-1.5 py-0.5 text-zinc-100 outline-none focus:border-cyan-500"
                    />
                  ) : (
                    <span
                      onDoubleClick={() => setEditing({ id: item.id, text: item.text })}
                      title="Double-click to edit"
                      className={`flex-1 leading-snug break-words ${item.state === 'done' ? 'line-through text-zinc-500' : item.state === 'doing' ? 'text-cyan-100' : 'text-zinc-200'}`}
                    >
                      {item.text}
                    </span>
                  )}
                  <span className="font-mono text-[9px] text-zinc-600 mt-0.5">{item.id}</span>
                  <button
                    type="button"
                    onClick={() => remove(item)}
                    disabled={saving}
                    className="opacity-0 group-hover:opacity-100 text-zinc-500 hover:text-red-300 transition"
                    title="Remove"
                    aria-label={`Remove ${item.text}`}
                  >
                    <FiX className="text-xs" />
                  </button>
                </div>
              ))}

              <div className="flex items-center gap-2 pt-1">
                <FiPlus className="text-xs text-zinc-500 flex-shrink-0" />
                <input
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') add();
                  }}
                  placeholder={items.length ? 'Add an item' : 'Add the first item, or let the bot write the list'}
                  maxLength={300}
                  disabled={saving}
                  className="flex-1 bg-transparent border-b border-transparent focus:border-[#3f3f46] px-1 py-0.5 text-zinc-200 placeholder:text-zinc-600 outline-none"
                />
                {done > 0 && (
                  <button type="button" onClick={clearDone} disabled={saving} className="text-zinc-500 hover:text-zinc-200 font-mono">
                    clear done
                  </button>
                )}
              </div>
              {error && <div className="text-red-300">{error}</div>}
            </div>
          )}
        </div>
      </div>
    </aside>
  );
}
