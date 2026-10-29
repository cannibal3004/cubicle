'use client';

import React, { useEffect, useState } from 'react';
import { FiX, FiFolder, FiPlus, FiArchive, FiRotateCcw, FiTrash2, FiEdit2, FiMessageSquare, FiHardDrive } from 'react-icons/fi';
import { fetchProjectLog } from '../lib/api';
import { conversationTitle } from './Sidebar';
import ConfirmDialog from './ConfirmDialog';

const LOG_PAGE = 100;
const LOG_MAX = 1000;

function formatWhen(iso, withTime = true) {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleString('en-US', { month: 'short', day: 'numeric', ...(withTime ? { hour: 'numeric', minute: '2-digit' } : { year: 'numeric' }) });
}

function Card({ title, right, children }) {
  return (
    <div className="bg-[#18181b] border border-[#27272a] rounded-2xl p-5 space-y-3 shadow-sm">
      <div className="flex items-center justify-between gap-3">
        <h3 className="text-sm font-bold text-zinc-100">{title}</h3>
        {right}
      </div>
      {children}
    </div>
  );
}

// One project in one place: where it stands (its work notes), what happened (its log), the
// conversations in it, and its folder in the bot's computer.
export default function ProjectPage({
  project,
  bot,
  conversations = [],
  turnStates,
  onClose,
  onOpenConversation,
  onNewConversation,
  onRename,
  onArchive,
  onDelete,
  onSaveNotes,
  onOpenFolder,
}) {
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState(project.name);
  const [editingNotes, setEditingNotes] = useState(false);
  const [notes, setNotes] = useState(project.notes || '');
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [limit, setLimit] = useState(LOG_PAGE);
  const [log, setLog] = useState(null);

  useEffect(() => {
    if (!renaming) setName(project.name);
  }, [project.name, renaming]);
  useEffect(() => {
    if (!editingNotes) setNotes(project.notes || '');
  }, [project.notes, editingNotes]);
  // The log, newest first; fetched again when the bot adds to it.
  useEffect(() => {
    let cancelled = false;
    fetchProjectLog(project.id, limit)
      .then((data) => !cancelled && setLog(data.entries || []))
      .catch((err) => !cancelled && setLog({ error: err.message }));
    return () => {
      cancelled = true;
    };
  }, [project.id, project.log_entries, project.log_updated_at, limit]);

  const members = conversations.filter((c) => c.project_id === project.id);
  const byId = Object.fromEntries(conversations.map((c) => [c.id, c]));
  const status = (project.notes || '').split('\n').map((line) => line.trim()).find(Boolean) || '';

  const act = async (what, fn) => {
    setBusy(what);
    setError('');
    try {
      return await fn();
    } catch (err) {
      setError(err?.message || 'That did not work.');
      return null;
    } finally {
      setBusy('');
    }
  };

  const commitName = async () => {
    const next = name.trim();
    setRenaming(false);
    if (next && next !== project.name) await act('rename', () => onRename(next));
    else setName(project.name);
  };

  const buttonClass =
    'flex items-center gap-1.5 px-2.5 py-1.5 text-xs rounded-lg text-zinc-300 hover:text-white hover:bg-[#1f1f23] disabled:opacity-50 transition';

  return (
    <section className="absolute inset-0 z-30 bg-[#0d0d10] flex flex-col animate-fade-in font-sans text-zinc-100">
      <div className="px-6 py-3.5 border-b border-[#1c1c20] flex items-center justify-between gap-3 bg-[#09090b]/80 backdrop-blur-md">
        <div className="flex items-center gap-3 min-w-0">
          <div className="w-9 h-9 rounded-xl flex items-center justify-center border border-amber-500/30 bg-amber-500/10 flex-shrink-0">
            <FiFolder className={project.archived ? 'text-zinc-500' : 'text-amber-300'} />
          </div>
          <div className="min-w-0">
            <div className="flex items-center gap-2 min-w-0">
              {renaming ? (
                <input
                  autoFocus
                  value={name}
                  maxLength={80}
                  onChange={(e) => setName(e.target.value)}
                  onBlur={commitName}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') commitName();
                    if (e.key === 'Escape') {
                      setRenaming(false);
                      setName(project.name);
                    }
                  }}
                  className="bg-[#141417] border border-[#34343a] rounded px-1.5 py-0.5 text-sm font-bold text-zinc-100 focus:outline-none"
                  aria-label="Project name"
                />
              ) : (
                <button type="button" onClick={() => setRenaming(true)} className="group flex items-center gap-1.5 min-w-0" title="Rename">
                  <h2 className="text-sm font-bold text-zinc-100 tracking-wide truncate">{project.name}</h2>
                  <FiEdit2 className="text-[10px] text-zinc-600 group-hover:text-zinc-300 flex-shrink-0" />
                </button>
              )}
              {project.archived && <span className="text-[9px] uppercase tracking-wider text-zinc-500 border border-[#2e2e34] rounded px-1.5 py-0.5">archived</span>}
            </div>
            <p className="text-[11px] text-zinc-500 truncate">
              {bot?.name ? `${bot.avatar || '🤖'} ${bot.name}'s project` : 'Project'}
              {status ? ` · ${status}` : ''}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-1 flex-shrink-0">
          <button type="button" onClick={onNewConversation} className={buttonClass} title="Start a conversation in this project">
            <FiPlus /> New conversation
          </button>
          <button type="button" disabled={busy === 'folder'} onClick={() => act('folder', onOpenFolder)} className={buttonClass} title={`/workspace/${project.folder}`}>
            <FiHardDrive /> Files
          </button>
          <button
            type="button"
            disabled={Boolean(busy)}
            onClick={() => act('archive', () => onArchive(!project.archived))}
            className={buttonClass}
            title={project.archived ? 'Bring the project back' : 'Hide the project and its conversations from the list; nothing is deleted'}
          >
            {project.archived ? <FiRotateCcw /> : <FiArchive />} {project.archived ? 'Unarchive' : 'Archive'}
          </button>
          <button type="button" disabled={Boolean(busy)} onClick={() => setConfirmingDelete(true)} className={`${buttonClass} text-rose-400 hover:text-rose-300 hover:bg-rose-500/10`}>
            <FiTrash2 /> Delete
          </button>
          <button type="button" onClick={onClose} className="ml-2 p-1 rounded-lg text-zinc-400 hover:text-white hover:bg-[#1c1c20] transition" title="Close the project">
            <FiX className="text-base" />
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto p-6">
        {error && <p className="max-w-6xl mb-4 text-[11px] text-red-300 bg-red-500/10 border border-red-500/20 rounded-xl px-3 py-2">{error}</p>}
        <div className="max-w-6xl grid grid-cols-1 lg:grid-cols-3 gap-6 items-start">
          <div className="lg:col-span-2 space-y-6 min-w-0">
            <Card
              title="Work notes"
              right={
                !editingNotes && (
                  <button type="button" onClick={() => setEditingNotes(true)} className="text-[11px] text-zinc-400 hover:text-white">
                    Edit
                  </button>
                )
              }
            >
              <p className="text-[11px] text-zinc-500 -mt-1">
                Where the work stands: status, decisions, next steps. The bot keeps them; your edits are shown to it in the project&apos;s conversations.
              </p>
              {editingNotes ? (
                <>
                  <textarea
                    autoFocus
                    value={notes}
                    onChange={(e) => setNotes(e.target.value)}
                    maxLength={project.notes_limit || 8000}
                    rows={14}
                    className="w-full bg-[#222226] border border-[#2e2e34] rounded-xl px-3.5 py-2.5 font-mono text-xs text-zinc-100 leading-relaxed focus:outline-none focus:border-zinc-400 resize-y"
                    placeholder="Where the work stands: status, decisions, next steps."
                    aria-label="Work notes"
                  />
                  <div className="flex items-center gap-2">
                    <span className="text-[11px] text-zinc-600 font-mono">
                      {notes.length.toLocaleString()}/{(project.notes_limit || 8000).toLocaleString()}
                    </span>
                    <button type="button" onClick={() => setEditingNotes(false)} disabled={busy === 'notes'} className="ml-auto px-3 py-1.5 text-xs rounded-lg text-zinc-300 hover:text-white hover:bg-[#1f1f23]">
                      Cancel
                    </button>
                    <button
                      type="button"
                      disabled={busy === 'notes'}
                      onClick={async () => {
                        const saved = await act('notes', async () => {
                          await onSaveNotes(notes.trim());
                          return true;
                        });
                        if (saved) setEditingNotes(false);
                      }}
                      className="px-4 py-1.5 text-xs rounded-lg bg-white text-black font-semibold hover:bg-zinc-200 disabled:opacity-50"
                    >
                      {busy === 'notes' ? 'Saving…' : 'Save'}
                    </button>
                  </div>
                </>
              ) : project.notes ? (
                <div className="whitespace-pre-wrap break-words text-xs leading-relaxed text-zinc-200">{project.notes}</div>
              ) : (
                <p className="text-xs text-zinc-600">No work notes yet. The bot writes where the project stands here.</p>
              )}
              {project.notes_updated_at && !editingNotes && <p className="text-[10px] text-zinc-600">Updated {formatWhen(project.notes_updated_at)}</p>}
            </Card>

            <Card title={`Log · ${project.log_entries || 0} entr${project.log_entries === 1 ? 'y' : 'ies'}`}>
              <p className="text-[11px] text-zinc-500 -mt-1">What the bot did and found along the way, newest first. It searches this when a message touches on the past.</p>
              {log === null && <p className="text-xs text-zinc-600">Loading…</p>}
              {log && log.error && <p className="text-xs text-red-300">{log.error}</p>}
              {Array.isArray(log) && !log.length && <p className="text-xs text-zinc-600">Nothing logged yet. The bot adds what it did and found as it works.</p>}
              {Array.isArray(log) && log.length > 0 && (
                <div className="space-y-3">
                  {log.map((entry) => {
                    const from = entry.thread_id && byId[entry.thread_id];
                    return (
                      <div key={entry.id} className="border-l-2 border-[#2e2e34] pl-3">
                        <div className="flex items-center gap-2 text-[10px] font-mono text-zinc-500">
                          <span>{formatWhen(entry.created_at)}</span>
                          {from && (
                            <button type="button" onClick={() => onOpenConversation(from)} className="truncate text-zinc-500 hover:text-zinc-200" title="Open the conversation it was logged from">
                              · {conversationTitle(from)}
                            </button>
                          )}
                        </div>
                        <div className="mt-0.5 whitespace-pre-wrap break-words text-xs leading-relaxed text-zinc-200">{entry.text}</div>
                      </div>
                    );
                  })}
                  {log.length >= limit && limit < LOG_MAX && (
                    <button type="button" onClick={() => setLimit((n) => Math.min(LOG_MAX, n + LOG_PAGE))} className="text-[11px] text-zinc-400 hover:text-white">
                      Show older entries
                    </button>
                  )}
                </div>
              )}
            </Card>
          </div>

          <div className="space-y-6 min-w-0">
            <Card
              title={`Conversations · ${members.length}`}
              right={
                <button type="button" onClick={onNewConversation} className="text-[11px] text-zinc-400 hover:text-white flex items-center gap-1">
                  <FiPlus /> New
                </button>
              }
            >
              {members.length ? (
                <div className="space-y-1 -mx-2">
                  {members.map((conversation) => {
                    const turn = turnStates?.[conversation.id];
                    const waiting = Boolean(turn?.pending_approval);
                    const working = Boolean(turn && turn.status === 'running' && !waiting);
                    return (
                      <button
                        key={conversation.id}
                        type="button"
                        onClick={() => onOpenConversation(conversation)}
                        className="w-full flex items-center gap-2 px-2 py-1.5 rounded-lg text-left text-xs text-zinc-300 hover:text-white hover:bg-[#222226] transition"
                      >
                        {waiting ? (
                          <span className="w-1.5 h-1.5 rounded-full bg-amber-400 flex-shrink-0 animate-pulse" title="Waiting for you" />
                        ) : working ? (
                          <span className="w-1.5 h-1.5 rounded-full bg-blue-400 flex-shrink-0" title="Working on a reply" />
                        ) : (
                          <FiMessageSquare className="text-[11px] text-zinc-600 flex-shrink-0" />
                        )}
                        <span className={`flex-1 min-w-0 truncate ${conversation.archived ? 'opacity-60 italic' : ''}`}>{conversationTitle(conversation)}</span>
                        <span className="text-[10px] text-zinc-600 flex-shrink-0">{formatWhen(conversation.last_activity, false)}</span>
                      </button>
                    );
                  })}
                </div>
              ) : (
                <p className="text-xs text-zinc-600">None yet. Start one here, or move one in from the sidebar.</p>
              )}
            </Card>

            <Card title="Folder">
              <p className="text-[11px] text-zinc-500 -mt-1">The project&apos;s files in {bot?.name || 'the bot'}&apos;s computer. The bot is told to keep them here.</p>
              <div className="font-mono text-[11px] text-zinc-300 bg-[#141417] border border-[#2e2e34] rounded-lg px-3 py-2 break-all">/workspace/{project.folder}</div>
              <button
                type="button"
                disabled={busy === 'folder'}
                onClick={() => act('folder', onOpenFolder)}
                className="flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-lg border border-[#2e2e34] text-zinc-200 hover:bg-[#222226] disabled:opacity-50"
              >
                <FiHardDrive /> {busy === 'folder' ? 'Opening…' : 'Open in the file viewer'}
              </button>
            </Card>

            <p className="text-[10px] text-zinc-600 px-1">Started {formatWhen(project.created_at, false)}</p>
          </div>
        </div>
      </div>

      {confirmingDelete && (
        <ConfirmDialog
          title={`Delete the project "${project.name}"?`}
          body="Its work notes and log are deleted. Its conversations stay, outside any project, and so do the files in its folder. This cannot be undone."
          busy={busy === 'delete'}
          onCancel={() => setConfirmingDelete(false)}
          onConfirm={async () => {
            const done = await act('delete', async () => {
              await onDelete();
              return true;
            });
            if (!done) setConfirmingDelete(false);
          }}
        />
      )}
    </section>
  );
}
