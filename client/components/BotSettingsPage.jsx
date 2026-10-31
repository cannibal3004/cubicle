'use client';

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  FiX,
  FiArchive,
  FiTrash2,
  FiRotateCcw,
  FiLogOut,
  FiUser,
  FiCpu,
  FiFileText,
  FiBookOpen,
  FiTool,
  FiKey,
  FiClock,
  FiUsers,
} from 'react-icons/fi';
import ModelPicker from './ModelPicker';
import { useThinkingLevels } from './ThinkingPicker';
import { AUTO_APPROVAL_CHOICES, Switch } from './ToolsPopover';
import BotToolsSection from './BotToolsSection';
import SecretsSection from './SecretsSection';
import SchedulesSection from './SchedulesSection';
import BotMcpSection from './BotMcpSection';
import SharingSection from './SharingSection';
import ConfirmDialog from './ConfirmDialog';
import {
  draftBotSuggestions,
  fetchSkills,
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
  fetchBotMcpServerTools,
} from '../lib/api';

const MAX_SUGGESTIONS = 6;
const AVATARS = ['🤖', '🧠', '🦊', '🐙', '🦉', '🐝', '🧭', '🛠️', '📚', '🎯', '🧪', '🌱'];
const ACCENTS = ['#3b82f6', '#8b5cf6', '#ec4899', '#f59e0b', '#10b981', '#06b6d4', '#f43f5e', '#a3a3a3'];

// The page's sections. `create`: there before the bot exists. `fields`: what the save bar saves
// from it (the others save themselves as you go).
const SECTIONS = [
  { id: 'general', label: 'General', icon: FiUser, create: true, fields: ['name', 'role', 'description', 'avatar', 'accent_color', 'orchestrator'], blurb: 'Name, look, and what the bot is for.' },
  { id: 'model', label: 'Model & thinking', icon: FiCpu, create: true, fields: ['model', 'thinking', 'auto_approval', 'gate_threshold'], blurb: 'Which model answers, how hard it thinks, and which actions it may take without asking.' },
  { id: 'instructions', label: 'Instructions', icon: FiFileText, create: true, fields: ['system_prompt', 'suggestions'], blurb: 'The system prompt, and the suggestion cards on an empty conversation.' },
  { id: 'notes', label: 'Notes & skills', icon: FiBookOpen, fields: ['memory', 'skill_settings', 'procedures'], blurb: 'What the bot keeps between conversations, the skills it has, and the ones it wrote itself.' },
  { id: 'tools', label: 'Tools', icon: FiTool, create: true, fields: ['tool_settings', 'trusted_tools', 'allowed_commands'], blurb: 'Which tools it may use, which run without asking, and MCP servers on its computer.' },
  { id: 'secrets', label: 'Secrets', icon: FiKey, fields: [], blurb: "API keys and tokens for this bot's computer." },
  { id: 'schedules', label: 'Schedules', icon: FiClock, fields: [], blurb: 'Prompts that run on their own.' },
  { id: 'sharing', label: 'Sharing', icon: FiUsers, fields: [], blurb: 'Teammates who can use this bot, and what they get.' },
  { id: 'manage', label: 'Archive or delete', icon: FiArchive, fields: [], blurb: 'Hide the bot and keep its history, or delete it for good.' },
];

const inputClass =
  'w-full bg-[#222226] border border-[#2e2e34] rounded-xl px-3.5 py-2.5 text-xs text-zinc-100 placeholder-zinc-500 focus:outline-none focus:border-zinc-400 transition';
const labelClass = 'block text-[11px] uppercase tracking-wide text-zinc-500 font-semibold mb-1.5';
const helpClass = 'text-[11px] text-zinc-500 mt-1.5 leading-relaxed';

const list = (value) => (Array.isArray(value) ? value : []);
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// What the form shows for a bot (or a new one) before any edit.
function baseValues(bot, defaultModel) {
  return {
    name: bot?.name || '',
    role: bot?.role || '',
    description: bot?.description || '',
    avatar: bot?.avatar || '🤖',
    accent_color: bot?.accent_color || ACCENTS[0],
    orchestrator: Boolean(bot?.orchestrator),
    model: bot?.model || defaultModel || '',
    thinking: bot?.thinking || '',
    auto_approval: bot?.auto_approval || 'inherit',
    gate_threshold: typeof bot?.gate_threshold === 'number' ? bot.gate_threshold : null,
    system_prompt: bot?.system_prompt || '',
    suggestions: list(bot?.suggestions),
    memory: bot?.memory || '',
    procedures: list(bot?.procedures),
    skill_settings: bot?.skill_settings || {},
    tool_settings: bot?.tool_settings || {},
    trusted_tools: list(bot?.trusted_tools),
    allowed_commands: list(bot?.allowed_commands),
  };
}

// A field as the server takes it.
function forServer(field, value) {
  switch (field) {
    case 'name':
    case 'role':
    case 'description':
    case 'model':
    case 'system_prompt':
      return String(value || '').trim();
    case 'avatar':
      return String(value || '').trim() || '🤖';
    case 'suggestions':
      return value.map((s) => s.trim()).filter(Boolean);
    case 'gate_threshold':
      return value === null ? 'inherit' : value;
    default:
      return value;
  }
}

function Card({ title, help, children, right }) {
  return (
    <div className="bg-[#18181b] border border-[#27272a] rounded-2xl p-5 space-y-4 shadow-sm">
      {(title || right) && (
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            {title && <h3 className="text-sm font-bold text-zinc-100">{title}</h3>}
            {help && <p className="text-xs text-zinc-400 mt-0.5 leading-relaxed">{help}</p>}
          </div>
          {right}
        </div>
      )}
      {children}
    </div>
  );
}

// A bot's settings as a page of its own: a section list on the left, and a save bar for the
// bot's own fields. `bot` is null to create one. Edits are kept per field over the live bot, so
// a change made elsewhere meanwhile (the tools popover, the bot writing its notes) stays unless
// the same field was edited here.
export default function BotSettingsPage({
  bot,
  sectionRequest,
  models = [],
  catalogError,
  onRefreshModels,
  defaultModel,
  onClose,
  onSave,
  onArchive,
  onDelete,
  onLeave,
  onSortNotes,
  onDirtyChange,
}) {
  const isEdit = Boolean(bot);
  const botId = bot?.id;
  // A bot someone shared with you: their bot, which you may not archive, delete or make an orchestrator.
  const sharedWithMe = Boolean(bot?.shared);
  const thinkingLevels = useThinkingLevels();
  const base = useMemo(() => baseValues(bot, defaultModel), [bot, defaultModel]);
  const [edits, setEdits] = useState({});
  const values = { ...base, ...edits };
  const set = (field, value) =>
    setEdits((current) => {
      const next = { ...current };
      if (same(value, base[field])) delete next[field];
      else next[field] = value;
      return next;
    });
  const dirty = Object.keys(edits).length > 0;
  const reportDirty = useRef(onDirtyChange);
  reportDirty.current = onDirtyChange;
  useEffect(() => {
    reportDirty.current?.(dirty);
  }, [dirty]);
  useEffect(() => () => reportDirty.current?.(false), []);

  const sections = SECTIONS.filter((s) => (isEdit || s.create) && !(s.id === 'sharing' && bot?.orchestrator));
  // `sectionRequest` ({id, nonce}) opens a section, again each time it is asked for.
  const [section, setSection] = useState(sectionRequest?.id || 'general');
  useEffect(() => {
    if (sectionRequest?.id) setSection(sectionRequest.id);
  }, [sectionRequest]);
  const current = sections.find((s) => s.id === section) || sections[0];
  const show = (id) => current.id === id;
  const editedSections = sections.filter((s) => s.fields.some((f) => f in edits));

  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState('');
  const [confirming, setConfirming] = useState(''); // "", "close", "delete", "leave"
  const [drafting, setDrafting] = useState(false);
  const [openProcedure, setOpenProcedure] = useState('');
  // The skills library, for this bot's on/off switches.
  const [librarySkills, setLibrarySkills] = useState([]);
  useEffect(() => {
    fetchSkills()
      .then((data) => setLibrarySkills(data?.skills || []))
      .catch(() => setLibrarySkills([]));
  }, []);
  const [newRule, setNewRule] = useState('');

  const loadBotSecrets = useCallback(() => fetchBotSecrets(botId), [botId]);
  const saveBotSecret = useCallback((name, value) => setBotSecret(botId, name, value), [botId]);
  const removeBotSecret = useCallback((name) => deleteBotSecret(botId, name), [botId]);
  const loadSchedules = useCallback(() => fetchBotSchedules(botId), [botId]);
  const addSchedule = useCallback((schedule) => createBotSchedule(botId, schedule), [botId]);
  const changeSchedule = useCallback((id, changes) => updateBotSchedule(botId, id, changes), [botId]);
  const removeSchedule = useCallback((id) => deleteBotSchedule(botId, id), [botId]);
  const runSchedule = useCallback((id) => runBotSchedule(botId, id), [botId]);
  const loadMcp = useCallback(() => fetchBotMcpServers(botId), [botId]);
  const addMcp = useCallback((server) => createBotMcpServer(botId, server), [botId]);
  const changeMcp = useCallback((id, changes) => updateBotMcpServer(botId, id, changes), [botId]);
  const removeMcp = useCallback((id) => deleteBotMcpServer(botId, id), [botId]);
  const testMcp = useCallback((id) => testBotMcpServer(botId, id), [botId]);
  const loadMcpTools = useCallback((id) => fetchBotMcpServerTools(botId, id), [botId]);

  const save = async () => {
    if (!values.name.trim()) {
      setSection('general');
      setError('Give the bot a name.');
      return;
    }
    const fields = isEdit ? Object.keys(edits) : Object.keys(base).filter((f) => !['memory', 'procedures', 'skill_settings'].includes(f));
    const payload = Object.fromEntries(fields.map((f) => [f, forServer(f, values[f])]));
    setSaving(true);
    setError('');
    try {
      await onSave(payload, current.id);
      setEdits({});
      setSaved(true);
      setTimeout(() => setSaved(false), 1500);
    } catch (err) {
      setError(err?.message || 'Could not save the bot.');
    } finally {
      setSaving(false);
    }
  };

  const run = async (fn) => {
    setSaving(true);
    setError('');
    try {
      await fn();
    } catch (err) {
      setError(err?.message || 'The action failed.');
      setSaving(false);
    }
  };

  const close = () => (dirty ? setConfirming('close') : onClose());

  const addRule = () => {
    const rule = newRule.trim().split(/\s+/).join(' ');
    if (rule && !values.allowed_commands.includes(rule)) set('allowed_commands', [...values.allowed_commands, rule]);
    setNewRule('');
  };

  const sharingBlurb = sharedWithMe ? `Shared with you by ${bot.shared.owner_name}.` : current.blurb;

  return (
    <section className="absolute inset-0 z-30 bg-[#0d0d10] flex flex-col animate-fade-in font-sans text-zinc-100">
      {/* Page header: the bot, and the section's blurb */}
      <div className="px-6 py-3.5 border-b border-[#1c1c20] flex items-center justify-between gap-3 bg-[#09090b]/80 backdrop-blur-md">
        <div className="flex items-center gap-3 min-w-0">
          <div
            className="w-9 h-9 rounded-xl flex items-center justify-center text-lg border flex-shrink-0"
            style={{ background: `${values.accent_color}22`, borderColor: `${values.accent_color}66` }}
          >
            {values.avatar || '🤖'}
          </div>
          <div className="min-w-0">
            <h2 className="text-sm font-bold text-zinc-100 tracking-wide truncate">
              {isEdit ? values.name || bot.name : values.name.trim() || 'New bot'}
              {saved && <span className="ml-2 text-[11px] font-normal text-emerald-400">Saved</span>}
            </h2>
            <p className="text-[11px] text-zinc-500 truncate">{current.id === 'sharing' ? sharingBlurb : current.blurb}</p>
          </div>
        </div>
        <button
          type="button"
          onClick={close}
          className="p-1 rounded-lg text-zinc-400 hover:text-white hover:bg-[#1c1c20] transition"
          title={isEdit ? 'Close the bot settings' : 'Close without creating'}
        >
          <FiX className="text-base" />
        </button>
      </div>

      <div className="flex-1 flex min-h-0">
        {/* Section nav; a dot marks a section with unsaved edits */}
        <nav className="w-52 flex-shrink-0 border-r border-[#1c1c20] p-3 space-y-0.5 overflow-y-auto select-none">
          {sections.map((item) => {
            const Icon = item.icon;
            const active = item.id === current.id;
            const label = item.id === 'manage' && sharedWithMe ? 'Leave' : item.label;
            return (
              <button
                key={item.id}
                type="button"
                onClick={() => setSection(item.id)}
                className={`w-full flex items-center gap-2.5 px-3 py-2 rounded-xl text-xs text-left transition ${
                  active ? 'bg-[#27272a] text-white border border-[#34343a]' : 'text-zinc-400 hover:bg-[#1c1c20] hover:text-zinc-100 border border-transparent'
                }`}
              >
                <Icon className="text-sm flex-shrink-0" />
                <span className="truncate">{label}</span>
                {editedSections.includes(item) && <span className="ml-auto w-1.5 h-1.5 rounded-full bg-amber-400" title="Unsaved changes" />}
              </button>
            );
          })}
          {!isEdit && (
            <p className="px-3 pt-3 text-[10px] text-zinc-600 leading-relaxed">Notes, secrets, schedules and sharing open once the bot exists.</p>
          )}
        </nav>

        <div className="flex-1 overflow-y-auto p-6">
          <div className="max-w-3xl space-y-6">
            {show('general') && (
              <Card title="Identity" help="How the bot appears in the sidebar and the chat.">
                <div className="flex gap-4">
                  <div
                    className="w-16 h-16 rounded-2xl flex items-center justify-center text-3xl border flex-shrink-0"
                    style={{ background: `${values.accent_color}22`, borderColor: `${values.accent_color}66` }}
                  >
                    {values.avatar || '🤖'}
                  </div>
                  <div className="flex-1 min-w-0 grid grid-cols-1 sm:grid-cols-2 gap-3">
                    <div>
                      <label className={labelClass} htmlFor="bot-name">Name</label>
                      <input id="bot-name" autoFocus={!isEdit} value={values.name} onChange={(e) => set('name', e.target.value)} placeholder="Research Assistant" maxLength={80} className={inputClass} />
                    </div>
                    <div>
                      <label className={labelClass} htmlFor="bot-role">Role</label>
                      <input id="bot-role" value={values.role} onChange={(e) => set('role', e.target.value)} placeholder="General Intelligence" maxLength={200} className={inputClass} />
                    </div>
                  </div>
                </div>
                <div>
                  <span className={labelClass}>Avatar and colour</span>
                  <div className="flex flex-wrap items-center gap-1.5">
                    {AVATARS.map((emoji) => (
                      <button
                        key={emoji}
                        type="button"
                        onClick={() => set('avatar', emoji)}
                        className={`w-8 h-8 rounded-lg text-lg flex items-center justify-center transition border ${
                          values.avatar === emoji ? 'border-zinc-400 bg-[#1f1f23]' : 'border-transparent hover:bg-[#1a1a1e]'
                        }`}
                        title={`Use ${emoji}`}
                      >
                        {emoji}
                      </button>
                    ))}
                    <input
                      value={values.avatar}
                      onChange={(e) => set('avatar', e.target.value.slice(0, 4))}
                      className="w-14 bg-[#222226] border border-[#2e2e34] rounded-lg px-2 py-1 text-center text-sm focus:outline-none focus:border-zinc-400"
                      title="Or type any emoji"
                      aria-label="Custom avatar"
                    />
                    <div className="flex items-center gap-1.5 ml-auto">
                      {ACCENTS.map((color) => (
                        <button
                          key={color}
                          type="button"
                          onClick={() => set('accent_color', color)}
                          className={`w-5 h-5 rounded-full border-2 transition ${values.accent_color === color ? 'border-white scale-110' : 'border-transparent'}`}
                          style={{ background: color }}
                          title="Accent colour"
                        />
                      ))}
                    </div>
                  </div>
                </div>
                <div>
                  <label className={labelClass} htmlFor="bot-description">Description</label>
                  <input
                    id="bot-description"
                    value={values.description}
                    onChange={(e) => set('description', e.target.value)}
                    placeholder="What this bot is for (shown in the sidebar)"
                    maxLength={200}
                    className={inputClass}
                  />
                </div>
              </Card>
            )}
            {show('general') && !sharedWithMe && (
              <Card title="Orchestrator">
                <label className="flex items-start gap-2.5 text-xs text-zinc-300 cursor-pointer">
                  <input type="checkbox" checked={values.orchestrator} onChange={(e) => set('orchestrator', e.target.checked)} className="accent-cyan-400 mt-0.5" />
                  <span>
                    <span className="font-semibold">This bot is an orchestrator</span>
                    <span className="block text-[11px] text-zinc-500 mt-0.5">
                      Can see what your other bots are doing, create new bots, and hand them work. Tasks it delegates show up in the other bot&apos;s
                      chat as coming from it.
                    </span>
                  </span>
                </label>
              </Card>
            )}

            {show('model') && (
              <Card title="Model" help="Leave as is to use the default model. You can also change it from the chat header.">
                <ModelPicker
                  currentModel={values.model}
                  models={models}
                  catalogError={catalogError}
                  onRefresh={onRefreshModels}
                  onSelectModel={(m) => set('model', m)}
                  align="left"
                  triggerClassName="w-full flex items-center gap-2 bg-[#222226] hover:bg-[#26262b] border border-[#2e2e34] rounded-xl px-3.5 py-2.5 text-xs text-zinc-200 transition"
                />
              </Card>
            )}
            {show('model') && (
              <Card title="Thinking" help="How hard the model thinks before answering. Auto decides per message with the decision model.">
                <select id="bot-thinking" value={values.thinking} onChange={(e) => set('thinking', e.target.value)} className={inputClass} aria-label="Thinking">
                  <option value="">Default (App Settings)</option>
                  {thinkingLevels.map((level) => (
                    <option key={level.value} value={level.value}>
                      {level.label}
                      {level.help ? ` - ${level.help}` : ''}
                    </option>
                  ))}
                </select>
                <p className={helpClass}>Each conversation can override it with /think or the chip in the chat header.</p>
              </Card>
            )}
            {show('model') && (
              <Card
                title="Auto-approval"
                help="Let a decision model approve this bot's low-risk actions. App setting follows Auto-approval in App Settings; Shadow only records what it would have decided."
              >
                <div className="flex rounded-xl border border-[#2e2e34] overflow-hidden max-w-md">
                  {AUTO_APPROVAL_CHOICES.map((choice) => (
                    <button
                      key={choice.value}
                      type="button"
                      onClick={() => set('auto_approval', choice.value)}
                      className={`flex-1 px-2 py-2 text-[11px] transition ${
                        values.auto_approval === choice.value ? 'bg-emerald-500/20 text-emerald-300' : 'text-zinc-400 hover:text-white hover:bg-[#222226]'
                      }`}
                    >
                      {choice.label}
                    </button>
                  ))}
                </div>
                <div>
                  <div className="flex items-center justify-between max-w-md">
                    <span className={labelClass + ' mb-0'}>Risk threshold</span>
                    <span className="text-[11px] font-mono text-zinc-400">
                      {values.gate_threshold === null ? 'app setting' : values.gate_threshold.toFixed(2)}
                      {values.gate_threshold !== null && (
                        <button type="button" onClick={() => set('gate_threshold', null)} className="ml-2 text-zinc-500 hover:text-white">
                          use app setting
                        </button>
                      )}
                    </span>
                  </div>
                  <input
                    type="range"
                    min="0.05"
                    max="0.9"
                    step="0.05"
                    value={values.gate_threshold === null ? 0.2 : values.gate_threshold}
                    onChange={(e) => set('gate_threshold', Number(e.target.value))}
                    className={`w-full max-w-md accent-emerald-500 ${values.gate_threshold === null ? 'opacity-50' : ''}`}
                    aria-label="Risk threshold for this bot"
                  />
                  <p className={helpClass}>
                    Risk scores below this are approved for this bot. Higher lets more through: reads of the web score around 0.3 to 0.6 as
                    &quot;downloads code&quot;, so a research bot may need 0.5 or more; 0.2 is the strict default. Trusted tools (under Tools) run
                    without asking whatever the mode.
                  </p>
                </div>
              </Card>
            )}

            {show('instructions') && (
              <Card title="System prompt" help="Who the bot is and how it works. Sent at the start of every turn.">
                <textarea
                  id="bot-prompt"
                  value={values.system_prompt}
                  onChange={(e) => set('system_prompt', e.target.value)}
                  placeholder={`You are ${values.name.trim() || 'this bot'}, a helpful AI assistant.`}
                  rows={18}
                  maxLength={20000}
                  className={`${inputClass} font-mono leading-relaxed resize-y min-h-[200px]`}
                  aria-label="System prompt"
                />
                <p className="text-[11px] text-zinc-600 text-right -mt-2">{values.system_prompt.length.toLocaleString()}/20,000</p>
              </Card>
            )}
            {show('instructions') && (
              <Card
                title={`Suggestion cards · ${values.suggestions.length}/${MAX_SUGGESTIONS}`}
                help="Shown on an empty conversation so someone new knows where to start. A click puts one in the message box."
                right={
                  isEdit && (
                    <button
                      type="button"
                      disabled={drafting}
                      onClick={async () => {
                        setDrafting(true);
                        setError('');
                        try {
                          const drafted = await draftBotSuggestions(bot.id);
                          const kept = values.suggestions.map((s) => s.trim()).filter(Boolean);
                          set('suggestions', [...kept, ...drafted.filter((s) => !kept.includes(s))].slice(0, MAX_SUGGESTIONS));
                        } catch (err) {
                          setError(err?.message || 'Could not draft suggestions.');
                        } finally {
                          setDrafting(false);
                        }
                      }}
                      className="text-[11px] text-cyan-300 hover:text-white disabled:opacity-50 flex-shrink-0"
                    >
                      {drafting ? 'Drafting…' : 'Draft some for me'}
                    </button>
                  )
                }
              >
                <div className="space-y-1.5">
                  {values.suggestions.map((suggestion, index) => (
                    <div key={index} className="flex items-center gap-1.5">
                      <input
                        type="text"
                        value={suggestion}
                        maxLength={300}
                        onChange={(e) => set('suggestions', values.suggestions.map((s, i) => (i === index ? e.target.value : s)))}
                        placeholder="Is Nextcloud healthy?"
                        className={inputClass}
                        aria-label={`Suggestion ${index + 1}`}
                      />
                      <button
                        type="button"
                        onClick={() => set('suggestions', values.suggestions.filter((_, i) => i !== index))}
                        className="p-1.5 text-zinc-500 hover:text-white"
                        title="Remove"
                      >
                        <FiX />
                      </button>
                    </div>
                  ))}
                  {values.suggestions.length < MAX_SUGGESTIONS && (
                    <button type="button" onClick={() => set('suggestions', [...values.suggestions, ''])} className="text-[11px] text-zinc-400 hover:text-white">
                      + Add a suggestion
                    </button>
                  )}
                </div>
              </Card>
            )}

            {show('notes') && isEdit && (
              <Card
                title={`Notes (memory) · ${values.memory.length.toLocaleString()}/${(bot.memory_limit || 16000).toLocaleString()}`}
                help="Durable facts about the bot's job, shared by all its conversations. Where a piece of work stands goes in that project's work notes, and its history in the project's log. Your edits are shown to the bot in the chat."
              >
                <textarea
                  id="bot-memory"
                  value={values.memory}
                  onChange={(e) => set('memory', e.target.value)}
                  placeholder="Nothing yet. The bot writes here with memory_write and memory_edit: the goal, decisions, where things are, how to run things."
                  rows={16}
                  maxLength={bot.memory_limit || 16000}
                  className={`${inputClass} font-mono leading-relaxed resize-y min-h-[160px]`}
                  aria-label="Notes"
                />
                {onSortNotes && values.memory.trim() && (
                  <button
                    type="button"
                    disabled={dirty}
                    onClick={() => onSortNotes(bot)}
                    className="text-[11px] text-amber-300 hover:text-white disabled:opacity-40 disabled:hover:text-amber-300"
                    title={dirty ? 'Save or discard your changes first' : 'Starts a conversation in which the bot moves project status into projects and history into their logs'}
                  >
                    Have the bot sort these into notes and projects…
                  </button>
                )}
              </Card>
            )}
            {show('notes') && isEdit && (
              <Card
                title={`Skills${librarySkills.length ? ` · ${librarySkills.filter((s) => values.skill_settings[s.id] !== false).length} of ${librarySkills.length} on` : ''}`}
                help="Instructions for kinds of tasks, from the skills library (App Settings → Skills). The bot sees each one's name and description and reads it when a task fits; switch off what this bot should not use."
              >
                <div className="space-y-1.5">
                  {librarySkills.map((skill) => {
                    const on = values.skill_settings[skill.id] !== false;
                    return (
                      <div key={skill.id} className="flex items-start gap-3 rounded-xl border border-[#2e2e34] bg-[#141417] px-3 py-2">
                        <div className="min-w-0 flex-1">
                          <div className={`text-xs truncate ${on ? 'text-zinc-100' : 'text-zinc-500'}`}>{skill.name}</div>
                          <div className="text-[11px] text-zinc-500 leading-snug line-clamp-2" title={skill.description}>{skill.description}</div>
                        </div>
                        <Switch
                          on={on}
                          onChange={(next) => {
                            const settings = { ...values.skill_settings };
                            if (next) delete settings[skill.id];
                            else settings[skill.id] = false;
                            set('skill_settings', settings);
                          }}
                          label={`Toggle ${skill.name}`}
                        />
                      </div>
                    );
                  })}
                  {!librarySkills.length && <span className="text-[11px] text-zinc-600">No skills in the library yet; install some in App Settings → Skills.</span>}
                </div>
              </Card>
            )}
            {show('notes') && isEdit && (
              <Card
                title={`Skills this bot wrote${values.procedures.length ? ` · ${values.procedures.length}` : ''}`}
                help="How-tos the bot wrote itself, most from what you showed it on its computer (Show the bot, on the computer page). It reads one before a task it covers, and keeps them up to date."
              >
                <div className="space-y-1.5">
                  {values.procedures.map((procedure) => (
                    <div key={procedure.id} className="rounded-xl border border-[#2e2e34] bg-[#141417]">
                      <div className="flex items-center gap-2 px-3 py-2">
                        <button
                          type="button"
                          onClick={() => setOpenProcedure((open) => (open === procedure.id ? '' : procedure.id))}
                          className="flex-1 min-w-0 text-left text-xs text-zinc-100 truncate hover:text-white"
                          title={procedure.description || procedure.title}
                        >
                          {procedure.title}
                          {procedure.description && <span className="text-zinc-500"> · {procedure.description}</span>}
                        </button>
                        <span className="text-[10px] text-zinc-600 font-mono shrink-0">{(procedure.updated_at || procedure.created_at || '').slice(0, 10)}</span>
                        <button
                          type="button"
                          onClick={() => set('procedures', values.procedures.filter((p) => p.id !== procedure.id))}
                          className="text-zinc-500 hover:text-rose-300 shrink-0"
                          title="Remove (on save)"
                        >
                          ×
                        </button>
                      </div>
                      {openProcedure === procedure.id && (
                        <pre className="px-3 pb-2.5 max-h-72 overflow-auto text-[11px] font-mono text-zinc-300 whitespace-pre-wrap">{procedure.steps}</pre>
                      )}
                    </div>
                  ))}
                  {!values.procedures.length && <span className="text-[11px] text-zinc-600">None yet.</span>}
                </div>
              </Card>
            )}

            {show('tools') && (
              <Card title="Tools this bot may use" help="Switch groups, apps or single tools off; trust a tool to let it run without asking.">
                <BotToolsSection
                  settings={values.tool_settings}
                  onChange={(next) => set('tool_settings', next)}
                  trusted={values.trusted_tools}
                  onTrustedChange={(next) => set('trusted_tools', next)}
                />
              </Card>
            )}
            {show('tools') && (
              <Card
                title="Always-allowed commands"
                help="Terminal commands that run without asking, as prefixes: “git status”, “npm test”, “cat”. A script runs unasked only when every command in it matches one. Added from the approval card, or here."
              >
                <div className="flex flex-wrap gap-1.5">
                  {values.allowed_commands.map((rule) => (
                    <span
                      key={rule}
                      className="inline-flex items-center gap-1 px-2 py-1 rounded-lg border border-emerald-500/40 bg-emerald-500/10 text-[11px] font-mono text-emerald-200"
                    >
                      {rule} …
                      <button
                        type="button"
                        onClick={() => set('allowed_commands', values.allowed_commands.filter((r) => r !== rule))}
                        className="text-emerald-300/70 hover:text-white"
                        title="Remove"
                      >
                        ×
                      </button>
                    </span>
                  ))}
                  {!values.allowed_commands.length && <span className="text-[11px] text-zinc-600">None yet.</span>}
                </div>
                <div className="flex gap-2 max-w-md">
                  <input
                    value={newRule}
                    onChange={(e) => setNewRule(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key !== 'Enter') return;
                      e.preventDefault();
                      addRule();
                    }}
                    placeholder="git status"
                    className={`${inputClass} font-mono`}
                    aria-label="New always-allowed command"
                  />
                  <button type="button" onClick={addRule} className="px-3 text-[11px] rounded-xl border border-[#2e2e34] text-zinc-300 hover:text-white hover:bg-[#222226]">
                    Add
                  </button>
                </div>
              </Card>
            )}
            {show('tools') && isEdit && (
              <Card title="This bot's MCP servers" help="Tool servers only this bot gets: a command run inside its computer, or a URL. Saved as you go.">
                <BotMcpSection load={loadMcp} create={addMcp} update={changeMcp} remove={removeMcp} test={testMcp} loadTools={loadMcpTools} />
              </Card>
            )}

            {show('secrets') && isEdit && (
              <Card title="Secrets" help="Saved as you go.">
                <SecretsSection
                  scope="bot"
                  load={loadBotSecrets}
                  save={saveBotSecret}
                  remove={removeBotSecret}
                  hint="Set as environment variables in this bot's computer, on top of the app-wide secrets. Values are stored encrypted and never shown again; a running computer picks up changes at its next start."
                />
              </Card>
            )}

            {show('schedules') && isEdit && (
              <Card title="Schedules" help="Prompts the bot runs on its own, each in a conversation of its own. Saved as you go.">
                <SchedulesSection load={loadSchedules} create={addSchedule} update={changeSchedule} remove={removeSchedule} runNow={runSchedule} />
              </Card>
            )}

            {show('sharing') && isEdit && (
              <Card title="Sharing" help={sharedWithMe ? `${bot.shared.owner_name} shared this bot with you.` : 'Saved as you go.'}>
                <SharingSection bot={bot} onLeave={onLeave} />
              </Card>
            )}

            {show('manage') && isEdit && sharedWithMe && (
              <Card title="Leave this bot" help={`It stops being shared with you; ${bot.shared.owner_name} can share it again. Your conversations with it stay with the bot, out of sight.`}>
                <button
                  type="button"
                  disabled={saving}
                  onClick={() => onLeave?.(bot)}
                  className="flex items-center gap-1.5 px-3 py-2 text-xs rounded-xl text-rose-300 border border-rose-500/30 hover:bg-rose-500/10 disabled:opacity-50 transition"
                >
                  <FiLogOut /> Leave {bot.name}
                </button>
              </Card>
            )}
            {show('manage') && isEdit && !sharedWithMe && onArchive && (
              <Card
                title={bot.archived ? 'Archived' : 'Archive'}
                help={bot.archived ? 'The bot is hidden from the list. Bring it back with its history.' : 'Hide the bot from the list and keep everything: its conversations, notes and computer.'}
              >
                <button
                  type="button"
                  disabled={saving}
                  onClick={() => run(() => onArchive(bot, !bot.archived))}
                  className="flex items-center gap-1.5 px-3 py-2 text-xs rounded-xl text-zinc-200 border border-[#2e2e34] hover:bg-[#222226] disabled:opacity-50 transition"
                >
                  {bot.archived ? <FiRotateCcw /> : <FiArchive />}
                  {bot.archived ? 'Unarchive' : 'Archive'} {bot.name}
                </button>
              </Card>
            )}
            {show('manage') && isEdit && !sharedWithMe && onDelete && (
              <Card title="Delete" help="The bot, all its conversations, and its computer are removed. This cannot be undone.">
                <button
                  type="button"
                  disabled={saving}
                  onClick={() => setConfirming('delete')}
                  className="flex items-center gap-1.5 px-3 py-2 text-xs rounded-xl text-rose-300 border border-rose-500/30 hover:bg-rose-500/10 disabled:opacity-50 transition"
                >
                  <FiTrash2 /> Delete {bot.name}
                </button>
              </Card>
            )}
          </div>
        </div>
      </div>

      {/* The save bar: the bot's own fields, from every section */}
      {(dirty || !isEdit || error) && (
        <div className="px-6 py-3 border-t border-[#1c1c20] bg-[#09090b]/90 backdrop-blur-md flex items-center justify-between gap-3">
          <p className="text-[11px] min-w-0 truncate">
            {error ? (
              <span className="text-rose-400">{error}</span>
            ) : isEdit ? (
              <span className="text-amber-300">Unsaved changes in {editedSections.map((s) => s.label).join(', ')}</span>
            ) : (
              <span className="text-zinc-400">A new bot. Everything here can be changed later.</span>
            )}
          </p>
          <div className="flex items-center gap-2 flex-shrink-0">
            {isEdit ? (
              <button
                type="button"
                disabled={saving || !dirty}
                onClick={() => {
                  setEdits({});
                  setError('');
                }}
                className="px-3 py-1.5 text-xs rounded-lg text-zinc-300 hover:text-white hover:bg-[#1f1f23] disabled:opacity-40 transition"
              >
                Discard
              </button>
            ) : (
              <button type="button" onClick={close} className="px-3 py-1.5 text-xs rounded-lg text-zinc-300 hover:text-white hover:bg-[#1f1f23] transition">
                Cancel
              </button>
            )}
            <button
              type="button"
              disabled={saving || (isEdit && !dirty)}
              onClick={save}
              className="px-4 py-1.5 text-xs rounded-lg bg-white text-black font-semibold hover:bg-zinc-200 disabled:opacity-50 transition"
            >
              {saving ? 'Saving…' : isEdit ? 'Save changes' : 'Create bot'}
            </button>
          </div>
        </div>
      )}

      {confirming === 'close' && (
        <ConfirmDialog
          title={isEdit ? 'Discard unsaved changes?' : 'Discard this new bot?'}
          body={isEdit ? `Your changes in ${editedSections.map((s) => s.label).join(', ')} are not saved.` : 'It has not been created yet.'}
          confirmLabel="Discard"
          onCancel={() => setConfirming('')}
          onConfirm={() => {
            setConfirming('');
            setEdits({});
            onClose();
          }}
        />
      )}
      {confirming === 'delete' && (
        <ConfirmDialog
          title={`Delete ${bot?.name || 'this bot'}?`}
          body="The bot, all its conversations, and its computer are removed. This cannot be undone. Archiving keeps everything and just hides the bot."
          busy={saving}
          onCancel={() => setConfirming('')}
          onConfirm={() => run(() => onDelete(bot))}
        />
      )}
    </section>
  );
}
