'use client';

import React, { useCallback, useEffect, useState } from 'react';
import { FiServer, FiTrash2, FiRefreshCw, FiPlus, FiPower, FiPlay } from 'react-icons/fi';

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

function mapText(map) {
  return Object.entries(map || {})
    .map(([k, v]) => `${k}=${v}`)
    .join('\n');
}

// MCP servers: a command run over stdio or a streamable-HTTP URL, whose tools
// the bots get like a connected app's. Secrets never go in here: write $NAME
// to use an app secret.
export default function McpServersSection({ admin = false, load, create, update, remove, test, testDraft, reconnect }) {
  const [items, setItems] = useState([]);
  const [showForm, setShowForm] = useState(false);
  const [name, setName] = useState('');
  const [transport, setTransport] = useState('stdio');
  const [command, setCommand] = useState('');
  const [args, setArgs] = useState('');
  const [url, setUrl] = useState('');
  const [vars, setVars] = useState('');
  const [trust, setTrust] = useState(false);
  const [scope, setScope] = useState('user');
  const [draftResult, setDraftResult] = useState(null);
  const [results, setResults] = useState({});
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const data = await load();
      setItems(data?.servers || []);
    } catch (err) {
      setError(err?.message || 'Could not load MCP servers.');
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

  const draft = () => ({
    name: name.trim(),
    transport,
    command: transport === 'stdio' ? command.trim() : undefined,
    args: transport === 'stdio' ? args.trim() : undefined,
    env: transport === 'stdio' ? parseMap(vars) : undefined,
    url: transport === 'http' ? url.trim() : undefined,
    headers: transport === 'http' ? parseMap(vars) : undefined,
    trust_read_only: trust,
    scope: admin ? scope : 'user',
  });

  const canSubmit = name.trim() && (transport === 'stdio' ? command.trim() : url.trim());

  const submit = async () => {
    const ok = await run(() => create(draft()), 'Could not add the server.');
    if (ok) {
      setName('');
      setCommand('');
      setArgs('');
      setUrl('');
      setVars('');
      setTrust(false);
      setDraftResult(null);
      setShowForm(false);
    }
  };

  const inputClass =
    'bg-[#222226] border border-[#2e2e34] rounded-xl px-3 py-2 text-xs text-zinc-200 font-mono placeholder-zinc-500 focus:outline-none focus:border-zinc-400 transition';

  return (
    <div className="space-y-3">
      {items.length === 0 && <p className="text-[11px] text-zinc-500">No MCP servers yet.</p>}
      {items.length > 0 && (
        <ul className="divide-y divide-[#232328] rounded-xl border border-[#27272a] overflow-hidden">
          {items.map((item) => {
            const result = results[item.id];
            return (
              <li key={item.id} className={`px-3 py-2 ${item.enabled ? 'bg-[#18181b]' : 'bg-[#141416]'}`}>
                <div className="flex items-center gap-3">
                  <FiServer className={`text-xs flex-shrink-0 ${item.enabled ? 'text-cyan-300/80' : 'text-zinc-600'}`} />
                  <span className={`text-xs truncate ${item.enabled ? 'text-zinc-100' : 'text-zinc-500'}`}>{item.name}</span>
                  <span className="text-[10px] text-zinc-500 truncate font-mono" title={item.transport === 'stdio' ? `${item.command} ${item.args.join(' ')}` : item.url}>
                    {item.transport === 'stdio' ? `${item.command} ${item.args.join(' ')}` : item.url}
                  </span>
                  <span className="ml-auto flex items-center gap-1 flex-shrink-0">
                    <button type="button" disabled={busy} onClick={async () => { const r = await run(() => test(item.id), 'Test failed.'); if (r) setResults((prev) => ({ ...prev, [item.id]: r })); }} className="px-2 py-1 rounded-lg text-[11px] text-zinc-300 hover:text-white hover:bg-[#1f1f23] disabled:opacity-50 transition" title="Connect and list its tools">
                      <FiPlay className="text-[10px]" />
                    </button>
                    <button type="button" disabled={busy} onClick={() => run(() => reconnect(item.id), 'Reconnect failed.')} className="px-2 py-1 rounded-lg text-[11px] text-zinc-300 hover:text-white hover:bg-[#1f1f23] disabled:opacity-50 transition" title="Reconnect">
                      <FiRefreshCw className="text-[10px]" />
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
                  <span>{item.scope === 'system' ? 'system · everyone' : 'yours'}</span>
                  {item.trust_read_only && <span>trusted read-only</span>}
                  {item.secret_references?.length > 0 && <span>secrets: {item.secret_references.join(', ')}</span>}
                  {item.created_by === 'bot' && <span>added by a bot</span>}
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
            <input type="text" value={name} onChange={(e) => setName(e.target.value)} placeholder="Name, e.g. Context7" maxLength={60} className={`${inputClass} flex-1`} />
            <select value={transport} onChange={(e) => setTransport(e.target.value)} className={inputClass}>
              <option value="stdio">Command (stdio)</option>
              <option value="http">URL (streamable HTTP)</option>
            </select>
          </div>
          {transport === 'stdio' ? (
            <div className="flex items-center gap-2">
              <input type="text" value={command} onChange={(e) => setCommand(e.target.value)} placeholder="npx" className={`${inputClass} w-28`} />
              <input type="text" value={args} onChange={(e) => setArgs(e.target.value)} placeholder="-y @upstash/context7-mcp" className={`${inputClass} flex-1`} />
            </div>
          ) : (
            <input type="text" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://mcp.example.com/mcp" className={`${inputClass} w-full`} />
          )}
          <textarea
            value={vars}
            onChange={(e) => setVars(e.target.value)}
            placeholder={transport === 'stdio' ? 'Environment, one per line: CONTEXT7_API_KEY=$CONTEXT7_API_KEY' : 'Headers, one per line: Authorization=Bearer $EXA_API_KEY'}
            rows={2}
            className={`${inputClass} w-full resize-y`}
          />
          {admin && (
            <label className="flex items-center gap-2 text-[11px] text-zinc-400">
              <select value={scope} onChange={(e) => setScope(e.target.value)} className={inputClass}>
                <option value="user">Only my bots</option>
                <option value="system">Every bot of every account (system)</option>
              </select>
            </label>
          )}
          <label className="flex items-center gap-2 text-[11px] text-zinc-400">
            <input type="checkbox" checked={trust} onChange={(e) => setTrust(e.target.checked)} className="accent-cyan-400" />
            Treat all its tools as read-only (no approval); only for servers that cannot change anything, such as documentation or search.
          </label>
          <div className="flex items-center gap-2">
            <button type="button" onClick={submit} disabled={busy || !canSubmit} className="px-3 py-2 rounded-xl text-xs font-medium bg-zinc-100 text-black hover:bg-white disabled:opacity-40 transition flex items-center gap-1.5">
              <FiPlus className="text-xs" /> Add
            </button>
            <button type="button" onClick={async () => { const r = await run(() => testDraft(draft()), 'Test failed.'); if (r) setDraftResult(r); }} disabled={busy || !canSubmit} className="px-3 py-2 rounded-xl text-xs text-zinc-200 hover:text-white bg-[#1f1f23] disabled:opacity-40 transition">
              Test first
            </button>
            <button type="button" onClick={() => setShowForm(false)} className="px-3 py-2 rounded-xl text-xs text-zinc-400 hover:text-white transition">
              Cancel
            </button>
          </div>
          {draftResult && (
            <p className="text-[10px] text-zinc-400">{draftResult.ok ? `Connected: ${draftResult.tools.map((t) => t.name).join(', ') || 'no tools'}` : `Failed: ${draftResult.error}`}</p>
          )}
        </div>
      )}

      {error && <p className="text-[11px] text-rose-400">{error}</p>}
      <p className="text-[10px] leading-relaxed text-zinc-500">
        A server&apos;s tools are offered to every bot like a connected app&apos;s, and can be switched off per bot in its Tools menu. Tools the server
        marks read-only run directly; the rest ask you first. Write $NAME in a value to use an app secret, never the secret itself. Commands run
        inside the API container, which has node, npx and uvx.
      </p>
    </div>
  );
}
