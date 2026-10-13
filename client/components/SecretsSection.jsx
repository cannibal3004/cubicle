'use client';

import React, { useCallback, useEffect, useState } from 'react';
import { FiKey, FiTrash2, FiRefreshCw, FiPlus } from 'react-icons/fi';

function formatWhen(value) {
  if (!value) return '';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

// A list of named secrets with add, replace and remove. Values are write-only:
// the server never returns them, so the list shows names and dates only.
// `scope` is 'app' or 'bot'; in a bot's list, app secrets appear greyed with
// an Override button and the bot's own can be removed.
export default function SecretsSection({ scope, load, save, remove, hint }) {
  const [items, setItems] = useState([]);
  const [name, setName] = useState('');
  const [value, setValue] = useState('');
  const [replacing, setReplacing] = useState(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const data = await load();
      setItems(data?.secrets || []);
    } catch (err) {
      setError(err?.message || 'Could not load secrets.');
    }
  }, [load]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Not a <form>: the bot editor is one already, and a form inside a form is
  // dropped by the browser, which would make Add submit the whole editor.
  const submit = async (event) => {
    event?.preventDefault();
    const target = (replacing || name).trim().toUpperCase();
    if (!target || !value) return;
    setBusy(true);
    setError('');
    try {
      await save(target, value);
      setName('');
      setValue('');
      setReplacing(null);
      await refresh();
    } catch (err) {
      setError(err?.message || 'Could not save the secret.');
    } finally {
      setBusy(false);
    }
  };

  const onEnter = (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      event.stopPropagation();
      submit();
    }
  };

  const removeOne = async (target) => {
    setBusy(true);
    setError('');
    try {
      await remove(target);
      await refresh();
    } catch (err) {
      setError(err?.message || 'Could not remove the secret.');
    } finally {
      setBusy(false);
    }
  };

  const inputClass =
    'bg-[#222226] border border-[#2e2e34] rounded-xl px-3 py-2 text-xs text-zinc-200 font-mono placeholder-zinc-500 focus:outline-none focus:border-zinc-400 transition';

  return (
    <div className="space-y-3">
      {items.length === 0 && <p className="text-[11px] text-zinc-500">No secrets yet.</p>}
      {items.length > 0 && (
        <ul className="divide-y divide-[#232328] rounded-xl border border-[#27272a] overflow-hidden">
          {items.map((item) => {
            const inherited = scope === 'bot' && item.source === 'app';
            return (
              <li key={`${item.source}-${item.name}`} className={`flex items-center gap-3 px-3 py-2 ${inherited ? 'bg-[#141416]' : 'bg-[#18181b]'}`}>
                <FiKey className={`text-xs flex-shrink-0 ${inherited ? 'text-zinc-600' : 'text-amber-300/80'}`} />
                <span className={`font-mono text-xs truncate ${inherited ? 'text-zinc-500' : 'text-zinc-100'}`}>{item.name}</span>
                <span className="text-[10px] text-zinc-500 truncate">
                  {inherited ? (item.overridden ? 'from App Settings, overridden here' : 'from App Settings') : item.overrides_app ? 'overrides the app secret' : ''}
                  {!inherited && item.updated_at ? ` · ${formatWhen(item.updated_at)}` : ''}
                </span>
                <span className="ml-auto flex items-center gap-1 flex-shrink-0">
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => { setReplacing(item.name); setName(item.name); setValue(''); }}
                    className="px-2 py-1 rounded-lg text-[11px] text-zinc-300 hover:text-white hover:bg-[#1f1f23] disabled:opacity-50 transition flex items-center gap-1"
                    title={inherited ? 'Set a different value for this bot' : 'Enter a new value'}
                  >
                    <FiRefreshCw className="text-[10px]" /> {inherited ? 'Override' : 'Replace'}
                  </button>
                  {!inherited && (
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => removeOne(item.name)}
                      className="px-2 py-1 rounded-lg text-[11px] text-rose-400 hover:text-rose-300 hover:bg-rose-500/10 disabled:opacity-50 transition"
                      title="Remove this secret"
                    >
                      <FiTrash2 className="text-[10px]" />
                    </button>
                  )}
                </span>
              </li>
            );
          })}
        </ul>
      )}

      <div className="flex items-stretch gap-2">
        <input
          suppressHydrationWarning={true}
          type="text"
          value={name}
          onChange={(e) => { setName(e.target.value.toUpperCase()); if (replacing) setReplacing(null); }}
          placeholder="NAME"
          maxLength={64}
          autoComplete="off"
          spellCheck={false}
          onKeyDown={onEnter}
          className={`${inputClass} w-40 uppercase`}
        />
        <input
          suppressHydrationWarning={true}
          type="password"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder={replacing ? `New value for ${replacing}` : 'value'}
          autoComplete="new-password"
          onKeyDown={onEnter}
          className={`${inputClass} flex-1 min-w-0`}
        />
        <button
          type="button"
          onClick={submit}
          disabled={busy || !name.trim() || !value}
          className="px-3 py-2 rounded-xl text-xs font-medium bg-zinc-100 text-black hover:bg-white disabled:opacity-40 transition flex items-center gap-1.5 flex-shrink-0"
        >
          <FiPlus className="text-xs" /> {replacing ? 'Replace' : 'Add'}
        </button>
      </div>
      {error && <p className="text-[11px] text-rose-400">{error}</p>}
      <p className="text-[10px] leading-relaxed text-zinc-500">
        {hint || 'Available inside the bot computer as environment variables with these names. Values are stored encrypted and never shown again; a running computer picks up changes at its next start.'}
      </p>
    </div>
  );
}
