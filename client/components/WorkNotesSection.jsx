'use client';

import React, { useEffect, useState } from 'react';
import { FiFileText, FiFolder, FiChevronRight } from 'react-icons/fi';
import { fetchProjectLog } from '../lib/api';

function formatWhen(iso) {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}

// Where the work stands: the work notes of the conversation's project, or the
// conversation's own when it is in none, and the project's log. The bot keeps
// them with its work notes and project log tools; the user can edit the notes
// here (the edit is shown to the bot where it happened).
export default function WorkNotesSection({ project, conversation, onSave }) {
  const notes = project ? project.notes || '' : conversation?.work_notes || '';
  const limit = project?.notes_limit || 8000;
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(notes);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [logOpen, setLogOpen] = useState(false);
  const [log, setLog] = useState(null);

  const key = project ? project.id : conversation?.id;
  useEffect(() => {
    setEditing(false);
    setError(null);
    setLogOpen(false);
    setLog(null);
  }, [key]);
  useEffect(() => {
    if (!editing) setDraft(notes);
  }, [notes, editing]);
  // The log is fetched when opened, and again when the bot adds to it.
  useEffect(() => {
    if (!logOpen || !project) return undefined;
    let cancelled = false;
    fetchProjectLog(project.id)
      .then((data) => { if (!cancelled) setLog(data.entries || []); })
      .catch((err) => { if (!cancelled) setLog({ error: err.message }); });
    return () => { cancelled = true; };
  }, [logOpen, project?.id, project?.log_updated_at, project?.log_entries]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!conversation && !project) return null;

  const save = async () => {
    if (!onSave) return;
    setSaving(true);
    setError(null);
    try {
      await onSave(draft.trim());
      setEditing(false);
    } catch (err) {
      setError(err?.message || 'Could not save the work notes.');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="border-t border-[#1f1f23] text-[11px] text-zinc-300">
      <div className="flex items-center gap-2 px-3 py-2 font-mono text-zinc-400">
        {project ? <FiFolder className="text-xs text-amber-300/80" /> : <FiFileText className="text-xs text-amber-300/80" />}
        <span className="text-zinc-200">Work notes</span>
        <span className="truncate">· {project ? project.name : 'this conversation'}</span>
        {!editing && onSave && (
          <button type="button" onClick={() => setEditing(true)} className="ml-auto text-zinc-500 hover:text-zinc-200">
            edit
          </button>
        )}
      </div>
      <div className="px-3 pb-2.5">
        {editing ? (
          <>
            <textarea
              autoFocus
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              maxLength={limit}
              rows={10}
              className="w-full bg-[#141417] border border-[#2c2c31] rounded-lg px-2 py-1.5 font-mono text-[11px] text-zinc-100 leading-relaxed outline-none focus:border-amber-500/60 resize-y"
              placeholder="Where the work stands: status, decisions, next steps."
            />
            <div className="flex items-center gap-2 mt-1">
              <span className="text-zinc-600 font-mono">{draft.length}/{limit}</span>
              <button type="button" onClick={() => setEditing(false)} disabled={saving} className="ml-auto text-zinc-500 hover:text-zinc-200">
                cancel
              </button>
              <button type="button" onClick={save} disabled={saving} className="text-amber-300 hover:text-white">
                {saving ? 'saving…' : 'save'}
              </button>
            </div>
          </>
        ) : notes ? (
          <div className="whitespace-pre-wrap break-words leading-relaxed text-zinc-300 max-h-64 overflow-y-auto">{notes}</div>
        ) : (
          <p className="text-zinc-600">
            {project ? 'No work notes yet. The bot writes where the project stands here.' : 'None yet. The bot keeps where this conversation\'s work stands here.'}
          </p>
        )}
        {error && <div className="text-red-300 mt-1">{error}</div>}
      </div>
      {project && (
        <div className="border-t border-[#1f1f23]">
          <button
            type="button"
            onClick={() => setLogOpen((v) => !v)}
            className="w-full flex items-center gap-2 px-3 py-2 font-mono text-zinc-400 hover:text-zinc-200"
            aria-expanded={logOpen}
          >
            <FiChevronRight className={`text-xs transition-transform ${logOpen ? 'rotate-90' : ''}`} />
            <span className="text-zinc-200">Log</span>
            <span>· {project.log_entries || 0} entr{project.log_entries === 1 ? 'y' : 'ies'}</span>
          </button>
          {logOpen && (
            <div className="px-3 pb-3 space-y-2 max-h-80 overflow-y-auto">
              {log === null && <p className="text-zinc-600">Loading…</p>}
              {log && log.error && <p className="text-red-300">{log.error}</p>}
              {Array.isArray(log) && !log.length && <p className="text-zinc-600">Nothing logged yet. The bot adds what it did and found as it works.</p>}
              {Array.isArray(log) &&
                log.map((entry) => (
                  <div key={entry.id} className="border-l border-[#2c2c31] pl-2">
                    <div className="text-[10px] font-mono text-zinc-500">{formatWhen(entry.created_at)}</div>
                    <div className="whitespace-pre-wrap break-words leading-relaxed text-zinc-300">{entry.text}</div>
                  </div>
                ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
