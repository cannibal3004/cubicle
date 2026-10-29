'use client';

import React, { useEffect, useRef, useState } from 'react';
import { searchEverything } from '../lib/api';
import WaitingApprovals from './WaitingApprovals';
import {
  FiSearch,
  FiPlus,
  FiSettings,
  FiActivity,
  FiMoreHorizontal,
  FiEdit2,
  FiArchive,
  FiLogOut,
  FiRotateCcw,
  FiTrash2,
  FiChevronRight,
  FiFolder,
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
function BotMenu({ bot, onEdit, onArchive, onDelete, onLeave, onOpenChange }) {
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
            <FiSettings /> Settings
          </button>
          {bot.shared ? (
            <button type="button" className={`${item} text-rose-400 hover:bg-rose-500/10`} onClick={() => { setOpen(false); onLeave?.(bot); }}>
              <FiLogOut /> Leave…
            </button>
          ) : (
            <>
              <button type="button" className={`${item} text-zinc-200 hover:bg-[#26262b]`} onClick={() => { setOpen(false); onArchive(bot, !bot.archived); }}>
                {bot.archived ? <FiRotateCcw /> : <FiArchive />} {bot.archived ? 'Unarchive' : 'Archive'}
              </button>
              <button type="button" className={`${item} text-rose-400 hover:bg-rose-500/10`} onClick={() => { setOpen(false); onDelete(bot); }}>
                <FiTrash2 /> Delete…
              </button>
            </>
          )}
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

// Today: the time; otherwise the day.
function formatWhen(iso) {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  if (date.toDateString() === new Date().toDateString()) return formatTime(iso);
  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

// The turns of one bot, across its conversations: a waiting one first.
function botTurn(turnStates, botId) {
  const turns = Object.values(turnStates || {}).filter((turn) => (turn.bot_id || turn.thread_id) === botId);
  return turns.find((turn) => turn.pending_approval) || turns.find((turn) => turn.status === 'running') || null;
}

const SHOWN_CONVERSATIONS = 6;

function conversationTitle(conversation) {
  return conversation.title || (conversation.messages ? 'Untitled conversation' : 'New conversation');
}

// The active bot's conversations, under its card, grouped by project: projects
// first (in order of their latest activity), then conversations in none.
function ConversationList({
  bot,
  conversations,
  projects,
  activeThreadId,
  turnStates,
  onSelect,
  onNew,
  onRename,
  onArchive,
  onDelete,
  onMove,
  onNewProject,
  onRenameProject,
  onArchiveProject,
  onDeleteProject,
}) {
  const [showArchived, setShowArchived] = useState(false);
  const [showAllLoose, setShowAllLoose] = useState(false);
  const [editing, setEditing] = useState(null); // { kind: 'conversation' | 'project', id }
  const [draft, setDraft] = useState('');
  const [moving, setMoving] = useState(null); // conversation id
  const [creatingProject, setCreatingProject] = useState(false);
  const [collapsed, setCollapsed] = useState({});

  const listed = conversations && conversations.length ? conversations : [{ id: bot.id, bot_id: bot.id, title: '', messages: 0 }];
  const allProjects = projects || [];
  const projectIds = new Set(allProjects.map((p) => p.id));
  const visible = (c) => showArchived || !c.archived;
  const inProject = (projectId) => listed.filter((c) => c.project_id === projectId && visible(c));
  const loose = listed.filter((c) => (!c.project_id || !projectIds.has(c.project_id)) && visible(c));
  const looseShown = showAllLoose ? loose : loose.slice(0, SHOWN_CONVERSATIONS);
  // Projects in order of their latest conversation, then the rest by creation.
  const order = new Map();
  listed.forEach((c, index) => {
    if (c.project_id && !order.has(c.project_id)) order.set(c.project_id, index);
  });
  const shownProjects = allProjects
    .filter((p) => showArchived || !p.archived)
    .sort((a, b) => (order.has(a.id) ? order.get(a.id) : 1e6) - (order.has(b.id) ? order.get(b.id) : 1e6));
  const archivedCount = listed.filter((c) => c.archived).length + allProjects.filter((p) => p.archived).length;

  const startEditing = (kind, id, text) => {
    setDraft(text || '');
    setEditing({ kind, id });
  };
  const commit = (item) => {
    const text = draft.trim();
    const kind = editing?.kind;
    setEditing(null);
    if (kind === 'project') {
      if (text && text !== item.name) onRenameProject?.(item, text);
    } else if (text !== (item.title || '')) {
      onRename?.(item, text);
    }
  };
  const isEditing = (kind, id) => editing?.kind === kind && editing.id === id;

  const titleInput = (item, kind) => (
    <input
      autoFocus
      value={draft}
      maxLength={80}
      onChange={(event) => setDraft(event.target.value)}
      onBlur={() => commit(item)}
      onKeyDown={(event) => {
        if (event.key === 'Enter') commit(item);
        if (event.key === 'Escape') setEditing(null);
      }}
      className="flex-1 min-w-0 bg-[#141417] border border-[#34343a] rounded px-1 py-0.5 text-[11.5px] text-zinc-100 focus:outline-none"
      aria-label={kind === 'project' ? 'Project name' : 'Conversation title'}
    />
  );

  const row = (conversation) => {
    const active = conversation.id === activeThreadId;
    const turn = turnStates?.[conversation.id];
    const waiting = Boolean(turn?.pending_approval);
    const working = Boolean(turn && turn.status === 'running' && !waiting);
    const title = conversationTitle(conversation);
    const busy = isEditing('conversation', conversation.id) || moving === conversation.id;
    return (
      <div
        key={conversation.id}
        onClick={() => !busy && onSelect?.(bot.id, conversation.id)}
        className={`group/conv flex items-center gap-1.5 px-2 py-1 rounded-lg cursor-pointer text-[11.5px] transition ${
          active ? 'bg-[#232327] text-white' : 'text-zinc-400 hover:bg-[#1c1c20] hover:text-zinc-200'
        }`}
        title={title}
      >
        {waiting && <span className="w-1.5 h-1.5 rounded-full bg-amber-400 flex-shrink-0 animate-pulse" title="Waiting for you" />}
        {working && <span className="w-1.5 h-1.5 rounded-full bg-blue-400 flex-shrink-0" title="Working on a reply" />}
        {isEditing('conversation', conversation.id) ? (
          titleInput(conversation, 'conversation')
        ) : moving === conversation.id ? (
          <select
            autoFocus
            value={conversation.project_id || ''}
            onClick={(event) => event.stopPropagation()}
            onChange={(event) => {
              setMoving(null);
              if ((event.target.value || null) !== (conversation.project_id || null)) onMove?.(conversation, event.target.value);
            }}
            onBlur={() => setMoving(null)}
            className="flex-1 min-w-0 bg-[#141417] border border-[#34343a] rounded px-1 py-0.5 text-[11.5px] text-zinc-100 focus:outline-none"
            aria-label="Move to project"
          >
            <option value="">No project</option>
            {allProjects.filter((p) => !p.archived || p.id === conversation.project_id).map((p) => (
              <option key={p.id} value={p.id}>{p.name}</option>
            ))}
          </select>
        ) : (
          <span className={`flex-1 min-w-0 truncate ${conversation.archived ? 'opacity-60 italic' : ''}`}>{title}</span>
        )}
        {!busy && (
          <>
            <span className="flex-shrink-0 text-[10px] text-zinc-600 group-hover/conv:hidden">{formatWhen(conversation.last_activity)}</span>
            <span className="hidden group-hover/conv:flex items-center gap-0.5 flex-shrink-0" onClick={(event) => event.stopPropagation()}>
              <button type="button" title="Rename" onClick={() => startEditing('conversation', conversation.id, conversation.title)} className="p-0.5 text-zinc-500 hover:text-white">
                <FiEdit2 className="text-[10px]" />
              </button>
              {onMove && (
                <button type="button" title="Move to a project" onClick={() => setMoving(conversation.id)} className="p-0.5 text-zinc-500 hover:text-white">
                  <FiFolder className="text-[10px]" />
                </button>
              )}
              {conversation.id !== bot.id && (
                <button
                  type="button"
                  title={conversation.archived ? 'Unarchive' : 'Archive'}
                  onClick={() => onArchive?.(conversation, !conversation.archived)}
                  className="p-0.5 text-zinc-500 hover:text-white"
                >
                  {conversation.archived ? <FiRotateCcw className="text-[10px]" /> : <FiArchive className="text-[10px]" />}
                </button>
              )}
              <button
                type="button"
                title={conversation.id === bot.id ? 'Empty this conversation' : 'Delete'}
                onClick={() => {
                  const question =
                    conversation.id === bot.id
                      ? `Empty "${title}"? Its messages are deleted; the conversation stays, as the bot's first one. This cannot be undone.`
                      : `Delete "${title}"? Its messages are deleted. This cannot be undone.`;
                  if (window.confirm(question)) onDelete?.(conversation);
                }}
                className="p-0.5 text-zinc-500 hover:text-rose-300"
              >
                <FiTrash2 className="text-[10px]" />
              </button>
            </span>
          </>
        )}
      </div>
    );
  };

  return (
    <div className="ml-6 mt-1 mb-1.5 pl-2 border-l border-[#26262b] space-y-0.5" onClick={(event) => event.stopPropagation()}>
      <div className="flex items-center gap-1">
        <button
          type="button"
          onClick={() => onNew?.(bot.id)}
          className="flex-1 flex items-center gap-1.5 px-2 py-1 rounded-lg text-[11px] text-zinc-500 hover:text-zinc-200 hover:bg-[#1c1c20] transition"
        >
          <FiPlus className="text-[11px]" /> New conversation
        </button>
        {onNewProject && (
          <button
            type="button"
            onClick={() => {
              setDraft('');
              setCreatingProject(true);
            }}
            className="flex items-center gap-1 px-2 py-1 rounded-lg text-[11px] text-zinc-500 hover:text-zinc-200 hover:bg-[#1c1c20] transition"
            title="New project: a piece of work with its own work notes and log"
          >
            <FiFolder className="text-[11px]" /> Project
          </button>
        )}
      </div>
      {creatingProject && (
        <div className="flex items-center gap-1.5 px-2 py-1">
          <FiFolder className="text-[11px] text-amber-300/80 flex-shrink-0" />
          <input
            autoFocus
            value={draft}
            maxLength={80}
            placeholder="Project name"
            onChange={(event) => setDraft(event.target.value)}
            onBlur={() => setCreatingProject(false)}
            onKeyDown={(event) => {
              if (event.key === 'Escape') setCreatingProject(false);
              if (event.key === 'Enter' && draft.trim()) {
                onNewProject(bot.id, draft.trim());
                setCreatingProject(false);
              }
            }}
            className="flex-1 min-w-0 bg-[#141417] border border-[#34343a] rounded px-1 py-0.5 text-[11.5px] text-zinc-100 focus:outline-none"
          />
        </div>
      )}

      {shownProjects.map((project) => {
        const members = inProject(project.id);
        const open = !collapsed[project.id];
        const hasActive = members.some((c) => c.id === activeThreadId);
        return (
          <div key={project.id}>
            <div
              onClick={() => setCollapsed((prev) => ({ ...prev, [project.id]: open }))}
              className={`group/proj flex items-center gap-1.5 px-2 py-1 rounded-lg cursor-pointer text-[11.5px] transition ${
                hasActive && !open ? 'text-white' : 'text-zinc-300 hover:bg-[#1c1c20]'
              }`}
              title={project.notes ? project.notes.split('\n')[0] : project.name}
            >
              <FiChevronRight className={`text-[10px] text-zinc-500 flex-shrink-0 transition-transform ${open ? 'rotate-90' : ''}`} />
              <FiFolder className={`text-[11px] flex-shrink-0 ${project.archived ? 'text-zinc-600' : 'text-amber-300/80'}`} />
              {isEditing('project', project.id) ? (
                <span className="flex-1 min-w-0 flex" onClick={(event) => event.stopPropagation()}>{titleInput(project, 'project')}</span>
              ) : (
                <span className={`flex-1 min-w-0 truncate font-medium ${project.archived ? 'opacity-60 italic' : ''}`}>{project.name}</span>
              )}
              {!isEditing('project', project.id) && (
                <>
                  <span className="flex-shrink-0 text-[10px] text-zinc-600 group-hover/proj:hidden">{members.length || ''}</span>
                  <span className="hidden group-hover/proj:flex items-center gap-0.5 flex-shrink-0" onClick={(event) => event.stopPropagation()}>
                    <button type="button" title="New conversation in this project" onClick={() => onNew?.(bot.id, project.id)} className="p-0.5 text-zinc-500 hover:text-white">
                      <FiPlus className="text-[10px]" />
                    </button>
                    <button type="button" title="Rename" onClick={() => startEditing('project', project.id, project.name)} className="p-0.5 text-zinc-500 hover:text-white">
                      <FiEdit2 className="text-[10px]" />
                    </button>
                    <button
                      type="button"
                      title={project.archived ? 'Unarchive' : 'Archive'}
                      onClick={() => onArchiveProject?.(project, !project.archived)}
                      className="p-0.5 text-zinc-500 hover:text-white"
                    >
                      {project.archived ? <FiRotateCcw className="text-[10px]" /> : <FiArchive className="text-[10px]" />}
                    </button>
                    <button
                      type="button"
                      title="Delete the project"
                      onClick={() => {
                        if (window.confirm(`Delete the project "${project.name}" and its log? Its conversations stay, outside any project. This cannot be undone.`)) {
                          onDeleteProject?.(project);
                        }
                      }}
                      className="p-0.5 text-zinc-500 hover:text-rose-300"
                    >
                      <FiTrash2 className="text-[10px]" />
                    </button>
                  </span>
                </>
              )}
            </div>
            {open && (
              <div className="ml-3 pl-1.5 border-l border-[#232328] space-y-0.5">
                {members.map(row)}
                {!members.length && <p className="px-2 py-0.5 text-[10.5px] text-zinc-600">No conversations yet.</p>}
              </div>
            )}
          </div>
        );
      })}

      {looseShown.map(row)}
      {loose.length > SHOWN_CONVERSATIONS && (
        <button type="button" onClick={() => setShowAllLoose((v) => !v)} className="w-full text-left px-2 py-0.5 text-[10.5px] text-zinc-600 hover:text-zinc-300">
          {showAllLoose ? 'Show fewer' : `Show all (${loose.length - SHOWN_CONVERSATIONS} more)`}
        </button>
      )}
      {(archivedCount > 0 || showArchived) && (
        <button type="button" onClick={() => setShowArchived((v) => !v)} className="w-full text-left px-2 py-0.5 text-[10.5px] text-zinc-600 hover:text-zinc-300">
          {showArchived ? 'Hide archived' : `Show archived (${archivedCount})`}
        </button>
      )}
    </div>
  );
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
  onLeaveBot,
  turnStates,
  waitingApprovals,
  conversations,
  activeThreadId,
  onSelectConversation,
  onNewConversation,
  onRenameConversation,
  onArchiveConversation,
  onDeleteConversation,
  onMoveConversation,
  projects,
  onNewProject,
  onRenameProject,
  onArchiveProject,
  onDeleteProject,
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
  const active = all.filter((bot) => !bot.archived && !bot.shared && matches(bot));
  const archived = all.filter((bot) => bot.archived && !bot.shared && matches(bot));
  // Bots teammates shared with this person, under a heading of their own.
  const sharedWithMe = all.filter((bot) => bot.shared && matches(bot));
  const archivedOpen = showArchived || (term && archived.length > 0);

  const renderBot = (bot) => {
    const isActive = activeBotId === bot.id;
    const turn = botTurn(turnStates, bot.id);
    const needsApproval = Boolean(turn?.pending_approval);
    const isWorking = Boolean(turn && turn.status === 'running' && !needsApproval);
    const menuOpen = openMenuFor === bot.id;

    // Archived cards are dimmed through their children, not the card: an
    // opacity on the card makes it a stacking context, and the menu inside
    // it would then paint beneath the next card. The card with an open
    // menu is lifted above its neighbours for the same reason.
    return (
      <React.Fragment key={bot.id}>
      <div
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
                onLeave={onLeaveBot}
                onOpenChange={(isOpen) => setOpenMenuFor((current) => (isOpen ? bot.id : current === bot.id ? null : current))}
              />
            </div>
          </div>

          <p className={`text-[11px] truncate mt-0.5 text-zinc-400 group-hover:text-zinc-300 ${bot.archived ? 'opacity-70' : ''}`}>
            {bot.shared ? `${bot.shared.owner_name}'s · ` : ''}
            {bot.role || bot.description || 'General Intelligence'}
          </p>
        </div>
      </div>
      {isActive && !bot.archived && (
        <ConversationList
          bot={bot}
          conversations={conversations}
          activeThreadId={activeThreadId}
          turnStates={turnStates}
          onSelect={onSelectConversation}
          onNew={onNewConversation}
          onRename={onRenameConversation}
          onArchive={onArchiveConversation}
          onDelete={onDeleteConversation}
          onMove={onMoveConversation}
          projects={projects}
          onNewProject={onNewProject}
          onRenameProject={onRenameProject}
          onArchiveProject={onArchiveProject}
          onDeleteProject={onDeleteProject}
        />
      )}
      </React.Fragment>
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
        <WaitingApprovals waiting={waitingApprovals} />
        {active.map(renderBot)}

        {sharedWithMe.length > 0 && (
          <div className="pt-2">
            <div className="px-2 py-1 text-[11px] uppercase tracking-wide text-zinc-500" title="Teammates' bots you can use; your conversations with them are your own">
              Shared with me
            </div>
            <div className="space-y-1.5 mt-1">{sharedWithMe.map(renderBot)}</div>
          </div>
        )}

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
        {all.length > 0 && active.length === 0 && archived.length === 0 && sharedWithMe.length === 0 && !(found && found.results.length) && (
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
