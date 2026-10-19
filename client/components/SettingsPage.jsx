"use client";

import React, { useState, useEffect } from "react";
import { FiX, FiCheck, FiEye, FiEyeOff, FiUser, FiUsers, FiServer, FiShield, FiLink, FiKey, FiCpu } from "react-icons/fi";
import {
  fetchSettings,
  saveSettings,
  fetchAccount,
  updateProfile,
  fetchModelSlots,
  logout,
  checkDecider,
  fetchSecrets,
  setSecret,
  deleteSecret,
  fetchMcpServers,
  createMcpServer,
  updateMcpServer,
  deleteMcpServer,
  testMcpServer,
  reconnectMcpServer,
  testMcpDefinition,
} from "../lib/api";
import ModelPicker from "./ModelPicker";
import SecretsSection from "./SecretsSection";
import McpServersSection from "./McpServersSection";
import AccountSection from "./AccountSection";
import AdminUsersSection from "./AdminUsersSection";

const PROVIDERS = [
  {
    id: "openai_compatible",
    name: "OpenAI-compatible",
    defaultBaseUrl: "https://openrouter.ai/api/v1",
    hint: "OpenRouter, Ollama, LM Studio, llama.cpp, vLLM, or the OpenAI API. The key is optional for local servers.",
  },
  {
    id: "muapi",
    name: "MUAPI (legacy)",
    defaultBaseUrl: "https://api.muapi.ai/api/v1",
    hint: "The original MUAPI prediction API. Requires a MUAPI key.",
  },
];

const BASE_URL_PRESETS = [
  { label: "OpenRouter", value: "https://openrouter.ai/api/v1" },
  { label: "Ollama", value: "http://localhost:11434/v1" },
  { label: "LM Studio", value: "http://localhost:1234/v1" },
  { label: "llama.cpp", value: "http://localhost:8080/v1" },
];

// The page's sections. Admin-only ones are hidden from members, whose
// saves never carry app-wide keys either.
const SECTIONS = [
  { id: "account", label: "Account", icon: FiUser, blurb: "Your profile, password, two-factor and API tokens." },
  { id: "users", label: "Users", icon: FiUsers, admin: true, blurb: "Accounts on this server." },
  { id: "model", label: "Model server", icon: FiCpu, adminEdits: true, blurb: "The model server every bot uses, and the default model." },
  { id: "approval", label: "Auto-approval", icon: FiShield, blurb: "Which actions ask you, and which a decision model may wave through." },
  { id: "connectors", label: "Connectors", icon: FiLink, blurb: "App connections for your bots." },
  { id: "secrets", label: "Secrets", icon: FiKey, blurb: "API keys and tokens your bots may use in their computers." },
  { id: "mcp", label: "MCP servers", icon: FiServer, blurb: "Tool servers for your bots, and system-wide ones." },
];

const AUTO_APPROVAL_OPTIONS = [
  { value: "off", label: "Off: every approval asks you" },
  { value: "shadow", label: "Shadow: score and log, you still decide" },
  { value: "on", label: "On: approve low-risk actions automatically" },
];

const REASONING_OPTIONS = [
  { value: "", label: "Not sent (server default)" },
  { value: "none", label: "none" },
  { value: "minimal", label: "minimal" },
  { value: "low", label: "low" },
  { value: "medium", label: "medium" },
  { value: "high", label: "high" },
  { value: "xhigh", label: "xhigh" },
  { value: "max", label: "max" },
];

export default function SettingsPage({
  user,
  isOpen,
  onClose,
  currentModel,
  models = [],
  catalogError,
  onRefreshModels,
  onUpdateDefaultModel,
  onConnectionSaved,
  onProfileUpdate,
  onSignOut,
}) {
  const [userName, setUserName] = useState("");
  const [userEmail, setUserEmail] = useState("");
  const [section, setSection] = useState("account");
  const [profileState, setProfileState] = useState(""); // "", "saving", "saved", or an error
  const profileTimer = React.useRef(null);
  const saveProfile = (name, email) => {
    // Saved as you go, a moment after typing stops.
    if (profileTimer.current) clearTimeout(profileTimer.current);
    profileTimer.current = setTimeout(async () => {
      setProfileState("saving");
      try {
        await updateProfile(name, email);
        setProfileState("saved");
        if (onProfileUpdate) onProfileUpdate(name);
        setTimeout(() => setProfileState(""), 1500);
      } catch (err) {
        setProfileState(err?.message || "Could not save the profile.");
      }
    }, 600);
  };
  // Admins manage what is shared by everyone: the model server, the
  // connector key, the decision server, system MCP servers, accounts.
  const admin = ['owner', 'admin'].includes(user?.role);

  // LLM connection
  const [provider, setProvider] = useState("openai_compatible");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [apiKeyConfigured, setApiKeyConfigured] = useState(false);
  const [reasoningEffort, setReasoningEffort] = useState("");
  const [maxConcurrent, setMaxConcurrent] = useState(4);
  const [slots, setSlots] = useState(null); // {limit, active, waiting}
  const [showApiKey, setShowApiKey] = useState(false);

  // Composio
  const [composioApiKey, setComposioApiKey] = useState("");
  const [composioConfigured, setComposioConfigured] = useState(false);
  const [showComposioKey, setShowComposioKey] = useState(false);

  const [defaultModel, setDefaultModel] = useState("");
  const [savedField, setSavedField] = useState(null);
  const [saveError, setSaveError] = useState("");

  // Auto-approval gate
  const [autoApproval, setAutoApproval] = useState("off");
  const [deciderUrl, setDeciderUrl] = useState("");
  const [deciderThreshold, setDeciderThreshold] = useState(0.2);
  const [deciderCheck, setDeciderCheck] = useState(null); // {ok, scores, latency_ms, error}
  const [checkingDecider, setCheckingDecider] = useState(false);

  useEffect(() => {
    if (isOpen) {
      // Load saved user profile
      // Keys were renamed with the project; read the old ones as a fallback.
      // The profile lives on the account; the bots are told it.
      fetchAccount()
        .then((me) => {
          setUserName(me?.display_name || "");
          setUserEmail(me?.email || "");
        })
        .catch(console.error);

      fetchModelSlots().then(setSlots).catch(() => setSlots(null));

      fetchSettings()
        .then((data) => {
          if (data) {
            setProvider(data.llm_provider || "openai_compatible");
            setBaseUrl(data.llm_base_url || "");
            setApiKey("");
            setApiKeyConfigured(Boolean(data.llm_api_key_configured));
            setReasoningEffort(data.llm_reasoning_effort || "");
            setMaxConcurrent(Number.isFinite(Number(data.llm_max_concurrent)) ? Number(data.llm_max_concurrent) : 4);
            setComposioApiKey("");
            setComposioConfigured(Boolean(data.composio_api_key_configured));
            setDefaultModel(data.default_model || "");
            setAutoApproval(data.auto_approval || "off");
            setDeciderUrl(data.decider_url || "");
            setDeciderThreshold(typeof data.decider_threshold === "number" ? data.decider_threshold : 0.2);
            setDeciderCheck(null);
          }
        })
        .catch(console.error);
    }
  }, [isOpen]);

  // Sync from external model change (e.g. chat header ModelPicker)
  useEffect(() => {
    if (currentModel && currentModel !== defaultModel) {
      setDefaultModel(currentModel);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentModel]);

  if (!isOpen) return null;

  const providerInfo = PROVIDERS.find((p) => p.id === provider) || PROVIDERS[0];
  const sections = SECTIONS.filter((s) => admin || !s.admin);
  const current = sections.find((s) => s.id === section) || sections[0];
  const show = (id) => current.id === id;

  const triggerSavedNotice = (field) => {
    setSavedField(field);
    setTimeout(() => setSavedField(null), 1500);
  };

  const persist = async (field) => {
    setSaveError("");
    try {
      const saved = await saveSettings(
        admin
          ? {
              llm_provider: provider,
              llm_base_url: baseUrl.trim(),
              llm_api_key: apiKey,
              llm_reasoning_effort: reasoningEffort,
              llm_max_concurrent: Math.max(0, Math.min(64, Math.round(Number(maxConcurrent) || 0))),
              composio_api_key: composioApiKey,
              default_model: defaultModel,
              theme: "dark",
            }
          : { theme: "dark" }
      );
      setApiKey("");
      setComposioApiKey("");
      setApiKeyConfigured(Boolean(saved?.llm_api_key_configured));
      setComposioConfigured(Boolean(saved?.composio_api_key_configured));
      triggerSavedNotice(field);
      return saved;
    } catch (err) {
      console.error("Failed to save settings:", err);
      setSaveError(err?.message || "Failed to save settings.");
      return null;
    }
  };

  const handleSaveConnection = async () => {
    const saved = await persist("connection");
    if (saved && onConnectionSaved) onConnectionSaved(saved);
  };

  const handleSaveComposio = () => persist("composio_key");

  // The gate's settings are saved on their own so a half-typed URL never
  // rides along with a model-server save.
  const handleSaveGate = async () => {
    setSaveError("");
    try {
      await saveSettings({
        auto_approval: autoApproval,
        ...(admin ? { decider_url: deciderUrl.trim() } : {}),
        decider_threshold: Number(deciderThreshold),
      });
      triggerSavedNotice("gate");
    } catch (err) {
      console.error("Failed to save auto-approval settings:", err);
      setSaveError(err?.message || "Failed to save auto-approval settings.");
    }
  };

  const handleCheckDecider = async () => {
    setCheckingDecider(true);
    setDeciderCheck(null);
    try {
      setDeciderCheck(await checkDecider(deciderUrl.trim()));
    } catch (err) {
      setDeciderCheck({ ok: false, error: err?.message || "Could not reach the decision server." });
    } finally {
      setCheckingDecider(false);
    }
  };

  const handleSaveModel = async () => {
    const saved = await persist("model");
    if (saved && onUpdateDefaultModel) onUpdateDefaultModel(defaultModel);
  };

  const handleSignOut = async () => {
    try {
      await logout();
    } finally {
      if (onSignOut) onSignOut();
    }
  };

  const SaveButton = ({ field, onClick }) => (
    <button
      suppressHydrationWarning={true}
      type="button"
      onClick={onClick}
      className="px-3.5 py-2.5 rounded-xl border border-[#33333a] bg-[#222226] hover:bg-[#2c2c34] text-xs font-medium text-zinc-300 hover:text-white transition flex items-center gap-1 flex-shrink-0"
    >
      <FiCheck className={savedField === field ? "text-emerald-400" : "text-zinc-400"} />
      <span>{savedField === field ? "Saved" : "Save"}</span>
    </button>
  );

  const inputClass =
    "w-full bg-[#222226] border border-[#2e2e34] rounded-xl px-3.5 py-2.5 text-xs text-zinc-100 placeholder-zinc-500 focus:outline-none focus:border-zinc-400 transition font-sans disabled:opacity-70 disabled:cursor-default";

  return (
    <section className="absolute inset-0 z-30 bg-[#0d0d10] flex flex-col animate-fade-in font-sans text-zinc-100">
      {/* Page header */}
      <div className="px-6 py-3.5 border-b border-[#1c1c20] flex items-center justify-between bg-[#09090b]/80 backdrop-blur-md">
        <div className="min-w-0">
          <h2 className="text-sm font-bold text-zinc-100 tracking-wide">Settings</h2>
          <p className="text-[11px] text-zinc-500 truncate">{current.blurb}</p>
        </div>
        <button
          suppressHydrationWarning={true}
          onClick={onClose}
          className="p-1 rounded-lg text-zinc-400 hover:text-white hover:bg-[#1c1c20] transition"
          title="Close App Settings"
        >
          <FiX className="text-base" />
        </button>
      </div>

      <div className="flex-1 flex min-h-0">
        {/* Section nav */}
        <nav className="w-52 flex-shrink-0 border-r border-[#1c1c20] p-3 space-y-0.5 overflow-y-auto select-none">
          {sections.map((item) => {
            const Icon = item.icon;
            const active = item.id === current.id;
            return (
              <button
                key={item.id}
                type="button"
                onClick={() => setSection(item.id)}
                className={`w-full flex items-center gap-2.5 px-3 py-2 rounded-xl text-xs text-left transition ${
                  active ? "bg-[#27272a] text-white border border-[#34343a]" : "text-zinc-400 hover:bg-[#1c1c20] hover:text-zinc-100 border border-transparent"
                }`}
              >
                <Icon className="text-sm flex-shrink-0" />
                <span className="truncate">{item.label}</span>
                {(item.admin || item.adminEdits) && (
                  <span className={`ml-auto text-[9px] uppercase tracking-wider ${admin ? "text-amber-300/80" : "text-zinc-500"}`}>
                    {admin ? "admin" : "view only"}
                  </span>
                )}
              </button>
            );
          })}
        </nav>

        {/* The section's cards */}
        <div className="flex-1 overflow-y-auto p-6">
        <div className="max-w-3xl space-y-6">
        {/* Profile Card Section */}
        <div className={`bg-[#18181b] border border-[#27272a] rounded-2xl p-5 space-y-4 shadow-sm ${show("account") ? "" : "hidden"}`}>
          <div>
            <h3 className="text-sm font-bold text-zinc-100">Profile</h3>
            <p className="text-xs text-zinc-400 mt-0.5">
              How your bots address you, and the address they use when you ask to be copied or contacted. Saved as you go.
              {profileState === "saving" && <span className="ml-2 text-zinc-500">Saving…</span>}
              {profileState === "saved" && <span className="ml-2 text-emerald-400">Saved</span>}
              {profileState && profileState !== "saving" && profileState !== "saved" && <span className="ml-2 text-rose-400">{profileState}</span>}
            </p>
          </div>

          <div className="space-y-3">
            <input
              suppressHydrationWarning={true}
              type="text"
              value={userName}
              onChange={(e) => {
                setUserName(e.target.value);
                saveProfile(e.target.value, userEmail);
              }}
              placeholder="Your name"
              className={inputClass}
            />

            <input
              suppressHydrationWarning={true}
              type="email"
              value={userEmail}
              onChange={(e) => {
                setUserEmail(e.target.value);
                saveProfile(userName, e.target.value);
              }}
              placeholder="you@example.com"
              className={inputClass}
            />
          </div>

        </div>

        {/* LLM Connection Card (admins) */}
        <div className={`bg-[#18181b] border border-[#27272a] rounded-2xl p-5 space-y-5 shadow-sm ${show("model") ? "" : "hidden"}`}>
          <div>
            <h3 className="text-sm font-bold text-zinc-100">Model server</h3>
            <p className="text-xs text-zinc-400 mt-1 leading-relaxed">
              {admin
                ? "Shared by all bots. Keys are write-only and encrypted locally. Leave a key blank to keep it."
                : "Shared by everyone's bots and set by an admin. Shown here so you know which server your bots talk to."}
            </p>
          </div>

          {/* Provider */}
          <div className="space-y-1.5">
            <label className="text-xs font-semibold text-zinc-300 flex items-center gap-1.5">
              <span className="text-emerald-400">•</span> Provider
            </label>
            <div className="flex gap-2">
              {PROVIDERS.map((p) => {
                const active = provider === p.id;
                return (
                  <button
                    key={p.id}
                    suppressHydrationWarning={true}
                    type="button"
                    disabled={!admin}
                    onClick={() => setProvider(p.id)}
                    className={`flex-1 px-3 py-2 rounded-xl border text-xs font-medium transition disabled:cursor-default ${
                      active
                        ? "border-emerald-500/50 bg-emerald-500/10 text-emerald-300"
                        : "border-[#2e2e34] bg-[#222226] text-zinc-400 hover:text-white"
                    }`}
                  >
                    {p.name}
                  </button>
                );
              })}
            </div>
            <p className="text-[10px] leading-relaxed text-zinc-500">{providerInfo.hint}</p>
          </div>

          {/* Base URL */}
          <div className="space-y-1.5">
            <label className="text-xs font-semibold text-zinc-300 flex items-center gap-1.5">
              <span className="text-blue-400">•</span> Base URL
            </label>
            <input
              suppressHydrationWarning={true}
              type="text"
              value={baseUrl}
              disabled={!admin}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder={providerInfo.defaultBaseUrl}
              className={`${inputClass} font-mono text-cyan-300`}
            />
            {admin && provider === "openai_compatible" && (
              <div className="flex flex-wrap gap-1.5 pt-0.5">
                {BASE_URL_PRESETS.map((preset) => (
                  <button
                    key={preset.value}
                    suppressHydrationWarning={true}
                    type="button"
                    onClick={() => setBaseUrl(preset.value)}
                    className="px-2 py-1 rounded-lg border border-[#2e2e34] bg-[#1c1c20] text-[10px] text-zinc-400 hover:text-white hover:border-zinc-500 transition"
                  >
                    {preset.label}
                  </button>
                ))}
              </div>
            )}
            {admin && (
              <p className="text-[10px] leading-relaxed text-zinc-500">
                Leave blank to use the provider default. The server must expose <span className="font-mono">/chat/completions</span> under this path.
              </p>
            )}
          </div>

          {/* API Key */}
          {!admin && (
            <p className="text-xs text-zinc-400">
              <span className="font-semibold text-zinc-300">API key:</span> {apiKeyConfigured ? "stored on the server." : "none set."}
            </p>
          )}
          {admin && (
          <div className="space-y-1.5">
            <label className="text-xs font-semibold text-zinc-300 flex items-center gap-1.5">
              <span className="text-amber-400">•</span> API Key
            </label>
            <div className="flex items-center gap-2">
              <div className="relative flex-1">
                <input
                  suppressHydrationWarning={true}
                  type={showApiKey ? "text" : "password"}
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  placeholder={
                    apiKeyConfigured
                      ? "Stored securely — enter to replace"
                      : provider === "muapi"
                        ? "Paste MUAPI API key..."
                        : "Paste API key (optional for local servers)..."
                  }
                  className="w-full bg-[#222226] border border-[#2e2e34] rounded-xl pl-3.5 pr-8 py-2.5 text-xs text-zinc-200 font-mono placeholder-zinc-500 focus:outline-none focus:border-zinc-400 transition"
                />
                <button
                  suppressHydrationWarning={true}
                  type="button"
                  onClick={() => setShowApiKey(!showApiKey)}
                  className="absolute right-2.5 top-1/2 -translate-y-1/2 text-zinc-400 hover:text-white transition text-xs"
                >
                  {showApiKey ? <FiEyeOff /> : <FiEye />}
                </button>
              </div>
              <SaveButton field="connection" onClick={handleSaveConnection} />
            </div>
            <p className="text-[10px] leading-relaxed text-zinc-500">
              Saving reloads the model list from the server.
            </p>
          </div>
          )}

          {saveError && (
            <p className="text-[11px] text-red-300 bg-red-500/10 border border-red-500/20 rounded-xl px-3 py-2">{saveError}</p>
          )}
        </div>

        {/* Model defaults (admins) */}
        <div className={`bg-[#18181b] border border-[#27272a] rounded-2xl p-5 space-y-5 shadow-sm ${show("model") ? "" : "hidden"}`}>
          <div>
            <h3 className="text-sm font-bold text-zinc-100">Model defaults</h3>
            <p className="text-xs text-zinc-400 mt-1 leading-relaxed">
              {admin ? "Applied to new bots and to the active bot when saved." : "What a new bot starts with. You can pick another model for any bot from its chat header."}
            </p>
          </div>

          {/* Default model */}
          <div className="space-y-1.5">
            <label className="text-xs font-semibold text-zinc-300 flex items-center gap-1.5">
              <span className="text-purple-400">•</span> Default model
            </label>
            {!admin && <div className={`${inputClass} font-mono text-cyan-300`}>{defaultModel || "not set"}</div>}
            <div className={`flex items-center gap-2 ${admin ? "" : "hidden"}`}>
              <div className="flex-1 min-w-0">
                <ModelPicker
                  currentModel={defaultModel}
                  models={models}
                  catalogError={catalogError}
                  onRefresh={onRefreshModels}
                  onSelectModel={setDefaultModel}
                  align="left"
                  direction="up"
                  triggerClassName="w-full flex items-center gap-2 bg-[#222226] border border-[#2e2e34] rounded-xl px-3.5 py-2.5 text-xs text-zinc-200 focus:outline-none focus:border-zinc-400 transition cursor-pointer font-sans"
                />
              </div>
              <SaveButton field="model" onClick={handleSaveModel} />
            </div>
          </div>

          {/* Reasoning effort */}
          <div className="space-y-1.5">
            <label className="text-xs font-semibold text-zinc-300 flex items-center gap-1.5">
              <span className="text-pink-400">•</span> Reasoning effort
            </label>
            <select
              suppressHydrationWarning={true}
              value={reasoningEffort}
              disabled={!admin}
              onChange={(e) => setReasoningEffort(e.target.value)}
              className={`${inputClass} cursor-pointer`}
            >
              {REASONING_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
            <p className="text-[10px] leading-relaxed text-zinc-500">
              Sent as <span className="font-mono">reasoning_effort</span> only when set; “none” turns thinking off on
              servers that support it. Not every server accepts every level (xhigh and max are llama.cpp /
              halogen-flash-server levels). A server that rejects the value returns an error in chat, so switch
              back to “Not sent” if that happens. Saved with the default model.
            </p>
          </div>

          {/* Concurrent requests */}
          <div className="space-y-1.5">
            <label className="text-xs font-semibold text-zinc-300 flex items-center gap-1.5">
              <span className="text-sky-400">•</span> Requests at a time
            </label>
            <div className="flex items-center gap-2">
              <input
                suppressHydrationWarning={true}
                type="number"
                min="0"
                max="64"
                step="1"
                value={maxConcurrent}
                disabled={!admin}
                onChange={(e) => setMaxConcurrent(e.target.value)}
                className={`${inputClass} w-28 font-mono`}
              />
              {admin && <SaveButton field="model" onClick={handleSaveModel} />}
              {slots && (
                <span className="text-[11px] text-zinc-400">
                  Right now: {slots.active} running{slots.waiting ? `, ${slots.waiting} waiting` : ''}.
                </span>
              )}
            </div>
            <p className="text-[10px] leading-relaxed text-zinc-500">
              How many requests the model server handles at once; everything beyond that waits its turn, shared fairly between
              accounts, and the chat shows the place in line. Match it to the server (a halogen-flash-server runs 4). 0 means no limit.
              Saved with the default model.
            </p>
          </div>
        </div>

        {/* Auto-approval */}
        <div className={`bg-[#18181b] border border-[#27272a] rounded-2xl p-5 space-y-5 shadow-sm ${show("approval") ? "" : "hidden"}`}>
          <div>
            <h3 className="text-sm font-bold text-zinc-100">Auto-approval</h3>
            <p className="text-xs text-zinc-400 mt-1 leading-relaxed">
              A small decision model scores each action that would normally ask you. Low-risk actions can run
              without a prompt; anything it is unsure about still asks. Each bot can override this in its Tools menu.
            </p>
          </div>

          <div className="space-y-1.5">
            <label className="text-xs font-semibold text-zinc-300 flex items-center gap-1.5">
              <span className="text-emerald-400">•</span> Mode
            </label>
            <select
              suppressHydrationWarning={true}
              value={autoApproval}
              onChange={(e) => setAutoApproval(e.target.value)}
              className={`${inputClass} cursor-pointer`}
            >
              {AUTO_APPROVAL_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
            <p className="text-[10px] leading-relaxed text-zinc-500">
              Start with Shadow: approvals work exactly as before, and the Audit panel shows what the model would
              have approved, so you can judge it before switching it on.
            </p>
          </div>

          <div className={`space-y-1.5 ${admin ? "" : "hidden"}`}>
            <label className="text-xs font-semibold text-zinc-300 flex items-center gap-1.5">
              <span className="text-emerald-400">•</span> Decision server
            </label>
            <div className="flex items-center gap-2">
              <input
                suppressHydrationWarning={true}
                type="text"
                value={deciderUrl}
                onChange={(e) => {
                  setDeciderUrl(e.target.value);
                  setDeciderCheck(null);
                }}
                placeholder="http://halogen-host:8731"
                className={`${inputClass} font-mono`}
              />
              <button
                suppressHydrationWarning={true}
                type="button"
                onClick={handleCheckDecider}
                disabled={checkingDecider || !deciderUrl.trim()}
                className="px-3.5 py-2.5 rounded-xl border border-[#33333a] bg-[#222226] hover:bg-[#2c2c34] text-xs font-medium text-zinc-300 hover:text-white transition flex-shrink-0 disabled:opacity-50"
              >
                {checkingDecider ? "Testing…" : "Test"}
              </button>
            </div>
            {deciderCheck && (
              <p className={`text-[10px] leading-relaxed ${deciderCheck.ok ? "text-emerald-400" : "text-red-300"}`}>
                {deciderCheck.ok
                  ? `Reachable, answered in ${deciderCheck.latency_ms} ms. A harmless probe scored ${Math.max(...Object.values(deciderCheck.scores || { x: 0 })).toFixed(2)} at most.`
                  : deciderCheck.error}
              </p>
            )}
            <p className="text-[10px] leading-relaxed text-zinc-500">
              A halogen-flash-server with <span className="font-mono">decider-4b</span> on its NPU, or any server
              with a <span className="font-mono">POST /v1/systemone</span> route. If it cannot be reached, every
              approval asks you as usual.
            </p>
          </div>

          <div className="space-y-1.5">
            <label className="text-xs font-semibold text-zinc-300 flex items-center gap-1.5">
              <span className="text-emerald-400">•</span> Risk threshold
              <span className="font-mono text-zinc-500">{Number(deciderThreshold).toFixed(2)}</span>
            </label>
            <div className="flex items-center gap-2">
              <input
                suppressHydrationWarning={true}
                type="range"
                min="0.05"
                max="0.5"
                step="0.05"
                value={deciderThreshold}
                onChange={(e) => setDeciderThreshold(Number(e.target.value))}
                className="flex-1 accent-emerald-500"
              />
              <SaveButton field="gate" onClick={handleSaveGate} />
            </div>
            <p className="text-[10px] leading-relaxed text-zinc-500">
              An action is approved only when every risk score is below this. Lower is stricter. 0.20 is the
              tested default; above 0.30 the model starts waving through things it should not.
            </p>
          </div>
        </div>

        {/* Connectors */}
        <div className={`bg-[#18181b] border border-[#27272a] rounded-2xl p-5 space-y-5 shadow-sm ${show("connectors") ? "" : "hidden"}`}>
          <div>
            <h3 className="text-sm font-bold text-zinc-100">Connectors</h3>
          </div>

          <div className={`space-y-1.5 ${admin ? "" : "hidden"}`}>
            <label className="text-xs font-semibold text-zinc-300 flex items-center gap-1.5">
              <span className="text-cyan-400">•</span> Composio API Key
            </label>
            <div className="flex items-center gap-2">
              <div className="relative flex-1">
                <input
                  suppressHydrationWarning={true}
                  type={showComposioKey ? "text" : "password"}
                  value={composioApiKey}
                  onChange={(e) => setComposioApiKey(e.target.value)}
                  placeholder={composioConfigured ? "Stored securely — enter to replace" : "Optional connector key..."}
                  className="w-full bg-[#222226] border border-[#2e2e34] rounded-xl pl-3.5 pr-8 py-2.5 text-xs text-zinc-200 font-mono placeholder-zinc-500 focus:outline-none focus:border-zinc-400 transition"
                />
                <button
                  suppressHydrationWarning={true}
                  type="button"
                  onClick={() => setShowComposioKey(!showComposioKey)}
                  className="absolute right-2.5 top-1/2 -translate-y-1/2 text-zinc-400 hover:text-white transition text-xs"
                  title={showComposioKey ? "Hide Composio key" : "Show Composio key"}
                >
                  {showComposioKey ? <FiEyeOff /> : <FiEye />}
                </button>
              </div>
              <SaveButton field="composio_key" onClick={handleSaveComposio} />
            </div>
            <p className="text-[10px] leading-relaxed text-zinc-500">
              Enables live connector catalog and OAuth links in Marketplace. Leave blank to keep the stored key.
            </p>
          </div>
        </div>

        {/* The signed-in account */}
        <div className={`bg-[#18181b] border border-[#27272a] rounded-2xl p-5 space-y-4 shadow-sm ${show("account") ? "" : "hidden"}`}>
          <div>
            <h3 className="text-sm font-bold text-zinc-100">Account</h3>
            <p className="text-[11px] text-zinc-500 mt-1">
              Your password, two-factor authentication, API tokens for scripts, and the devices signed in as you.
            </p>
          </div>
          <AccountSection onSignOut={handleSignOut} />
        </div>

        {admin && (
          <div className={`bg-[#18181b] border border-[#27272a] rounded-2xl p-5 space-y-4 shadow-sm ${show("users") ? "" : "hidden"}`}>
            <div>
              <h3 className="text-sm font-bold text-zinc-100">Users</h3>
              <p className="text-[11px] text-zinc-500 mt-1">
                Accounts on this server. Each person gets their own bots, chats and settings; admins also manage what is shared.
              </p>
            </div>
            <AdminUsersSection currentUser={user} />
          </div>
        )}

        {/* Secrets for bot computers */}
        <div className={`bg-[#18181b] border border-[#27272a] rounded-2xl p-5 space-y-4 shadow-sm ${show("secrets") ? "" : "hidden"}`}>
          <div>
            <h3 className="text-sm font-bold text-zinc-100">Secrets</h3>
            <p className="text-[11px] text-zinc-500 mt-1">
              API keys and tokens every bot may use. Each one is set as an environment variable inside the bot computers; a bot can add its own or override these in its editor.
            </p>
          </div>
          <SecretsSection scope="app" load={fetchSecrets} save={setSecret} remove={deleteSecret} />
        </div>

        {/* MCP servers */}
        <div className={`bg-[#18181b] border border-[#27272a] rounded-2xl p-5 space-y-4 shadow-sm ${show("mcp") ? "" : "hidden"}`}>
          <div>
            <h3 className="text-sm font-bold text-zinc-100">MCP servers</h3>
            <p className="text-[11px] text-zinc-500 mt-1">
              Tool servers that speak the Model Context Protocol: a command run here over stdio, or a URL. Your own servers are offered to your bots; system servers, which admins manage, to everyone&apos;s. A bot can add one of yours if you ask it to.
            </p>
          </div>
          <McpServersSection
            admin={admin}
            load={fetchMcpServers}
            create={createMcpServer}
            update={updateMcpServer}
            remove={deleteMcpServer}
            test={testMcpServer}
            testDraft={testMcpDefinition}
            reconnect={reconnectMcpServer}
          />
        </div>
        </div>
        </div>
      </div>
    </section>
  );
}
