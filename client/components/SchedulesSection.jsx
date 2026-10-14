'use client';

import React, { useCallback, useEffect, useState } from 'react';
import { FiClock, FiTrash2, FiPlay, FiPlus, FiPower } from 'react-icons/fi';

const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

function formatWhen(value) {
  if (!value) return '';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}

const STATUS_STYLES = {
  completed: 'text-emerald-300',
  running: 'text-cyan-300',
  skipped: 'text-amber-300',
  needed_approval: 'text-amber-300',
  failed: 'text-rose-300',
  cancelled: 'text-zinc-400',
};

// A bot's scheduled tasks: list with enable, run now and remove, plus a form
// to add one. A cadence is once at a time, every N minutes or hours, daily at
// a time, or weekly on chosen days at a time, in the app's timezone.
export default function SchedulesSection({ load, create, update, remove, runNow }) {
  const [items, setItems] = useState([]);
  const [clock, setClock] = useState({ now: '', timezone: '' });
  const [name, setName] = useState('');
  const [prompt, setPrompt] = useState('');
  const [kind, setKind] = useState('daily');
  const [at, setAt] = useState('');
  const [every, setEvery] = useState('60');
  const [time, setTime] = useState('08:00');
  const [days, setDays] = useState([0, 1, 2, 3, 4]);
  const [showForm, setShowForm] = useState(false);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const data = await load();
      setItems(data?.schedules || []);
      setClock({ now: data?.now || '', timezone: data?.timezone || '' });
    } catch (err) {
      setError(err?.message || 'Could not load schedules.');
    }
  }, [load]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const run = async (action, failure) => {
    setBusy(true);
    setError('');
    try {
      await action();
      await refresh();
      return true;
    } catch (err) {
      setError(err?.message || failure);
      return false;
    } finally {
      setBusy(false);
    }
  };

  const cadence = () => {
    if (kind === 'once') return { kind, at: at.replace('T', ' ') };
    if (kind === 'every') return { kind, minutes: Number(every) };
    if (kind === 'daily') return { kind, time };
    return { kind, days, time };
  };

  const submit = async () => {
    if (!name.trim() || !prompt.trim()) return;
    const ok = await run(() => create({ name: name.trim(), prompt: prompt.trim(), cadence: cadence() }), 'Could not add the schedule.');
    if (ok) {
      setName('');
      setPrompt('');
      setShowForm(false);
    }
  };

  const inputClass =
    'bg-[#222226] border border-[#2e2e34] rounded-xl px-3 py-2 text-xs text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-zinc-400 transition';

  return (
    <div className="space-y-3">
      {items.length === 0 && <p className="text-[11px] text-zinc-500">No schedules yet.</p>}
      {items.length > 0 && (
        <ul className="divide-y divide-[#232328] rounded-xl border border-[#27272a] overflow-hidden">
          {items.map((item) => (
            <li key={item.id} className={`px-3 py-2 ${item.enabled ? 'bg-[#18181b]' : 'bg-[#141416]'}`}>
              <div className="flex items-center gap-3">
                <FiClock className={`text-xs flex-shrink-0 ${item.enabled ? 'text-cyan-300/80' : 'text-zinc-600'}`} />
                <span className={`text-xs truncate ${item.enabled ? 'text-zinc-100' : 'text-zinc-500'}`} title={item.prompt}>
                  {item.name}
                </span>
                <span className="text-[10px] text-zinc-500 truncate">
                  {item.cadence_text}
                  {item.created_by === 'bot' ? ' · set by the bot' : ''}
                </span>
                <span className="ml-auto flex items-center gap-1 flex-shrink-0">
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => run(() => runNow(item.id), 'Could not start the run.')}
                    className="px-2 py-1 rounded-lg text-[11px] text-zinc-300 hover:text-white hover:bg-[#1f1f23] disabled:opacity-50 transition"
                    title="Run now"
                  >
                    <FiPlay className="text-[10px]" />
                  </button>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => run(() => update(item.id, { enabled: !item.enabled }), 'Could not change the schedule.')}
                    className={`px-2 py-1 rounded-lg text-[11px] hover:bg-[#1f1f23] disabled:opacity-50 transition ${item.enabled ? 'text-emerald-300' : 'text-zinc-500'}`}
                    title={item.enabled ? 'Pause' : 'Resume'}
                  >
                    <FiPower className="text-[10px]" />
                  </button>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => run(() => remove(item.id), 'Could not remove the schedule.')}
                    className="px-2 py-1 rounded-lg text-[11px] text-rose-400 hover:text-rose-300 hover:bg-rose-500/10 disabled:opacity-50 transition"
                    title="Remove"
                  >
                    <FiTrash2 className="text-[10px]" />
                  </button>
                </span>
              </div>
              <div className="pl-6 mt-0.5 text-[10px] text-zinc-500 flex flex-wrap gap-x-3">
                {item.enabled && item.next_run_at && <span>next {formatWhen(item.next_run_at)}</span>}
                {!item.enabled && <span>paused</span>}
                {item.last_run_at && (
                  <span>
                    last {formatWhen(item.last_run_at)} ·{' '}
                    <span className={STATUS_STYLES[item.last_status] || 'text-zinc-400'}>{(item.last_status || '').replace('_', ' ')}</span>
                    {item.last_note ? ` (${item.last_note})` : ''}
                  </span>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}

      {!showForm && (
        <button
          type="button"
          onClick={() => setShowForm(true)}
          className="px-3 py-2 rounded-xl text-xs font-medium bg-[#1f1f23] text-zinc-200 hover:bg-[#27272b] transition flex items-center gap-1.5"
        >
          <FiPlus className="text-xs" /> Add a schedule
        </button>
      )}

      {showForm && (
        <div className="space-y-2 rounded-xl border border-[#27272a] p-3 bg-[#141416]">
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Name, e.g. Morning digest"
            maxLength={80}
            className={`${inputClass} w-full`}
          />
          <textarea
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder="What to do, written as a message to the bot. It starts a fresh turn in this chat, so say everything the bot needs."
            maxLength={4000}
            rows={3}
            className={`${inputClass} w-full resize-y`}
          />
          <div className="flex flex-wrap items-center gap-2">
            <select value={kind} onChange={(e) => setKind(e.target.value)} className={inputClass}>
              <option value="once">Once at</option>
              <option value="every">Every</option>
              <option value="daily">Daily at</option>
              <option value="weekly">Weekly at</option>
            </select>
            {kind === 'once' && <input type="datetime-local" value={at} onChange={(e) => setAt(e.target.value)} className={inputClass} />}
            {kind === 'every' && (
              <select value={every} onChange={(e) => setEvery(e.target.value)} className={inputClass}>
                <option value="15">15 minutes</option>
                <option value="30">30 minutes</option>
                <option value="60">hour</option>
                <option value="120">2 hours</option>
                <option value="360">6 hours</option>
                <option value="720">12 hours</option>
                <option value="1440">day</option>
              </select>
            )}
            {(kind === 'daily' || kind === 'weekly') && <input type="time" value={time} onChange={(e) => setTime(e.target.value)} className={inputClass} />}
            {kind === 'weekly' && (
              <div className="flex gap-1">
                {DAYS.map((label, index) => (
                  <button
                    key={label}
                    type="button"
                    onClick={() => setDays((prev) => (prev.includes(index) ? prev.filter((d) => d !== index) : [...prev, index].sort()))}
                    className={`px-2 py-1 rounded-lg text-[10px] font-mono border transition ${days.includes(index) ? 'border-cyan-400 text-cyan-200 bg-cyan-500/10' : 'border-[#2e2e34] text-zinc-500'}`}
                  >
                    {label}
                  </button>
                ))}
              </div>
            )}
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={submit}
              disabled={busy || !name.trim() || !prompt.trim() || (kind === 'once' && !at) || (kind === 'weekly' && days.length === 0)}
              className="px-3 py-2 rounded-xl text-xs font-medium bg-zinc-100 text-black hover:bg-white disabled:opacity-40 transition flex items-center gap-1.5"
            >
              <FiPlus className="text-xs" /> Add
            </button>
            <button type="button" onClick={() => setShowForm(false)} className="px-3 py-2 rounded-xl text-xs text-zinc-400 hover:text-white transition">
              Cancel
            </button>
          </div>
        </div>
      )}

      {error && <p className="text-[11px] text-rose-400">{error}</p>}
      <p className="text-[10px] leading-relaxed text-zinc-500">
        Each run posts the prompt into this bot's chat and starts a normal turn, with the bot's tools, notes and to-do list; approvals work as usual, so a run that
        needs one waits for you. A busy bot skips that run. Times are in {clock.timezone || 'the server timezone'}
        {clock.now ? ` (now ${clock.now})` : ''}. The bot can add schedules itself with its schedule tools, after your approval.
      </p>
    </div>
  );
}
