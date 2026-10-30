'use client';

import React, { useEffect, useMemo, useState } from 'react';
import { FiChevronRight, FiSearch } from 'react-icons/fi';

// The part of a name before its first _ - or . ("droplet" of droplet_create): tools that share
// it are grouped, so a server with hundreds of tools reads as a few dozen families.
function prefixOf(name) {
  const match = /^([A-Za-z0-9]+)[_.-]/.exec(name);
  return match ? match[1].toLowerCase() : '';
}

function groupTools(tools) {
  const counts = {};
  for (const tool of tools) counts[prefixOf(tool.name)] = (counts[prefixOf(tool.name)] || 0) + 1;
  const groups = {};
  for (const tool of tools) {
    const prefix = prefixOf(tool.name);
    const key = prefix && counts[prefix] > 1 ? prefix : '';
    (groups[key] = groups[key] || []).push(tool);
  }
  return Object.entries(groups).sort(([a], [b]) => (a === '' ? 1 : b === '' ? -1 : a.localeCompare(b)));
}

// What the server stores, from what is ticked: with new tools offered, everything but the
// unticked ones ("except", or "all" when none is unticked); otherwise only the ticked ones.
function filterFor(tools, offered, newOn) {
  if (newOn) {
    const off = tools.filter((t) => !offered.has(t.name)).map((t) => t.name);
    return off.length ? { tool_mode: 'except', tool_names: off } : { tool_mode: 'all', tool_names: [] };
  }
  return { tool_mode: 'only', tool_names: tools.filter((t) => offered.has(t.name)).map((t) => t.name) };
}

// Which of a server's tools it offers to the bots. `load()` lists them all, each marked
// offered under the saved choice; `onChange({tool_mode, tool_names})` gets the new choice.
export default function McpToolPicker({ load, mode = 'all', onChange }) {
  const [tools, setTools] = useState(null);
  const [error, setError] = useState('');
  const [offered, setOffered] = useState(new Set());
  const [newOn, setNewOn] = useState(mode !== 'only');
  const [query, setQuery] = useState('');
  const [open, setOpen] = useState({});

  useEffect(() => {
    let cancelled = false;
    setError('');
    load()
      .then((listing) => {
        if (cancelled) return;
        if (!listing?.ok) {
          setError(listing?.error || 'Could not list its tools.');
          return;
        }
        setTools(listing.tools || []);
        setOffered(new Set((listing.tools || []).filter((t) => t.offered).map((t) => t.name)));
      })
      .catch((err) => !cancelled && setError(err?.message || 'Could not list its tools.'));
    return () => {
      cancelled = true;
    };
  }, [load]);

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!tools) return [];
    return q ? tools.filter((t) => t.name.toLowerCase().includes(q) || (t.description || '').toLowerCase().includes(q)) : tools;
  }, [tools, query]);
  const groups = useMemo(() => groupTools(shown), [shown]);

  const apply = (nextOffered, nextNewOn = newOn) => {
    setOffered(nextOffered);
    setNewOn(nextNewOn);
    onChange(filterFor(tools, nextOffered, nextNewOn));
  };
  const setMany = (names, on) => {
    const next = new Set(offered);
    names.forEach((name) => (on ? next.add(name) : next.delete(name)));
    apply(next);
  };

  if (error) return <p className="text-[11px] text-rose-400">Tools: {error} Save the server once it connects to choose its tools.</p>;
  if (!tools) return <p className="text-[11px] text-zinc-500">Listing its tools…</p>;

  const buttonClass = 'px-2 py-1 rounded-lg text-[11px] text-zinc-300 hover:text-white bg-[#1f1f23] hover:bg-[#27272b] disabled:opacity-40 transition';
  return (
    <div className="space-y-2 rounded-xl border border-[#27272a] p-2.5 bg-[#111113]">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-[11px] text-zinc-300">
          Tools offered to the bots: <span className="text-zinc-100">{offered.size}</span> of {tools.length}
        </span>
        <label className="ml-auto flex items-center gap-1.5 text-[11px] text-zinc-400" title="Tools the server adds in a later version">
          <input type="checkbox" checked={newOn} onChange={(e) => apply(offered, e.target.checked)} className="accent-cyan-400" />
          Offer tools it adds later
        </label>
      </div>
      <div className="flex items-center gap-2">
        <div className="relative flex-1">
          <FiSearch className="absolute left-2.5 top-1/2 -translate-y-1/2 text-[11px] text-zinc-500" />
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={`Search ${tools.length} tools`}
            className="w-full bg-[#1b1b1f] border border-[#2e2e34] rounded-lg pl-7 pr-2 py-1.5 text-[11px] text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-zinc-400"
          />
        </div>
        <button type="button" disabled={!shown.length} onClick={() => setMany(shown.map((t) => t.name), true)} className={buttonClass}>
          Offer {query ? 'shown' : 'all'}
        </button>
        <button type="button" disabled={!shown.length} onClick={() => setMany(shown.map((t) => t.name), false)} className={buttonClass}>
          Offer none{query ? ' shown' : ''}
        </button>
      </div>
      <div className="max-h-80 overflow-y-auto space-y-1 pr-1">
        {groups.map(([prefix, members]) => {
          const on = members.filter((t) => offered.has(t.name)).length;
          const expanded = open[prefix] || !!query || groups.length === 1;
          return (
            <div key={prefix || '(other)'} className="rounded-lg border border-[#202024]">
              <div className="flex items-center gap-2 px-2 py-1.5">
                <input
                  type="checkbox"
                  checked={on === members.length}
                  ref={(el) => el && (el.indeterminate = on > 0 && on < members.length)}
                  onChange={(e) => setMany(members.map((t) => t.name), e.target.checked)}
                  className="accent-cyan-400"
                  aria-label={`Offer every ${prefix || 'other'} tool`}
                />
                <button type="button" onClick={() => setOpen((prev) => ({ ...prev, [prefix]: !expanded }))} className="flex items-center gap-1.5 min-w-0 flex-1 text-left">
                  <FiChevronRight className={`text-[11px] text-zinc-500 transition-transform ${expanded ? 'rotate-90' : ''}`} />
                  <span className="text-[11px] text-zinc-200 font-mono truncate">{prefix ? `${prefix}_*` : 'Other tools'}</span>
                  <span className="text-[10px] text-zinc-500 flex-shrink-0">{on} of {members.length}</span>
                </button>
              </div>
              {expanded && (
                <div className="border-t border-[#1c1c20] py-0.5">
                  {members.map((tool) => (
                    <label key={tool.name} className="flex items-start gap-2 px-2 py-1 pl-6 hover:bg-[#16161a] cursor-pointer">
                      <input type="checkbox" checked={offered.has(tool.name)} onChange={(e) => setMany([tool.name], e.target.checked)} className="mt-0.5 accent-cyan-400" />
                      <span className="min-w-0 flex-1">
                        <span className="flex items-center gap-1.5">
                          <span className="text-[11px] text-zinc-100 font-mono truncate">{tool.name}</span>
                          {tool.read_only && <span className="text-[9px] uppercase tracking-wide text-zinc-500 border border-zinc-700 rounded px-1">read</span>}
                        </span>
                        {tool.description && <span className="block text-[10px] text-zinc-500 leading-snug line-clamp-1">{tool.description}</span>}
                      </span>
                    </label>
                  ))}
                </div>
              )}
            </div>
          );
        })}
        {!shown.length && <p className="text-[11px] text-zinc-500 px-1">No tool matches.</p>}
      </div>
    </div>
  );
}
