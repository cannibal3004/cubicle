'use client';

import React, { useCallback, useEffect, useState } from 'react';
import { FiServer, FiTrash2, FiPlus, FiPower, FiPlay, FiEdit2, FiCheck } from 'react-icons/fi';
import { argsText, mapText } from './McpServersSection';
import McpToolPicker from './McpToolPicker';

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

// This bot's own MCP servers: a command the bot (or you) installed in its
// computer, bridged to the API by the computer; or a URL the API connects
// to, with the bot's secrets. Only this bot gets their tools.
export default function BotMcpSection({ load, create, update, remove, test, loadTools }) {
  const [items, setItems] = useState([]);
  const [showForm, setShowForm] = useState(false);
  // The id of the server the form is changing, or null when it adds one.
  const [editing, setEditing] = useState(null);
  // The tool choice made in the form ({tool_mode, tool_names}), or null when untouched.
  const [toolFilter, setToolFilter] = useState(null);
  const loadEditingTools = useCallback(() => loadTools(editing), [loadTools, editing]);
  const [name, setName] = useState('');
  // computer (a command in its computer), http (a URL, streamable HTTP or SSE) or sse (SSE only).
  const [transport, setTransport] = useState('computer');
  const [url, setUrl] = useState('');
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

  const isUrl = transport !== 'computer';

  const closeForm = () => {
    setName('');
    setTransport('computer');
    setUrl('');
    setCommand('');
    setArgs('');
    setVars('');
    setTrust(false);
    setEditing(null);
    setToolFilter(null);
    setShowForm(false);
  };

  // Values typed in rather than written as $NAME come back hidden; saved unchanged, they stay as they are.
  const startEdit = (item) => {
    setEditing(item.id);
    setName(item.name);
    setTransport(['http', 'sse'].includes(item.transport) ? item.transport : 'computer');
    setUrl(item.url || '');
    setCommand(item.command || '');
    setArgs(argsText(item.args));
    setVars(mapText(item.transport === 'computer' ? item.env : item.headers));
    setTrust(!!item.trust_read_only);
    setShowForm(true);
  };

  const submit = async () => {
    const server = isUrl
      ? { name: name.trim(), transport, url: url.trim(), headers: parseMap(vars), trust_read_only: trust }
      : { name: name.trim(), transport: 'computer', command: command.trim(), args: args.trim(), env: parseMap(vars), trust_read_only: trust };
    const ok = editing
      ? await run(() => update(editing, { ...server, ...(toolFilter || {}) }), 'Could not save the server.')
      : await run(() => create(server), 'Could not add the server.');
    if (ok) closeForm();
  };

  const inputClass =
    'bg-[#222226] border border-[#2e2e34] rounded-xl px-3 py-2 text-xs text-zinc-200 font-mono placeholder-zinc-500 focus:outline-none focus:border-zinc-400 transition';

  return (
    <div className="space-y-3">
      {items.length === 0 && <p className="text-[11px] text-zinc-500">No servers of its own yet.</p>}
      {items.length > 0 && (
        <ul className="divide-y divide-[#232328] rounded-xl border border-[#27272a] overflow-hidden">
          {items.map((item) => {
            const result = results[item.id];
            return (
              <li key={item.id} className={`px-3 py-2 ${item.enabled ? 'bg-[#18181b]' : 'bg-[#141416]'}`}>
                <div className="flex items-center gap-3">
                  <FiServer className={`text-xs flex-shrink-0 ${item.enabled ? 'text-cyan-300/80' : 'text-zinc-600'}`} />
                  <span className={`text-xs truncate ${item.enabled ? 'text-zinc-100' : 'text-zinc-500'}`}>{item.name}</span>
                  <span className="text-[10px] text-zinc-500 truncate font-mono" title={item.transport === 'computer' ? `${item.command} ${(item.args || []).join(' ')}` : item.url}>
                    {item.transport === 'computer' ? `${item.command} ${(item.args || []).join(' ')}` : item.url}
                  </span>
                  <span className="ml-auto flex items-center gap-1 flex-shrink-0">
                    <button type="button" disabled={busy} onClick={async () => { const r = await run(() => test(item.id), 'Test failed.'); if (r) setResults((prev) => ({ ...prev, [item.id]: r })); }} className="px-2 py-1 rounded-lg text-[11px] text-zinc-300 hover:text-white hover:bg-[#1f1f23] disabled:opacity-50 transition" title={item.transport === 'computer' ? 'Start the computer if needed, connect and list its tools' : 'Connect and list its tools'}>
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
                  {item.status === 'connected' && <span>{item.tool_count} tools{item.server_name ? ` · ${item.server_name}` : ''}{item.via === 'sse' ? ' · over SSE' : ''}</span>}
                  <span>{item.transport === 'computer' ? 'in its computer' : 'URL'}</span>
                  {item.error && <span className="text-rose-300 truncate" title={item.error}>{item.error}</span>}
                  {item.tool_mode === 'all' && item.offered_count > 60 && (
                    <span className="text-amber-400/90" title="Edit the server to choose which tools it offers">
                      offers all {item.offered_count} tools: choose which in Edit
                    </span>
                  )}
                  {item.tool_mode !== 'all' && item.status === 'connected' && <span>offers {item.offered_count} of {item.tool_count}</span>}
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
          <div className="flex items-center gap-2">
            <input type="text" value={name} onChange={(e) => setName(e.target.value)} placeholder="Name, e.g. Shodan" maxLength={60} className={`${inputClass} flex-1`} />
            <select value={transport} onChange={(e) => setTransport(e.target.value)} className={inputClass}>
              <option value="computer">Command in its computer</option>
              <option value="http">URL (streamable HTTP or SSE)</option>
              <option value="sse">URL (SSE only)</option>
            </select>
          </div>
          {isUrl ? (
            <input type="text" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://mcp.example.com/mcp" className={`${inputClass} w-full`} />
          ) : (
            <div className="flex items-center gap-2">
              <input type="text" value={command} onChange={(e) => setCommand(e.target.value)} placeholder="uvx" className={`${inputClass} w-28`} />
              <input type="text" value={args} onChange={(e) => setArgs(e.target.value)} placeholder="shodan-mcp" className={`${inputClass} flex-1`} />
            </div>
          )}
          <textarea
            value={vars}
            onChange={(e) => setVars(e.target.value)}
            placeholder={
              isUrl
                ? 'Headers, one per line: Authorization=Bearer $SHODAN_API_KEY (a secret of this bot or yours)'
                : 'Environment, one per line: SHODAN_API_KEY=$SHODAN_API_KEY (a secret of this bot or the app)'
            }
            rows={2}
            className={`${inputClass} w-full resize-y`}
          />
          {editing && loadTools && (
            <McpToolPicker load={loadEditingTools} mode={items.find((i) => i.id === editing)?.tool_mode} onChange={setToolFilter} />
          )}
          <label className="flex items-center gap-2 text-[11px] text-zinc-400">
            <input type="checkbox" checked={trust} onChange={(e) => setTrust(e.target.checked)} className="accent-cyan-400" />
            Treat all its tools as read-only (no approval); only for servers that cannot change anything.
          </label>
          <div className="flex items-center gap-2">
            <button type="button" onClick={submit} disabled={busy || !name.trim() || !(isUrl ? url.trim() : command.trim())} className="px-3 py-2 rounded-xl text-xs font-medium bg-zinc-100 text-black hover:bg-white disabled:opacity-40 transition flex items-center gap-1.5">
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
        Only this bot gets these servers&apos; tools. A command runs inside its computer, with its packages and files: install the server
        there first (the bot can do it with its terminal: npm, pip, uv); $NAME in a value is one of the computer&apos;s environment variables,
        which is where this bot&apos;s secrets and the app secrets are, and the server starts and stops with the computer. A URL is reached
        from the Cubicle server, streamable HTTP or SSE, whichever it speaks; $NAME in a header is a secret of this bot or yours.
      </p>
    </div>
  );
}
