'use client';

import React, { useCallback, useEffect, useState } from 'react';
import { FiX, FiArchive, FiTrash2, FiRotateCcw, FiAlertTriangle } from 'react-icons/fi';
import ModelPicker from './ModelPicker';
import BotToolsSection from './BotToolsSection';
import SecretsSection from './SecretsSection';
import SchedulesSection from './SchedulesSection';
import BotMcpSection from './BotMcpSection';
import {
  fetchBotSecrets,
  setBotSecret,
  deleteBotSecret,
  fetchBotSchedules,
  createBotSchedule,
  updateBotSchedule,
  deleteBotSchedule,
  runBotSchedule,
  fetchBotMcpServers,
  createBotMcpServer,
  updateBotMcpServer,
  deleteBotMcpServer,
  testBotMcpServer,
} from '../lib/api';

const AVATARS = ['🤖', '🧠', '🦊', '🐙', '🦉', '🐝', '🧭', '🛠️', '📚', '🎯', '🧪', '🌱'];
const ACCENTS = ['#3b82f6', '#8b5cf6', '#ec4899', '#f59e0b', '#10b981', '#06b6d4', '#f43f5e', '#a3a3a3'];

const inputClass =
  'w-full bg-[#141417] border border-[#2a2a2f] rounded-lg px-3 py-2 text-sm text-zinc-100 placeholder-zinc-600 focus:outline-none focus:border-zinc-500 transition';
const labelClass = 'block text-[11px] uppercase tracking-wide text-zinc-500 font-semibold mb-1.5';

function Overlay({ onClose, children, labelledBy }) {
  useEffect(() => {
    const onKey = (event) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/60 backdrop-blur-sm p-4 overflow-y-auto"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
      role="dialog"
      aria-modal="true"
      aria-labelledby={labelledBy}
    >
      {children}
    </div>
  );
}

// A small in-page confirmation. The artifact viewer and many kiosks block
// window.confirm, so destructive actions confirm here instead.
export function ConfirmDialog({ title, body, confirmLabel = 'Delete', busy = false, onConfirm, onCancel }) {
  return (
    <Overlay onClose={onCancel} labelledBy="confirm-title">
      <div className="w-full max-w-sm my-auto bg-[#111114] border border-[#26262b] rounded-2xl shadow-2xl p-5 text-zinc-200">
        <div className="flex items-start gap-3">
          <div className="w-9 h-9 rounded-xl bg-rose-500/10 border border-rose-500/30 flex items-center justify-center flex-shrink-0">
            <FiAlertTriangle className="text-rose-400" />
          </div>
          <div className="min-w-0">
            <h3 id="confirm-title" className="text-sm font-semibold text-white">
              {title}
            </h3>
            <p className="text-xs text-zinc-400 mt-1 leading-relaxed">{body}</p>
          </div>
        </div>
        <div className="flex justify-end gap-2 mt-5">
          <button
            type="button"
            onClick={onCancel}
            className="px-3 py-1.5 text-xs rounded-lg text-zinc-300 hover:text-white hover:bg-[#1f1f23] transition"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={busy}
            className="px-3 py-1.5 text-xs rounded-lg bg-rose-600 hover:bg-rose-500 text-white font-semibold disabled:opacity-50 transition"
          >
            {busy ? 'Working…' : confirmLabel}
          </button>
        </div>
      </div>
    </Overlay>
  );
}

// Create or edit a bot persona. `bot` is null in create mode.
export default function BotEditorModal({
  bot,
  models = [],
  catalogError,
  onRefreshModels,
  defaultModel,
  onClose,
  onSave,
  onArchive,
  onDelete,
}) {
  const isEdit = Boolean(bot);
  const [name, setName] = useState('');
  const [role, setRole] = useState('');
  const [description, setDescription] = useState('');
  const [avatar, setAvatar] = useState('🤖');
  const [accent, setAccent] = useState(ACCENTS[0]);
  const [model, setModel] = useState('');
  const [systemPrompt, setSystemPrompt] = useState('');
  const [memory, setMemory] = useState('');
  const [toolSettings, setToolSettings] = useState({});
  const [showTools, setShowTools] = useState(false);
  const [showSecrets, setShowSecrets] = useState(false);
  const botId = bot?.id;
  const loadBotSecrets = useCallback(() => fetchBotSecrets(botId), [botId]);
  const saveBotSecret = useCallback((name, value) => setBotSecret(botId, name, value), [botId]);
  const removeBotSecret = useCallback((name) => deleteBotSecret(botId, name), [botId]);
  const [showSchedules, setShowSchedules] = useState(false);
  const loadSchedules = useCallback(() => fetchBotSchedules(botId), [botId]);
  const addSchedule = useCallback((schedule) => createBotSchedule(botId, schedule), [botId]);
  const changeSchedule = useCallback((id, changes) => updateBotSchedule(botId, id, changes), [botId]);
  const removeSchedule = useCallback((id) => deleteBotSchedule(botId, id), [botId]);
  const runSchedule = useCallback((id) => runBotSchedule(botId, id), [botId]);
  const [showMcp, setShowMcp] = useState(false);
  const loadMcp = useCallback(() => fetchBotMcpServers(botId), [botId]);
  const addMcp = useCallback((server) => createBotMcpServer(botId, server), [botId]);
  const changeMcp = useCallback((id, changes) => updateBotMcpServer(botId, id, changes), [botId]);
  const removeMcp = useCallback((id) => deleteBotMcpServer(botId, id), [botId]);
  const testMcp = useCallback((id) => testBotMcpServer(botId, id), [botId]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [confirmingDelete, setConfirmingDelete] = useState(false);

  useEffect(() => {
    setName(bot?.name || '');
    setRole(bot?.role || '');
    setDescription(bot?.description || '');
    setAvatar(bot?.avatar || '🤖');
    setAccent(bot?.accent_color || ACCENTS[0]);
    setModel(bot?.model || defaultModel || '');
    setSystemPrompt(bot?.system_prompt || '');
    setMemory(bot?.memory || '');
    setToolSettings(bot?.tool_settings || {});
    setShowTools(false);
    setShowSecrets(false);
    setError('');
    setConfirmingDelete(false);
  }, [bot, defaultModel]);

  const submit = async (event) => {
    event?.preventDefault();
    if (!name.trim()) {
      setError('Give the bot a name.');
      return;
    }
    setSaving(true);
    setError('');
    try {
      await onSave({
        name: name.trim(),
        role: role.trim(),
        description: description.trim(),
        avatar: avatar.trim() || '🤖',
        accent_color: accent,
        model: model.trim(),
        system_prompt: systemPrompt.trim(),
        tool_settings: toolSettings,
        // Only an existing bot has notes to edit; the server records a
        // change in the thread so the bot sees it.
        ...(bot ? { memory } : {}),
      });
    } catch (err) {
      setError(err?.message || 'Could not save the bot.');
      setSaving(false);
    }
  };

  const runDangerous = async (fn) => {
    setSaving(true);
    setError('');
    try {
      await fn();
    } catch (err) {
      setError(err?.message || 'The action failed.');
      setSaving(false);
    }
  };

  if (confirmingDelete) {
    return (
      <ConfirmDialog
        title={`Delete ${bot?.name || 'this bot'}?`}
        body="The bot, its whole conversation, and its computer are removed. This cannot be undone. Archiving keeps everything and just hides the bot."
        busy={saving}
        onCancel={() => setConfirmingDelete(false)}
        onConfirm={() => runDangerous(() => onDelete(bot))}
      />
    );
  }

  return (
    <Overlay onClose={onClose} labelledBy="bot-editor-title">
      <form
        onSubmit={submit}
        className="w-full max-w-lg my-auto bg-[#111114] border border-[#26262b] rounded-2xl shadow-2xl text-zinc-200 flex flex-col"
      >
        <div className="flex items-center justify-between px-5 py-4 border-b border-[#1f1f23]">
          <h2 id="bot-editor-title" className="text-sm font-semibold text-white">
            {isEdit ? 'Edit bot' : 'New bot'}
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="p-1.5 rounded-lg text-zinc-400 hover:text-white hover:bg-[#1f1f23] transition"
            title="Close"
          >
            <FiX />
          </button>
        </div>

        <div className="px-5 py-4 space-y-4">
          {/* Identity row: avatar + name + role */}
          <div className="flex gap-4">
            <div className="flex-shrink-0">
              <label className={labelClass}>Avatar</label>
              <div
                className="w-16 h-16 rounded-2xl flex items-center justify-center text-3xl border"
                style={{ background: `${accent}22`, borderColor: `${accent}66` }}
              >
                {avatar || '🤖'}
              </div>
            </div>
            <div className="flex-1 min-w-0 space-y-3">
              <div>
                <label className={labelClass} htmlFor="bot-name">
                  Name
                </label>
                <input
                  id="bot-name"
                  autoFocus
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="Research Assistant"
                  maxLength={80}
                  className={inputClass}
                />
              </div>
              <div>
                <label className={labelClass} htmlFor="bot-role">
                  Role
                </label>
                <input
                  id="bot-role"
                  value={role}
                  onChange={(e) => setRole(e.target.value)}
                  placeholder="General Intelligence"
                  maxLength={200}
                  className={inputClass}
                />
              </div>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-1.5">
            {AVATARS.map((emoji) => (
              <button
                key={emoji}
                type="button"
                onClick={() => setAvatar(emoji)}
                className={`w-8 h-8 rounded-lg text-lg flex items-center justify-center transition border ${
                  avatar === emoji ? 'border-zinc-400 bg-[#1f1f23]' : 'border-transparent hover:bg-[#1a1a1e]'
                }`}
                title={`Use ${emoji}`}
              >
                {emoji}
              </button>
            ))}
            <input
              value={avatar}
              onChange={(e) => setAvatar(e.target.value.slice(0, 4))}
              className="w-14 bg-[#141417] border border-[#2a2a2f] rounded-lg px-2 py-1 text-center text-sm focus:outline-none focus:border-zinc-500"
              title="Or type any emoji"
              aria-label="Custom avatar"
            />
            <div className="flex items-center gap-1.5 ml-auto">
              {ACCENTS.map((color) => (
                <button
                  key={color}
                  type="button"
                  onClick={() => setAccent(color)}
                  className={`w-5 h-5 rounded-full border-2 transition ${accent === color ? 'border-white scale-110' : 'border-transparent'}`}
                  style={{ background: color }}
                  title="Accent colour"
                />
              ))}
            </div>
          </div>

          <div>
            <label className={labelClass} htmlFor="bot-description">
              Description
            </label>
            <input
              id="bot-description"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="What this bot is for (shown in the sidebar)"
              maxLength={200}
              className={inputClass}
            />
          </div>

          <div>
            <label className={labelClass}>Model</label>
            <ModelPicker
              currentModel={model}
              models={models}
              catalogError={catalogError}
              onRefresh={onRefreshModels}
              onSelectModel={setModel}
              align="left"
              triggerClassName="w-full justify-between bg-[#141417] border border-[#2a2a2f] rounded-lg px-3 py-2"
            />
            <p className="text-[11px] text-zinc-500 mt-1">Leave as is to use the default model. You can change it later from the chat header.</p>
          </div>

          <div>
            <label className={labelClass} htmlFor="bot-prompt">
              System prompt
            </label>
            <textarea
              id="bot-prompt"
              value={systemPrompt}
              onChange={(e) => setSystemPrompt(e.target.value)}
              placeholder={`You are ${name.trim() || 'this bot'}, a helpful AI assistant.`}
              rows={7}
              maxLength={20000}
              className={`${inputClass} font-mono text-xs leading-relaxed resize-y min-h-[120px]`}
            />
          </div>

          {bot && (
            <div>
              <label className={labelClass} htmlFor="bot-memory">
                Notes (memory) · {memory.length}/{bot.memory_limit || 16000}
              </label>
              <textarea
                id="bot-memory"
                value={memory}
                onChange={(e) => setMemory(e.target.value)}
                placeholder="Nothing yet. The bot writes here with memory_write and memory_edit: the goal, decisions, where things are, how to run things."
                rows={6}
                maxLength={bot.memory_limit || 16000}
                className={`${inputClass} font-mono text-xs leading-relaxed resize-y min-h-[100px]`}
              />
              <p className="text-[11px] text-zinc-500 mt-1">The bot keeps these across conversations and compactions. Your edits are shown to it in the chat.</p>
            </div>
          )}

          <div>
            <button
              type="button"
              onClick={() => setShowTools((v) => !v)}
              className="flex items-center justify-between w-full text-left"
              aria-expanded={showTools}
            >
              <span className={labelClass + ' mb-0'}>Tools</span>
              <span className="text-[11px] text-zinc-500">{showTools ? 'Hide' : 'Choose which tools this bot may use'}</span>
            </button>
            {showTools && (
              <div className="mt-2">
                <BotToolsSection settings={toolSettings} onChange={setToolSettings} />
              </div>
            )}
          </div>

          {bot && (
            <div>
              <button
                type="button"
                onClick={() => setShowSecrets((v) => !v)}
                className="flex items-center justify-between w-full text-left"
                aria-expanded={showSecrets}
              >
                <span className={labelClass + ' mb-0'}>Secrets</span>
                <span className="text-[11px] text-zinc-500">{showSecrets ? 'Hide' : 'API keys for this bot\'s computer'}</span>
              </button>
              {showSecrets && (
                <div className="mt-2">
                  <SecretsSection
                    scope="bot"
                    load={loadBotSecrets}
                    save={saveBotSecret}
                    remove={removeBotSecret}
                    hint="Set as environment variables in this bot's computer, on top of the app-wide secrets. Values are stored encrypted and never shown again; a running computer picks up changes at its next start."
                  />
                </div>
              )}
            </div>
          )}

          {bot && (
            <div>
              <button
                type="button"
                onClick={() => setShowSchedules((v) => !v)}
                className="flex items-center justify-between w-full text-left"
                aria-expanded={showSchedules}
              >
                <span className={labelClass + ' mb-0'}>Schedules</span>
                <span className="text-[11px] text-zinc-500">{showSchedules ? 'Hide' : 'Prompts that run on their own'}</span>
              </button>
              {showSchedules && (
                <div className="mt-2">
                  <SchedulesSection load={loadSchedules} create={addSchedule} update={changeSchedule} remove={removeSchedule} runNow={runSchedule} />
                </div>
              )}
            </div>
          )}

          {bot && (
            <div>
              <button
                type="button"
                onClick={() => setShowMcp((v) => !v)}
                className="flex items-center justify-between w-full text-left"
                aria-expanded={showMcp}
              >
                <span className={labelClass + ' mb-0'}>MCP servers on this computer</span>
                <span className="text-[11px] text-zinc-500">{showMcp ? 'Hide' : 'Tool servers only this bot gets'}</span>
              </button>
              {showMcp && (
                <div className="mt-2">
                  <BotMcpSection load={loadMcp} create={addMcp} update={changeMcp} remove={removeMcp} test={testMcp} />
                </div>
              )}
            </div>
          )}

          {error && <p className="text-xs text-rose-400">{error}</p>}
        </div>

        <div className="flex items-center justify-between gap-2 px-5 py-4 border-t border-[#1f1f23]">
          <div className="flex items-center gap-1">
            {isEdit && onArchive && (
              <button
                type="button"
                disabled={saving}
                onClick={() => runDangerous(() => onArchive(bot, !bot.archived))}
                className="flex items-center gap-1.5 px-2.5 py-1.5 text-xs rounded-lg text-zinc-300 hover:text-white hover:bg-[#1f1f23] disabled:opacity-50 transition"
                title={bot.archived ? 'Bring the bot back to the list' : 'Hide the bot, keep its history'}
              >
                {bot.archived ? <FiRotateCcw /> : <FiArchive />}
                {bot.archived ? 'Unarchive' : 'Archive'}
              </button>
            )}
            {isEdit && onDelete && (
              <button
                type="button"
                disabled={saving}
                onClick={() => setConfirmingDelete(true)}
                className="flex items-center gap-1.5 px-2.5 py-1.5 text-xs rounded-lg text-rose-400 hover:text-rose-300 hover:bg-rose-500/10 disabled:opacity-50 transition"
              >
                <FiTrash2 />
                Delete
              </button>
            )}
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={onClose}
              className="px-3 py-1.5 text-xs rounded-lg text-zinc-300 hover:text-white hover:bg-[#1f1f23] transition"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={saving}
              className="px-4 py-1.5 text-xs rounded-lg bg-white text-black font-semibold hover:bg-zinc-200 disabled:opacity-50 transition"
            >
              {saving ? 'Saving…' : isEdit ? 'Save changes' : 'Create bot'}
            </button>
          </div>
        </div>
      </form>
    </Overlay>
  );
}
