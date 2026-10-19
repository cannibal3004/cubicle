'use client';

import React, { useEffect, useRef, useState } from 'react';
import { FiTool, FiSliders } from 'react-icons/fi';
import { fetchToolCatalog } from '../lib/api';
import { countEnabled, isGroupOn, isToolkitOn, withGroup, withToolkit } from '../lib/toolSettings';

export function Switch({ on, onChange, disabled = false, label }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      disabled={disabled}
      onClick={(event) => {
        event.stopPropagation();
        onChange(!on);
      }}
      className={`relative inline-flex h-4 w-7 flex-shrink-0 items-center rounded-full transition disabled:opacity-40 ${
        on ? 'bg-emerald-500' : 'bg-zinc-700'
      }`}
    >
      <span className={`inline-block h-3 w-3 rounded-full bg-white shadow transition-transform ${on ? 'translate-x-3.5' : 'translate-x-0.5'}`} />
    </button>
  );
}

const AUTO_APPROVAL_CHOICES = [
  { value: 'inherit', label: 'App setting' },
  { value: 'off', label: 'Off' },
  { value: 'shadow', label: 'Shadow' },
  { value: 'on', label: 'On' },
];

// Quick per-conversation switches for tool groups and connected apps.
// Changes are saved to the bot straight away and apply from the next turn.
export default function ToolsPopover({ bot, onSave, onSaveAutoApproval, onSaveGateThreshold, onOpenEditor }) {
  // The bot's own risk threshold, or null to follow App Settings. Saved on release.
  const [threshold, setThreshold] = useState(typeof bot?.gate_threshold === 'number' ? bot.gate_threshold : null);
  useEffect(() => {
    setThreshold(typeof bot?.gate_threshold === 'number' ? bot.gate_threshold : null);
  }, [bot?.id, bot?.gate_threshold]);
  const [open, setOpen] = useState(false);
  const [catalog, setCatalog] = useState(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const ref = useRef(null);
  const settings = bot?.tool_settings || {};

  useEffect(() => {
    if (!open) return undefined;
    setLoading(true);
    fetchToolCatalog()
      .then((data) => setCatalog(data))
      .finally(() => setLoading(false));
    const close = (event) => {
      if (ref.current && !ref.current.contains(event.target)) setOpen(false);
    };
    document.addEventListener('mousedown', close);
    return () => document.removeEventListener('mousedown', close);
  }, [open]);

  const apply = async (next) => {
    setSaving(true);
    try {
      await onSave(next);
    } finally {
      setSaving(false);
    }
  };

  const counts = countEnabled(catalog, settings);
  const disabledGlobally = new Set(catalog?.disabled_toolkits || []);
  const toolCount = (tools) => `${tools.length} tool${tools.length === 1 ? '' : 's'}`;

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={`p-1.5 rounded-lg transition flex items-center gap-1 ${open ? 'text-white bg-[#1f1f23]' : 'text-zinc-400 hover:text-white hover:bg-[#1f1f23]'}`}
        title="Tools this bot may use"
      >
        <FiTool className="text-base" />
        {catalog && open && <span className="text-[10px] text-zinc-400">{counts.on}/{counts.total}</span>}
      </button>

      {open && (
        <div className="absolute right-0 mt-2 w-80 rounded-2xl bg-[#111114] border border-[#26262b] shadow-2xl z-50 text-zinc-200 overflow-hidden">
          <div className="px-4 py-3 border-b border-[#1f1f23]">
            <div className="text-xs font-semibold text-white">Tools for {bot?.name || 'this bot'}</div>
            <div className="text-[11px] text-zinc-500 mt-0.5">
              {catalog ? `${counts.on} of ${counts.total} tools offered on the next turn.` : 'Loading…'}
            </div>
          </div>

          <div className="max-h-80 overflow-y-auto py-1">
            {!loading && catalog && catalog.tools_enabled === false && (
              <p className="px-4 py-2 text-[11px] text-amber-400">Model tool calling is disabled on the server (LLM_TOOLS_ENABLED).</p>
            )}
            {catalog?.groups?.map((group) => (
              <div key={group.id} className="flex items-center justify-between px-4 py-2 hover:bg-[#16161a]">
                <div className="min-w-0">
                  <div className="text-xs text-zinc-100">{group.label}</div>
                  <div className="text-[10px] text-zinc-500">{toolCount(group.tools)}</div>
                </div>
                <Switch
                  on={isGroupOn(settings, group.id)}
                  disabled={saving}
                  label={`Toggle ${group.label}`}
                  onChange={(on) => apply(withGroup(settings, group.id, on))}
                />
              </div>
            ))}

            {catalog?.toolkits?.length > 0 && (
              <div className="px-4 pt-3 pb-1 text-[10px] uppercase tracking-wide text-zinc-500">Connected apps</div>
            )}
            {catalog?.toolkits?.map((toolkit) => {
              const offForAll = disabledGlobally.has(toolkit.slug);
              return (
                <div key={toolkit.slug} className={`flex items-center justify-between px-4 py-2 hover:bg-[#16161a] ${offForAll ? 'opacity-60' : ''}`}>
                  <div className="min-w-0">
                    <div className="text-xs text-zinc-100 truncate">{toolkit.name}</div>
                    <div className="text-[10px] text-zinc-500">
                      {offForAll ? 'Turned off for all bots in Plugins' : toolCount(toolkit.tools)}
                    </div>
                  </div>
                  <Switch
                    on={!offForAll && isToolkitOn(settings, toolkit.slug)}
                    disabled={saving || offForAll}
                    label={`Toggle ${toolkit.name}`}
                    onChange={(on) => apply(withToolkit(settings, toolkit.slug, on))}
                  />
                </div>
              );
            })}
            {catalog && !catalog.toolkits?.length && (
              <p className="px-4 py-2 text-[11px] text-zinc-500">No connected apps. Connect some in Plugins.</p>
            )}

            {onSaveAutoApproval && (
              <>
                <div className="px-4 pt-3 pb-1 text-[10px] uppercase tracking-wide text-zinc-500">Auto-approval</div>
                <div className="px-4 py-2">
                  <div className="flex rounded-lg border border-[#26262b] overflow-hidden">
                    {AUTO_APPROVAL_CHOICES.map((choice) => {
                      const current = bot?.auto_approval || 'inherit';
                      const active = current === choice.value;
                      return (
                        <button
                          key={choice.value}
                          type="button"
                          disabled={saving}
                          onClick={async () => {
                            if (active) return;
                            setSaving(true);
                            try {
                              await onSaveAutoApproval(choice.value);
                            } finally {
                              setSaving(false);
                            }
                          }}
                          className={`flex-1 px-2 py-1.5 text-[11px] transition disabled:opacity-40 ${
                            active ? 'bg-emerald-500/20 text-emerald-300' : 'text-zinc-400 hover:text-white hover:bg-[#16161a]'
                          }`}
                        >
                          {choice.label}
                        </button>
                      );
                    })}
                  </div>
                  <p className="mt-1.5 text-[10px] text-zinc-500">
                    Let a decision model approve this bot&apos;s low-risk actions. “App setting” follows Auto-approval in App Settings.
                  </p>
                  {Array.isArray(bot?.trusted_tools) && bot.trusted_tools.length > 0 && (
                    <p className="mt-1.5 text-[10px] text-emerald-300/80">
                      {bot.trusted_tools.length} trusted tool{bot.trusted_tools.length === 1 ? '' : 's'} run without asking whatever the mode
                      ({bot.trusted_tools.slice(0, 3).join(', ')}{bot.trusted_tools.length > 3 ? ', …' : ''}). Change this under “Choose individual tools”.
                    </p>
                  )}
                </div>
              </>
            )}

            {onSaveGateThreshold && (
              <div className="px-4 pb-3">
                <div className="flex items-center justify-between text-[10px] uppercase tracking-wide text-zinc-500">
                  <span>Risk threshold</span>
                  <span className="font-mono normal-case">{threshold === null ? 'app setting' : threshold.toFixed(2)}</span>
                </div>
                <div className="mt-1.5 flex items-center gap-2">
                  <input
                    type="range"
                    min="0.05"
                    max="0.9"
                    step="0.05"
                    value={threshold === null ? 0.2 : threshold}
                    disabled={saving}
                    onChange={(e) => setThreshold(Number(e.target.value))}
                    onMouseUp={async (e) => {
                      const value = Number(e.target.value);
                      setSaving(true);
                      try {
                        await onSaveGateThreshold(value);
                      } finally {
                        setSaving(false);
                      }
                    }}
                    onKeyUp={async (e) => {
                      if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'].includes(e.key)) return;
                      const value = Number(e.target.value);
                      setSaving(true);
                      try {
                        await onSaveGateThreshold(value);
                      } finally {
                        setSaving(false);
                      }
                    }}
                    className={`flex-1 accent-emerald-500 ${threshold === null ? 'opacity-50' : ''}`}
                    aria-label="Risk threshold for this bot"
                  />
                  {threshold !== null && (
                    <button
                      type="button"
                      disabled={saving}
                      onClick={async () => {
                        setSaving(true);
                        try {
                          await onSaveGateThreshold(null);
                        } finally {
                          setSaving(false);
                        }
                      }}
                      className="text-[10px] text-zinc-400 hover:text-white"
                    >
                      app
                    </button>
                  )}
                </div>
                <p className="mt-1.5 text-[10px] text-zinc-500">
                  Risk scores below this are approved for this bot. Higher lets more through: reads of the web score around 0.3 to 0.6 as
                  &quot;downloads code&quot;, so a research bot may need 0.5 or more; 0.2 is the strict default.
                </p>
              </div>
            )}
          </div>

          {onOpenEditor && (
            <button
              type="button"
              onClick={() => {
                setOpen(false);
                onOpenEditor();
              }}
              className="w-full flex items-center gap-2 px-4 py-2.5 text-xs text-zinc-300 hover:text-white hover:bg-[#16161a] border-t border-[#1f1f23] transition"
            >
              <FiSliders /> Choose individual tools…
            </button>
          )}
        </div>
      )}
    </div>
  );
}
