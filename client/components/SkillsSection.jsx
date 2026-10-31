'use client';

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { FiBookOpen, FiCheck, FiDownload, FiEdit2, FiPlus, FiTrash2, FiUpload } from 'react-icons/fi';
import { createSkill, deleteSkill, fetchSkill, fetchSkills, installSkill, updateSkill, uploadSkill } from '../lib/api';

function sourceLabel(skill) {
  const source = skill.source || {};
  if (source.kind === 'link') return source.url ? `from ${source.url.replace(/^https?:\/\/(www\.)?/, '')}` : 'from a link';
  if (source.kind === 'upload') return source.name ? `uploaded ${source.name}` : 'uploaded';
  return 'written here';
}

// The skills library: instructions for kinds of tasks (a folder with a SKILL.md, and any scripts and
// files it needs), offered to every bot, which loads one when a task fits it. Installed from a link
// or an upload, or written here.
export default function SkillsSection({ admin = false }) {
  const [items, setItems] = useState([]);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [url, setUrl] = useState('');
  const [scope, setScope] = useState('user');
  // The form: null (closed), { mode: 'new' } or { mode: 'edit', id, files }.
  const [form, setForm] = useState(null);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [instructions, setInstructions] = useState('');
  const fileInput = useRef(null);

  const refresh = useCallback(async () => {
    try {
      setItems((await fetchSkills())?.skills || []);
    } catch (err) {
      setError(err?.message || 'Could not load the skills.');
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const run = async (action, failure) => {
    setBusy(true);
    setError('');
    setNotice('');
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

  const announce = (result) => {
    const names = (result?.installed || []).map((s) => s.name);
    if (names.length) setNotice(`Installed ${names.length === 1 ? names[0] : `${names.length} skills: ${names.join(', ')}`}.`);
  };

  const install = async () => {
    const result = await run(() => installSkill(url.trim(), admin ? scope : 'user'), 'Could not install the skill.');
    if (result) {
      setUrl('');
      announce(result);
    }
  };

  const upload = async (event) => {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file) return;
    announce(await run(() => uploadSkill(file, admin ? scope : 'user'), 'Could not install the upload.'));
  };

  const closeForm = () => {
    setForm(null);
    setName('');
    setDescription('');
    setInstructions('');
  };

  const startEdit = async (skill) => {
    const detail = await run(() => fetchSkill(skill.id), 'Could not open the skill.');
    if (!detail) return;
    setForm({ mode: 'edit', id: skill.id, files: (detail.skill.files || []).map((f) => f.path) });
    setName(detail.skill.name);
    setDescription(detail.skill.description || '');
    setInstructions(detail.instructions || '');
  };

  const submit = async () => {
    const fields = { name: name.trim(), description: description.trim(), instructions };
    const ok =
      form.mode === 'edit'
        ? await run(() => updateSkill(form.id, fields), 'Could not save the skill.')
        : await run(() => createSkill({ ...fields, scope: admin ? scope : 'user' }), 'Could not create the skill.');
    if (ok) closeForm();
  };

  const inputClass =
    'bg-[#222226] border border-[#2e2e34] rounded-xl px-3 py-2 text-xs text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-zinc-400 transition';
  const buttonClass = 'px-3 py-2 rounded-xl text-xs font-medium bg-[#1f1f23] text-zinc-200 hover:bg-[#27272b] disabled:opacity-40 transition flex items-center gap-1.5';
  const canManage = (skill) => skill.scope !== 'system' || admin;

  return (
    <div className="space-y-3">
      {items.length === 0 && <p className="text-[11px] text-zinc-500">No skills yet.</p>}
      {items.length > 0 && (
        <ul className="divide-y divide-[#232328] rounded-xl border border-[#27272a] overflow-hidden">
          {items.map((skill) => (
            <li key={skill.id} className="px-3 py-2 bg-[#18181b]">
              <div className="flex items-start gap-3">
                <FiBookOpen className="text-xs mt-0.5 flex-shrink-0 text-amber-300/80" />
                <div className="min-w-0 flex-1">
                  <div className="text-xs text-zinc-100 truncate">{skill.name}</div>
                  <div className="text-[11px] text-zinc-400 leading-snug line-clamp-2" title={skill.description}>
                    {skill.description}
                  </div>
                  <div className="mt-0.5 text-[10px] text-zinc-500 flex flex-wrap gap-x-3">
                    <span>{skill.scope === 'system' ? 'system · everyone' : 'yours'}</span>
                    {skill.files?.length > 0 && <span>{skill.files.length} file{skill.files.length === 1 ? '' : 's'}</span>}
                    <span className="truncate max-w-[22rem]" title={skill.source?.url || ''}>
                      {sourceLabel(skill)}
                    </span>
                  </div>
                </div>
                {canManage(skill) && (
                  <span className="flex items-center gap-1 flex-shrink-0">
                    <button type="button" disabled={busy} onClick={() => startEdit(skill)} className="px-2 py-1 rounded-lg text-[11px] text-zinc-300 hover:text-white hover:bg-[#1f1f23] disabled:opacity-50 transition" title="Edit">
                      <FiEdit2 className="text-[10px]" />
                    </button>
                    <button type="button" disabled={busy} onClick={() => run(() => deleteSkill(skill.id), 'Could not delete the skill.')} className="px-2 py-1 rounded-lg text-[11px] text-rose-400 hover:text-rose-300 hover:bg-rose-500/10 disabled:opacity-50 transition" title="Delete">
                      <FiTrash2 className="text-[10px]" />
                    </button>
                  </span>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}

      {!form && (
        <div className="space-y-2 rounded-xl border border-[#27272a] p-3 bg-[#141416]">
          <div className="flex items-center gap-2">
            <input
              type="text"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && url.trim() && !busy && install()}
              placeholder="https://github.com/anthropics/skills/tree/main/skills/pdf"
              className={`${inputClass} flex-1 font-mono`}
            />
            <button type="button" onClick={install} disabled={busy || !url.trim()} className={buttonClass}>
              <FiDownload className="text-xs" /> Install
            </button>
          </div>
          <div className="flex items-center gap-2 flex-wrap">
            <button type="button" onClick={() => fileInput.current?.click()} disabled={busy} className={buttonClass}>
              <FiUpload className="text-xs" /> Upload a zip or SKILL.md
            </button>
            <input ref={fileInput} type="file" accept=".zip,.md" onChange={upload} className="hidden" />
            <button type="button" onClick={() => setForm({ mode: 'new' })} disabled={busy} className={buttonClass}>
              <FiPlus className="text-xs" /> Write one
            </button>
            {admin && (
              <select value={scope} onChange={(e) => setScope(e.target.value)} className={`${inputClass} ml-auto`}>
                <option value="user">For my bots</option>
                <option value="system">For every account&apos;s bots (system)</option>
              </select>
            )}
          </div>
          {busy && <p className="text-[11px] text-zinc-500">Working…</p>}
        </div>
      )}

      {form && (
        <div className="space-y-2 rounded-xl border border-[#27272a] p-3 bg-[#141416]">
          <input type="text" value={name} onChange={(e) => setName(e.target.value)} placeholder="Name, e.g. weekly-report" maxLength={64} className={`${inputClass} w-full`} />
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="What it does and when to use it. The bot decides from this, e.g. “Write the Monday status report. Use when asked for the weekly report or a status update.”"
            rows={2}
            maxLength={1024}
            className={`${inputClass} w-full resize-y`}
          />
          <textarea
            value={instructions}
            onChange={(e) => setInstructions(e.target.value)}
            placeholder={'The instructions, in Markdown: steps, rules, examples.'}
            rows={12}
            className={`${inputClass} w-full resize-y font-mono`}
          />
          {form.mode === 'edit' && form.files?.length > 0 && (
            <p className="text-[10px] text-zinc-500">
              Its other files stay as they are: <span className="font-mono">{form.files.join(', ')}</span>
            </p>
          )}
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={submit}
              disabled={busy || !name.trim() || !description.trim() || !instructions.trim()}
              className="px-3 py-2 rounded-xl text-xs font-medium bg-zinc-100 text-black hover:bg-white disabled:opacity-40 transition flex items-center gap-1.5"
            >
              {form.mode === 'edit' ? <><FiCheck className="text-xs" /> Save</> : <><FiPlus className="text-xs" /> Add</>}
            </button>
            <button type="button" onClick={closeForm} className="px-3 py-2 rounded-xl text-xs text-zinc-400 hover:text-white transition">
              Cancel
            </button>
          </div>
        </div>
      )}

      {notice && <p className="text-[11px] text-emerald-300">{notice}</p>}
      {error && <p className="text-[11px] text-rose-400">{error}</p>}
      <p className="text-[10px] leading-relaxed text-zinc-500">
        A skill is a folder with a SKILL.md: a name and a description at the top, then instructions, plus any scripts and files they use (the
        format other assistants use too). Every bot sees each skill&apos;s name and description and reads one in full when a task fits it; its
        files are copied into the bot&apos;s computer, under /workspace/skills, to run there. Install from a GitHub folder or repository, a link to
        a .zip, or an upload; switch a skill off for one bot in its settings. Install only skills you trust: a bot follows them.
      </p>
    </div>
  );
}
