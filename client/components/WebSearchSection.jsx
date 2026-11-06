"use client";

import React, { useEffect, useState } from "react";
import { FiCheck, FiEye, FiEyeOff } from "react-icons/fi";
import { fetchSettings, saveSettings, testWebSearch } from "../lib/api";

const PROVIDERS = [
  { id: "exa", label: "Exa", needs: "key", hint: "API key from dashboard.exa.ai. Search with categories (news, companies, people), and answers with sources." },
  { id: "tavily", label: "Tavily", needs: "key", hint: "API key (tvly-…) from app.tavily.com. Search with a news topic and a deeper mode, and answers with sources." },
  { id: "searxng", label: "SearXNG", needs: "url", hint: "Your own SearXNG server's address, with json in search.formats in its settings.yml. No key, no answers." },
];

const inputClass =
  "w-full bg-[#1b1b1f] border border-[#2e2e34] rounded-lg px-3 py-2 text-xs text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-zinc-400";

// One provider's fields: everyone's (scope "app", admins) or this account's own (scope "account").
function ProviderCard({ scope, title, help, values, onSaved }) {
  const prefix = scope === "app" ? "web_search_" : "user_web_search_";
  const [provider, setProvider] = useState(values[`${prefix}provider`] || "");
  const [key, setKey] = useState("");
  const [showKey, setShowKey] = useState(false);
  const [url, setUrl] = useState(values[`${prefix}url`] || "");
  const [busy, setBusy] = useState("");
  const [outcome, setOutcome] = useState(null); // {ok, text}
  const configured = Boolean(values[`${prefix}api_key_configured`]);
  const info = PROVIDERS.find((p) => p.id === provider);

  useEffect(() => {
    setProvider(values[`${prefix}provider`] || "");
    setUrl(values[`${prefix}url`] || "");
  }, [values, prefix]);

  const test = async () => {
    setBusy("test");
    setOutcome(null);
    try {
      const r = await testWebSearch({ scope, provider, api_key: key, url: url.trim() });
      setOutcome(
        r.ok
          ? { ok: true, text: `${r.provider} answered in ${(r.ms / 1000).toFixed(1)} s with ${r.results} result${r.results === 1 ? "" : "s"}${r.first ? `: “${r.first}”` : ""}${r.cost_usd != null ? ` · $${r.cost_usd}` : ""}${r.credits != null ? ` · ${r.credits} credit${r.credits === 1 ? "" : "s"}` : ""}` }
          : { ok: false, text: r.error || "It did not work." }
      );
    } catch (err) {
      setOutcome({ ok: false, text: err?.message || "The test failed." });
    } finally {
      setBusy("");
    }
  };

  const save = async () => {
    setBusy("save");
    setOutcome(null);
    try {
      const saved = await saveSettings({ [`${prefix}provider`]: provider, [`${prefix}api_key`]: key, [`${prefix}url`]: url.trim() });
      setKey("");
      onSaved(saved);
      setOutcome({ ok: true, text: "Saved." });
    } catch (err) {
      setOutcome({ ok: false, text: err?.message || "Could not save." });
    } finally {
      setBusy("");
    }
  };

  return (
    <div className="bg-[#18181b] border border-[#27272a] rounded-2xl p-5 space-y-3 shadow-sm">
      <div>
        <h3 className="text-sm font-semibold text-zinc-100">{title}</h3>
        <p className="text-[11px] text-zinc-500 mt-1 leading-relaxed">{help}</p>
      </div>
      <select value={provider} onChange={(e) => { setProvider(e.target.value); setOutcome(null); }} className={inputClass}>
        <option value="">{scope === "app" ? "None: bots can read pages, but not search" : "Use everyone's"}</option>
        {PROVIDERS.map((p) => (
          <option key={p.id} value={p.id}>{p.label}</option>
        ))}
      </select>
      {info && <p className="text-[11px] text-zinc-500">{info.hint}</p>}
      {info?.needs === "key" && (
        <div className="relative">
          <input
            type={showKey ? "text" : "password"}
            value={key}
            onChange={(e) => setKey(e.target.value)}
            placeholder={configured ? "A key is saved; type a new one to replace it" : "API key"}
            autoComplete="off"
            className={`${inputClass} pr-9`}
          />
          <button type="button" onClick={() => setShowKey((v) => !v)} className="absolute right-2.5 top-1/2 -translate-y-1/2 text-zinc-500 hover:text-zinc-200" title={showKey ? "Hide" : "Show"}>
            {showKey ? <FiEyeOff /> : <FiEye />}
          </button>
        </div>
      )}
      {info?.needs === "url" && (
        <input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://searxng.example.com" className={inputClass} />
      )}
      <div className="flex items-center gap-2">
        <button type="button" onClick={save} disabled={!!busy} className="px-3 py-1.5 rounded-lg text-xs font-semibold bg-zinc-100 text-black hover:bg-white disabled:opacity-40">
          {busy === "save" ? "Saving…" : "Save"}
        </button>
        {provider && (
          <button type="button" onClick={test} disabled={!!busy} className="px-3 py-1.5 rounded-lg text-xs border border-[#2e2e34] text-zinc-300 hover:text-white disabled:opacity-40">
            {busy === "test" ? "Searching…" : "Test"}
          </button>
        )}
        {outcome && (
          <span className={`text-[11px] ${outcome.ok ? "text-emerald-300" : "text-rose-300"} flex items-center gap-1 min-w-0`}>
            {outcome.ok && <FiCheck className="shrink-0" />}
            <span className="truncate" title={outcome.text}>{outcome.text}</span>
          </span>
        )}
      </div>
    </div>
  );
}

// Web search for bots: everyone's provider (admins) and, optionally, this account's own.
export default function WebSearchSection({ admin }) {
  const [values, setValues] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    fetchSettings()
      .then(setValues)
      .catch((err) => setError(err?.message || "Could not load the settings."));
  }, []);

  if (error) return <p className="text-xs text-rose-400">{error}</p>;
  if (!values) return <p className="text-xs text-zinc-500">Loading…</p>;
  return (
    <div className="space-y-4">
      {admin && (
        <ProviderCard
          scope="app"
          title="For everyone"
          help="The search provider every bot uses: web_search, web_answer (Exa and Tavily) and web_fetch, which reads a page. Without one, bots can still read pages they are given, but not search."
          values={values}
          onSaved={setValues}
        />
      )}
      <ProviderCard
        scope="account"
        title="Your own (optional)"
        help="Your bots use this instead of everyone's, with your own key: useful for your own account's credits or a different provider. A teammate using one of your bots goes through yours."
        values={values}
        onSaved={setValues}
      />
    </div>
  );
}
