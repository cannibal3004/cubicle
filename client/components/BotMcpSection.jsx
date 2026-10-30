'use client';

import React, { useCallback, useEffect, useState } from 'react';
import { FiServer, FiTrash2, FiPlus, FiPower, FiPlay, FiEdit2, FiCheck } from 'react-icons/fi';
import { argsText, mapText } from './McpServersSection';

const STATUS_STYLES = {
  connected: 'text-emerald-300',
  connecting: 'text-cyan-300',
  error: 'text-rose-300',
  stopped: 'text-zinc-400',
  disabled: 'text-zinc-500',
};

function parseMap(text) {
  const out = {};
  for (const line of (text || '').split(/\n|;/)) {
    const index = line.indexOf('=');
    if (index > 0) out[line.slice(0, index).trim()] = line.slice(index + 1).trim();
  }
  return out;
}

// MCP servers that run inside this bot's computer: a command the bot (or
// you) installed there, bridged to the API by the computer. Only this bot
// gets their tools.
export default function BotMcpSection({ load, create, update, remove, test }) {
  const [items, setItems] = useState([]);
  const [showForm, setShowForm] = useState(false);
  // The id of the server the form is changing, or null when it adds one.
  const [editing, setEditing] = useState(null);
  const [name, setName] = useState('');
  const [command, setCommand] = useState('');
  const [args, setArgs] = useState('');
  const [vars, setVars] = useState('');
  const [trust, setTrust] = useState(false);
  const [results, setResults] = useState({});
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const data = await load();
      setItems(data?.servers || []);
    } catch (err) {
      setError(err?.message || 'Could not load the servers.');
    }
  }, [load]);

  useEffect(() => {
    refresh();
    const interval = window.setInterval(refresh, 5000);
    return () => window.clearInterval(interval);
  }, [refresh]);

  const run = async (action, failure) => {
    setBusy(true);
    setError('');
    try {
      const result = await action();
      await refresh();
      return result;
    } catch (err) {
      setError(err?.message || failure);
      return null;
    } finally {
      setBusy(false);
    }
  };

  const closeForm = () => {
    setName('');
    setCommand('');
    setArgs('');
    setVars('');
    setTrust(false);
    setEditing(null);
    setShowForm(false);
  };

  // Values typed in rather than written as $NAME come back hidden; saved unchanged, they stay as they are.
  const startEdit = (item) => {
    setEditing(item.id);
    setName(item.name);
    setCommand(item.command || '');
    setArgs(argsText(item.args));
    setVars(mapText(item.env));
    setTrust(!!item.trust_read_only);
    setShowForm(true);
  };

  const submit = async () => {
    const server = { name: name.trim(), transport: 'computer', command: command.trim(), args: args.trim(), env: parseMap(vars), trust_read_only: trust };
    const ok = editing
      ? await run(() => update(editing, server), 'Could not save the server.')
      : await run(() => create(server), 'Could not add the server.');
    if (ok) closeForm();
  };

  const inputClass =
    'bg-[#222226] border border-[#2e2e34] rounded-xl px-3 py-2 text-xs text-zinc-200 font-mono placeholder-zinc-500 focus:outline-none focus:border-zinc-400 transition';

  return (
    <div className="space-y-3">
      {items.length === 0 && <p className="text-[11px] text-zinc-500">No servers on this computer yet.</p>}
      {items.length > 0 && (
        <ul className="divide-y divide-[#232328] rounded-xl border border-[#27272a] overflow-hidden">
          {items.map((item) => {
            const result = results[item.id];
            return (
              <li key={item.id} className={`px-3 py-2 ${item.enabled ? 'bg-[#18181b]' : 'bg-[#141416]'}`}>
                <div className="flex items-center gap-3">
                  <FiServer className={`text-xs flex-shrink-0 ${item.enabled ? 'text-cyan-300/80' : 'text-zinc-600'}`} />
                  <span className={`text-xs truncate ${item.enabled ? 'text-zinc-100' : 'text-zinc-500'}`}>{item.name}</span>
                  <span className="text-[10px] text-zinc-500 truncate font-mono" title={`${item.command} ${(item.args || []).join(' ')}`}>
                    {item.command} {(item.args || []).join(' ')}
                  </span>
                  <span className="ml-auto flex items-center gap-1 flex-shrink-0">
                    <button type="button" disabled={busy} onClick={async () => { const r = await run(() => test(item.id), 'Test failed.'); if (r) setResults((prev) => ({ ...prev, [item.id]: r })); }} className="px-2 py-1 rounded-lg text-[11px] text-zinc-300 hover:text-white hover:bg-[#1f1f23] disabled:opacity-50 transition" title="Start the computer if needed, connect and list its tools">
                      <FiPlay className="text-[10px]" />
                    </button>
                    <button type="button" disabled={busy} onClick={() => startEdit(item)} className="px-2 py-1 rounded-lg text-[11px] text-zinc-300 hover:text-white hover:bg-[#1f1f23] disabled:opacity-50 transition" title="Edit">
                      <FiEdit2 className="text-[10px]" />
                    </button>
                    <button type="button" disabled={busy} onClick={() => run(() => update(item.id, { enabled: !item.enabled }), 'Could not change the server.')} className={`px-2 py-1 rounded-lg text-[11px] hover:bg-[#1f1f23] disabled:opacity-50 transition ${item.enabled ? 'text-emerald-300' : 'text-zinc-500'}`} title={item.enabled ? 'Switch off' : 'Switch on'}>
                      <FiPower className="text-[10px]" />
                    </button>
                    <button type="button" disabled={busy} onClick={() => run(() => remove(item.id), 'Could not remove the server.')} className="px-2 py-1 rounded-lg text-[11px] text-rose-400 hover:text-rose-300 hover:bg-rose-500/10 disabled:opacity-50 transition" title="Remove">
                      <FiTrash2 className="text-[10px]" />
                    </button>
                  </span>
                </div>
                <div className="pl-6 mt-0.5 text-[10px] text-zinc-500 flex flex-wrap gap-x-3">
                  <span className={STATUS_STYLES[item.status] || 'text-zinc-400'}>{item.status}</span>
                  {item.status === 'connected' && <span>{item.tool_count} tools{item.server_name ? ` · ${item.server_name}` : ''}</span>}
                  {item.error && <span className="text-rose-300 truncate" title={item.error}>{item.error}</span>}
                  {item.trust_read_only && <span>trusted read-only</span>}
                  {item.secret_references?.length > 0 && <span>uses: {item.secret_references.join(', ')}</span>}
                  {item.created_by === 'bot' && <span>added by the bot</span>}
                </div>
                {result && (
                  <div className="pl-6 mt-1 text-[10px] text-zinc-400">
                    {result.ok ? `Connected: ${result.tools.map((t) => t.name).join(', ') || 'no tools'}` : `Failed: ${result.error}`}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}

      {!showForm && (
        <button type="button" onClick={() => setShowForm(true)} className="px-3 py-2 rounded-xl text-xs font-medium bg-[#1f1f23] text-zinc-200 hover:bg-[#27272b] transition flex items-center gap-1.5">
          <FiPlus className="text-xs" /> Add a server
        </button>
      )}

      {showForm && (
        <div className="space-y-2 rounded-xl border border-[#27272a] p-3 bg-[#141416]">
          <input type="text" value={name} onChange={(e) => setName(e.target.value)} placeholder="Name, e.g. Shodan" maxLength={60} className={`${inputClass} w-full`} />
          <div className="flex items-center gap-2">
            <input type="text" value={command} onChange={(e) => setCommand(e.target.value)} placeholder="uvx" className={`${inputClass} w-28`} />
            <input type="text" value={args} onChange={(e) => setArgs(e.target.value)} placeholder="shodan-mcp" className={`${inputClass} flex-1`} />
          </div>
          <textarea
            value={vars}
            onChange={(e) => setVars(e.target.value)}
            placeholder={'Environment, one per line: SHODAN_API_KEY=$SHODAN_API_KEY (a secret of this bot or the app)'}
            rows={2}
            className={`${inputClass} w-full resize-y`}
          />
          <label className="flex items-center gap-2 text-[11px] text-zinc-400">
            <input type="checkbox" checked={trust} onChange={(e) => setTrust(e.target.checked)} className="accent-cyan-400" />
            Treat all its tools as read-only (no approval); only for servers that cannot change anything.
          </label>
          <div className="flex items-center gap-2">
            <button type="button" onClick={submit} disabled={busy || !name.trim() || !command.trim()} className="px-3 py-2 rounded-xl text-xs font-medium bg-zinc-100 text-black hover:bg-white disabled:opacity-40 transition flex items-center gap-1.5">
              {editing ? <><FiCheck className="text-xs" /> Save</> : <><FiPlus className="text-xs" /> Add</>}
            </button>
            <button type="button" onClick={closeForm} className="px-3 py-2 rounded-xl text-xs text-zinc-400 hover:text-white transition">
              Cancel
            </button>
          </div>
        </div>
      )}

      {error && <p className="text-[11px] text-rose-400">{error}</p>}
      <p className="text-[10px] leading-relaxed text-zinc-500">
        The command runs inside this bot&apos;s computer, with its packages and files, and only this bot gets the tools. Install the server
        there first (the bot can do it with its terminal: npm, pip, uv). $NAME in a value is one of the computer&apos;s environment variables,
        which is where this bot&apos;s secrets and the app secrets are. Servers start with the computer and stop with it.
      </p>
    </div>
  );
}
