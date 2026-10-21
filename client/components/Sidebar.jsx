'use client';

import React, { useEffect, useRef, useState } from 'react';
import { searchEverything } from '../lib/api';
import {
  FiSearch,
  FiPlus,
  FiSettings,
  FiActivity,
  FiMoreHorizontal,
  FiEdit2,
  FiArchive,
  FiRotateCcw,
  FiTrash2,
  FiChevronRight,
} from 'react-icons/fi';

function BotAvatar({ bot, size = 'md' }) {
  const sizeClass = size === 'sm' ? 'w-7 h-7 text-sm' : 'w-10 h-10 text-xl';
  const accent = bot?.accent_color || '#3b82f6';
  return (
    <div
      className={`${sizeClass} rounded-xl flex items-center justify-center flex-shrink-0 border shadow-inner`}
      style={{ background: `${accent}22`, borderColor: `${accent}55` }}
      aria-hidden="true"
    >
      {bot?.avatar || '🤖'}
    </div>
  );
}

// Per-bot actions, shown on hover. Rendered inline so a click on it does
// not also select the bot.
function BotMenu({ bot, onEdit, onArchive, onDelete, onOpenChange }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);

  useEffect(() => {
    if (onOpenChange) onOpenChange(open);
  }, [open, onOpenChange]);

  useEffect(() => {
    if (!open) return undefined;
    const close = (event) => {
      if (ref.current && !ref.current.contains(event.target)) setOpen(false);
    };
    document.addEventListener('mousedown', close);
    return () => document.removeEventListener('mousedown', close);
  }, [open]);

  const item = 'w-full flex items-center gap-2 px-2.5 py-1.5 text-xs rounded-md text-left transition';

  return (
    <div ref={ref} className="relative flex-shrink-0" onClick={(event) => event.stopPropagation()}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={`p-1 rounded-md text-zinc-500 hover:text-white hover:bg-[#2a2a2f] transition ${open ? 'opacity-100' : 'opacity-0 group-hover:opacity-100'}`}
        title="Bot options"
        aria-label={`Options for ${bot.name}`}
      >
        <FiMoreHorizontal />
      </button>
      {open && (
        <div className="absolute right-0 top-7 z-30 w-40 bg-[#1a1a1e] border border-[#2c2c31] rounded-lg shadow-xl p-1">
          <button type="button" className={`${item} text-zinc-200 hover:bg-[#26262b]`} onClick={() => { setOpen(false); onEdit(bot); }}>
            <FiEdit2 /> Edit
          </button>
          <button type="button" className={`${item} text-zinc-200 hover:bg-[#26262b]`} onClick={() => { setOpen(false); onArchive(bot, !bot.archived); }}>
            {bot.archived ? <FiRotateCcw /> : <FiArchive />} {bot.archived ? 'Unarchive' : 'Archive'}
          </button>
          <button type="button" className={`${item} text-rose-400 hover:bg-rose-500/10`} onClick={() => { setOpen(false); onDelete(bot); }}>
            <FiTrash2 /> Delete…
          </button>
        </div>
      )}
    </div>
  );
}

function formatTime(iso) {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true });
}

export default function Sidebar({
  bots,
  activeBotId,
  userName,
  onSelectBot,
  activeTab,
  onSelectTab,
  onOpenSettings,
  onOpenNewBot,
  onEditBot,
  onArchiveBot,
  onDeleteBot,
  turnStates,
}) {
  const [searchTerm, setSearchTerm] = useState('');
  // Results from inside the conversations, fetched a moment after typing stops.
  const [found, setFound] = useState(null); // null, or { query, results }
  const [searching, setSearching] = useState(false);
  useEffect(() => {
    const query = searchTerm.trim();
    if (query.length < 3) {
      setFound(null);
      return undefined;
    }
    let disposed = false;
    const timer = window.setTimeout(async () => {
      setSearching(true);
      try {
        const data = await searchEverything(query);
        if (!disposed) setFound({ query, results: data.results || [] });
      } catch (err) {
        if (!disposed) setFound({ query, results: [], error: err?.message });
      } finally {
        if (!disposed) setSearching(false);
      }
    }, 450);
    return () => {
      disposed = true;
      window.clearTimeout(timer);
    };
  }, [searchTerm]);
  const [showArchived, setShowArchived] = useState(false);
  const displayName = userName || 'You';

  const term = searchTerm.trim().toLowerCase();
  const matches = (bot) =>
    !term ||
    bot.name.toLowerCase().includes(term) ||
    (bot.role || '').toLowerCase().includes(term) ||
    (bot.description || '').toLowerCase().includes(term);

  const all = bots || [];
  const [openMenuFor, setOpenMenuFor] = useState(null);
  const active = all.filter((bot) => !bot.archived && matches(bot));
  const archived = all.filter((bot) => bot.archived && matches(bot));
  const archivedOpen = showArchived || (term && archived.length > 0);

  const renderBot = (bot) => {
    const isActive = activeBotId === bot.id;
    const turn = turnStates?.[bot.id];
    const needsApproval = Boolean(turn?.pending_approval);
    const isWorking = Boolean(turn && turn.status === 'running' && !needsApproval);
    const menuOpen = openMenuFor === bot.id;

    // Archived cards are dimmed through their children, not the card: an
    // opacity on the card makes it a stacking context, and the menu inside
    // it would then paint beneath the next card. The card with an open
    // menu is lifted above its neighbours for the same reason.
    return (
      <div
        key={bot.id}
        onClick={() => onSelectBot(bot.id)}
        className={`group relative p-2.5 rounded-xl cursor-pointer transition-all duration-150 flex items-start gap-3 ${
          isActive
            ? 'bg-[#27272a] text-white shadow-sm border border-[#34343a]'
            : 'hover:bg-[#1c1c20] text-zinc-400 border border-transparent'
        } ${menuOpen ? 'z-40' : ''}`}
      >
        <div className={bot.archived ? 'opacity-60' : ''}>
          <BotAvatar bot={bot} />
        </div>

        <div className="flex-1 min-w-0 pt-0.5">
          <div className="flex items-center justify-between gap-1">
            <h3 className={`text-xs font-semibold truncate flex items-center gap-1.5 min-w-0 ${isActive ? 'text-white' : 'text-zinc-200'}`}>
              <span className={`truncate ${bot.archived ? 'opacity-70' : ''}`}>{bot.name}</span>
              {needsApproval && (
                <span
                  className="w-2 h-2 rounded-full bg-amber-400 flex-shrink-0 animate-pulse"
                  title={`${turn.pending_approval.kind === 'question' ? 'Waiting for your answer' : 'Waiting for your approval'}: ${turn.pending_approval.summary || ''}`}
                />
              )}
              {isWorking && <span className="w-2 h-2 rounded-full bg-blue-400 flex-shrink-0" title="Working on a reply" />}
            </h3>
            <div className="flex items-center gap-1 flex-shrink-0">
              {!bot.archived && (
                <span className="text-[10px] text-zinc-500 font-normal group-hover:hidden">{formatTime(bot.created_at)}</span>
              )}
              <BotMenu
                bot={bot}
                onEdit={onEditBot}
                onArchive={onArchiveBot}
                onDelete={onDeleteBot}
                onOpenChange={(isOpen) => setOpenMenuFor((current) => (isOpen ? bot.id : current === bot.id ? null : current))}
              />
            </div>
          </div>

          <p className={`text-[11px] truncate mt-0.5 text-zinc-400 group-hover:text-zinc-300 ${bot.archived ? 'opacity-70' : ''}`}>
            {bot.role || bot.description || 'General Intelligence'}
          </p>
        </div>
      </div>
    );
  };

  return (
    <aside className="w-72 h-screen dark-sidebar flex flex-col justify-between select-none flex-shrink-0 text-zinc-300 font-sans">
      {/* Top Header & Search Area */}
      <div className="p-3.5 space-y-3">
        <div className="flex items-center justify-between pt-1 px-1">
          <div className="flex items-center gap-2 px-1">
            <span className="text-base leading-none">🧊</span>
            <span className="text-sm font-semibold tracking-tight text-zinc-100">Cubicle</span>
          </div>

          <button
            suppressHydrationWarning={true}
            onClick={onOpenNewBot}
            title="Create New Bot"
            className="text-zinc-400 hover:text-white transition p-1 rounded-md hover:bg-[#222226]"
          >
            <FiPlus className="text-lg" />
          </button>
        </div>

        <div className="relative">
          <FiSearch className="absolute left-3 top-2.5 text-zinc-500 text-xs" />
          <input
            suppressHydrationWarning={true}
            type="text"
            placeholder="Search"
            value={searchTerm}
            onChange={(e) => setSearchTerm(e.target.value)}
            className="w-full bg-[#222225] border border-[#2c2c30] rounded-xl pl-8 pr-3 py-1.5 text-xs text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-zinc-500 transition"
          />
        </div>
      </div>

      {/* Bot Roster List */}
      <div className="flex-1 overflow-y-auto px-2 space-y-1.5">
        {active.map(renderBot)}

        {all.length === 0 && (
          <div className="px-3 py-8 text-center">
            <p className="text-xs text-zinc-400">No bots yet.</p>
            <button
              type="button"
              onClick={onOpenNewBot}
              className="mt-3 inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-white text-black text-xs font-semibold hover:bg-zinc-200 transition"
            >
              <FiPlus /> Create your first bot
            </button>
          </div>
        )}
        {all.length > 0 && active.length === 0 && archived.length === 0 && !(found && found.results.length) && (
          <p className="px-3 py-6 text-center text-xs text-zinc-500">{searching ? 'Searching…' : `Nothing matches “${searchTerm}”.`}</p>
        )}

        {term.length >= 3 && (found || searching) && (
          <div className="pt-2">
            <div className="px-3 pb-1 text-[10px] uppercase tracking-wide text-zinc-500 flex items-center justify-between">
              <span>In conversations</span>
              {searching && <span className="normal-case text-zinc-600">searching…</span>}
            </div>
            {found?.error && <p className="px-3 py-2 text-[11px] text-rose-300">{found.error}</p>}
            {found && !found.error && found.results.length === 0 && !searching && (
              <p className="px-3 py-2 text-[11px] text-zinc-600">Nothing similar in messages, files or notes.</p>
            )}
            {(found?.results || []).map((hit) => (
              <button
                key={`${hit.kind}-${hit.ref}-${hit.chunk}`}
                type="button"
                onClick={() => onSelectBot(hit.bot_id)}
                className="w-full text-left px-3 py-2 rounded-xl hover:bg-[#1e1e22] transition"
                title={hit.snippet}
              >
                <div className="flex items-center gap-1.5 text-[10px] text-zinc-500">
                  <span className="text-zinc-300 font-semibold truncate">{hit.bot_name || hit.bot_id}</span>
                  <span className="px-1 rounded border border-[#2c2c30] uppercase tracking-wide">{hit.kind === 'attachment' ? 'file' : hit.kind}</span>
                  {hit.created_at && <span className="ml-auto">{String(hit.created_at).slice(0, 10)}</span>}
                </div>
                <div className="text-[11px] text-zinc-300 leading-snug line-clamp-3 mt-0.5">
                  {hit.kind === 'attachment' && hit.name ? <span className="text-zinc-500">{hit.name}: </span> : null}
                  {hit.snippet}
                </div>
              </button>
            ))}
          </div>
        )}

        {archived.length > 0 && (
          <div className="pt-2">
            <button
              type="button"
              onClick={() => setShowArchived((v) => !v)}
              className="w-full flex items-center gap-1.5 px-2 py-1 text-[11px] uppercase tracking-wide text-zinc-500 hover:text-zinc-300 transition"
            >
              <FiChevronRight className={`transition-transform ${archivedOpen ? 'rotate-90' : ''}`} />
              Archived ({archived.length})
            </button>
            {archivedOpen && <div className="space-y-1.5 mt-1">{archived.map(renderBot)}</div>}
          </div>
        )}
      </div>

      {/* Bottom Sidebar Footer */}
      <div className="p-3 space-y-2 border-t border-[#1f1f23]">
        <button
          suppressHydrationWarning={true}
          onClick={() => onSelectTab && onSelectTab('marketplace')}
          className={`w-full flex items-center gap-2 px-2 py-1 rounded-lg text-xs font-medium transition ${
            activeTab === 'marketplace' ? 'text-white bg-[#1e1e22]' : 'text-zinc-300 hover:text-white hover:bg-[#1e1e22]'
          }`}
        >
          <span className="text-sm">🧩</span>
          <span>Plugins</span>
        </button>

        <button
          suppressHydrationWarning={true}
          onClick={() => onSelectTab && onSelectTab('audit')}
          className={`w-full flex items-center gap-2 px-2 py-1 rounded-lg text-xs font-medium transition ${
            activeTab === 'audit' ? 'text-white bg-[#1e1e22]' : 'text-zinc-300 hover:text-white hover:bg-[#1e1e22]'
          }`}
        >
          <FiActivity className="text-sm text-cyan-400" />
          <span>Audit trail</span>
        </button>

        <div className="flex items-center justify-between pt-1">
          <button
            suppressHydrationWarning={true}
            onClick={onOpenSettings}
            className="flex items-center gap-2 px-2 py-1 rounded-lg text-xs font-medium text-zinc-300 hover:text-white hover:bg-[#1e1e22] transition"
          >
            <div className="w-5 h-5 rounded-full bg-[#2a2a2e] flex items-center justify-center text-[10px] text-zinc-400 font-bold border border-[#333338]">
              {displayName.charAt(0).toUpperCase()}
            </div>
            <span>{displayName}</span>
          </button>

          <button
            suppressHydrationWarning={true}
            onClick={onOpenSettings}
            className={`p-2 rounded-lg transition ${
              activeTab === 'settings' ? 'text-white bg-[#1e1e22]' : 'text-zinc-400 hover:text-zinc-200 hover:bg-[#1e1e22]'
            }`}
            title="Settings"
          >
            <FiSettings className="text-sm" />
          </button>
        </div>
      </div>
    </aside>
  );
}
