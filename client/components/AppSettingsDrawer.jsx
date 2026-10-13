"use client";

import React, { useState, useEffect } from "react";
import { FiX, FiCheck, FiEye, FiEyeOff } from "react-icons/fi";
import { fetchSettings, saveSettings, logout, checkDecider } from "../lib/api";
import ModelPicker from "./ModelPicker";

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

export default function AppSettingsDrawer({
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

  // LLM connection
  const [provider, setProvider] = useState("openai_compatible");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [apiKeyConfigured, setApiKeyConfigured] = useState(false);
  const [reasoningEffort, setReasoningEffort] = useState("");
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
      const localName = localStorage.getItem("cubicle_user_name") || localStorage.getItem("open_grok_user_name") || "";
      const localEmail = localStorage.getItem("cubicle_user_email") || localStorage.getItem("open_grok_user_email") || "";

      setUserName(localName);
      setUserEmail(localEmail);

      fetchSettings()
        .then((data) => {
          if (data) {
            setProvider(data.llm_provider || "openai_compatible");
            setBaseUrl(data.llm_base_url || "");
            setApiKey("");
            setApiKeyConfigured(Boolean(data.llm_api_key_configured));
            setReasoningEffort(data.llm_reasoning_effort || "");
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

  const triggerSavedNotice = (field) => {
    setSavedField(field);
    setTimeout(() => setSavedField(null), 1500);
  };

  const persist = async (field) => {
    setSaveError("");
    try {
      const saved = await saveSettings({
        llm_provider: provider,
        llm_base_url: baseUrl.trim(),
        llm_api_key: apiKey,
        llm_reasoning_effort: reasoningEffort,
        composio_api_key: composioApiKey,
        default_model: defaultModel,
        theme: "dark",
      });
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
        decider_url: deciderUrl.trim(),
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
    "w-full bg-[#222226] border border-[#2e2e34] rounded-xl px-3.5 py-2.5 text-xs text-zinc-100 placeholder-zinc-500 focus:outline-none focus:border-zinc-400 transition font-sans";

  return (
    <aside className="w-96 md:w-[420px] h-screen bg-[#111113] border-l border-[#1c1c20] flex flex-col z-30 shadow-2xl animate-fade-in select-none font-sans text-zinc-100 flex-shrink-0">
      {/* Drawer Header */}
      <div className="p-5 border-b border-[#1c1c20] flex items-center justify-between">
        <h2 className="text-sm font-bold text-zinc-100 tracking-wide">App Settings</h2>
        <button
          suppressHydrationWarning={true}
          onClick={onClose}
          className="p-1 rounded-lg text-zinc-400 hover:text-white hover:bg-[#1c1c20] transition"
          title="Close App Settings"
        >
          <FiX className="text-base" />
        </button>
      </div>

      {/* Drawer Content */}
      <div className="flex-1 overflow-y-auto p-5 space-y-6">
        {/* Profile Card Section */}
        <div className="bg-[#18181b] border border-[#27272a] rounded-2xl p-5 space-y-4 shadow-sm">
          <div>
            <h3 className="text-sm font-bold text-zinc-100">Profile</h3>
            <p className="text-xs text-zinc-400 mt-0.5">Shown in the sidebar. Saved as you go.</p>
          </div>

          <div className="space-y-3">
            <input
              suppressHydrationWarning={true}
              type="text"
              value={userName}
              onChange={(e) => {
                setUserName(e.target.value);
                localStorage.setItem("cubicle_user_name", e.target.value);
                if (onProfileUpdate) onProfileUpdate(e.target.value);
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
                localStorage.setItem("cubicle_user_email", e.target.value);
              }}
              placeholder="you@example.com"
              className={inputClass}
            />
          </div>

          <button
            suppressHydrationWarning={true}
            type="button"
            onClick={handleSignOut}
            className="text-[11px] text-zinc-500 hover:text-zinc-200 transition underline-offset-2 hover:underline"
          >
            Sign out of this browser
          </button>
        </div>

        {/* LLM Connection Card */}
        <div className="bg-[#18181b] border border-[#27272a] rounded-2xl p-5 space-y-5 shadow-sm">
          <div>
            <h3 className="text-sm font-bold text-zinc-100">Model server</h3>
            <p className="text-xs text-zinc-400 mt-1 leading-relaxed">
              Shared by all bots. Keys are write-only and encrypted locally. Leave a key blank to keep it.
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
                    onClick={() => setProvider(p.id)}
                    className={`flex-1 px-3 py-2 rounded-xl border text-xs font-medium transition ${
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
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder={providerInfo.defaultBaseUrl}
              className={`${inputClass} font-mono text-cyan-300`}
            />
            {provider === "openai_compatible" && (
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
            <p className="text-[10px] leading-relaxed text-zinc-500">
              Leave blank to use the provider default. The server must expose <span className="font-mono">/chat/completions</span> under this path.
            </p>
          </div>

          {/* API Key */}
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

          {saveError && (
            <p className="text-[11px] text-red-300 bg-red-500/10 border border-red-500/20 rounded-xl px-3 py-2">{saveError}</p>
          )}
        </div>

        {/* Model defaults */}
        <div className="bg-[#18181b] border border-[#27272a] rounded-2xl p-5 space-y-5 shadow-sm">
          <div>
            <h3 className="text-sm font-bold text-zinc-100">Model defaults</h3>
            <p className="text-xs text-zinc-400 mt-1 leading-relaxed">
              Applied to new bots and to the active bot when saved.
            </p>
          </div>

          {/* Default model */}
          <div className="space-y-1.5">
            <label className="text-xs font-semibold text-zinc-300 flex items-center gap-1.5">
              <span className="text-purple-400">•</span> Default model
            </label>
            <div className="flex items-center gap-2">
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
        </div>

        {/* Auto-approval */}
        <div className="bg-[#18181b] border border-[#27272a] rounded-2xl p-5 space-y-5 shadow-sm">
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

          <div className="space-y-1.5">
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
        <div className="bg-[#18181b] border border-[#27272a] rounded-2xl p-5 space-y-5 shadow-sm">
          <div>
            <h3 className="text-sm font-bold text-zinc-100">Connectors</h3>
          </div>

          <div className="space-y-1.5">
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
      </div>
    </aside>
  );
}
