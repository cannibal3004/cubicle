'use client';

import React, { useState } from 'react';
import { FiChevronRight, FiShield } from 'react-icons/fi';
import { useToolCatalog } from '../lib/toolCatalog';
import { Switch } from './ToolsPopover';
import {
  countEnabled,
  isGroupOn,
  isToolOn,
  isToolkitOn,
  isTrusted,
  toolkitDefaultOn,
  withGroup,
  withTool,
  withToolkit,
  withTrusted,
} from '../lib/toolSettings';

// The approval chip doubles as the trust switch: "asks first" becomes
// "trusted" (no card for this bot) and back with a click.
function ToolRow({ tool, on, parentOn, onChange, trusted, onTrust }) {
  const canTrust = tool.needs_approval && on && parentOn;
  return (
    <label className={`flex items-start gap-2.5 px-3 py-1.5 rounded-lg hover:bg-[#16161a] cursor-pointer ${parentOn ? '' : 'opacity-50'}`}>
      <input
        type="checkbox"
        checked={on}
        disabled={!parentOn}
        onChange={(event) => onChange(event.target.checked)}
        className="mt-0.5 accent-emerald-500"
      />
      <span className="min-w-0 flex-1">
        <span className="flex items-center gap-2">
          <span className="text-xs text-zinc-100 font-mono truncate">{tool.name}</span>
          {tool.needs_approval ? (
            <button
              type="button"
              disabled={!canTrust}
              title={trusted ? 'Runs without asking for this bot. Click to ask again.' : 'Asks before running. Click to trust it for this bot.'}
              onClick={(event) => {
                event.preventDefault();
                event.stopPropagation();
                onTrust(!trusted);
              }}
              className={`inline-flex items-center gap-1 text-[9px] uppercase tracking-wide rounded px-1 border transition disabled:cursor-default ${
                trusted
                  ? 'text-emerald-300 border-emerald-500/40 bg-emerald-500/10'
                  : 'text-amber-400/90 border-amber-500/30 hover:border-amber-400/70'
              }`}
            >
              {trusted && <FiShield className="text-[9px]" />}
              {trusted ? 'trusted' : 'asks first'}
            </button>
          ) : (
            <span className="text-[9px] uppercase tracking-wide text-zinc-500 border border-zinc-700 rounded px-1">read</span>
          )}
        </span>
        <span className="block text-[11px] text-zinc-500 leading-snug line-clamp-2">{tool.description}</span>
      </span>
    </label>
  );
}

function Section({ title, subtitle, on, disabled, onToggle, tools, settings, onChange, trusted, onTrustedChange }) {
  const [expanded, setExpanded] = useState(false);
  const enabledCount = tools.filter((tool) => isToolOn(settings, tool.name)).length;
  const trustedCount = on ? tools.filter((tool) => tool.needs_approval && isToolOn(settings, tool.name) && isTrusted(trusted, tool.name)).length : 0;
  return (
    <div className="rounded-xl border border-[#26262b] bg-[#141417]">
      <div className="flex items-center gap-2 px-3 py-2">
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="flex items-center gap-2 min-w-0 flex-1 text-left"
          aria-expanded={expanded}
        >
          <FiChevronRight className={`text-zinc-500 flex-shrink-0 transition-transform ${expanded ? 'rotate-90' : ''}`} />
          <span className="min-w-0">
            <span className="block text-xs text-zinc-100 truncate">{title}</span>
            <span className="block text-[10px] text-zinc-500">
              {subtitle || (on ? `${enabledCount} of ${tools.length} tools on${trustedCount ? `, ${trustedCount} trusted` : ''}` : 'Off for this bot')}
            </span>
          </span>
        </button>
        <Switch on={on} disabled={disabled} onChange={onToggle} label={`Toggle ${title}`} />
      </div>
      {expanded && (
        <div className="border-t border-[#1f1f23] py-1">
          {tools.map((tool) => (
            <ToolRow
              key={tool.name}
              tool={tool}
              on={isToolOn(settings, tool.name)}
              parentOn={on}
              onChange={(checked) => onChange(withTool(settings, tool.name, checked))}
              trusted={isTrusted(trusted, tool.name)}
              onTrust={(checked) => onTrustedChange(withTrusted(trusted, tool.name, checked))}
            />
          ))}
        </div>
      )}
    </div>
  );
}

// The "Tools" part of the bot editor: group and app switches with
// per-tool checkboxes underneath. Edits the settings object and the
// trusted list passed in.
export default function BotToolsSection({ settings, onChange, trusted = [], onTrustedChange = () => {} }) {
  const { catalog, error } = useToolCatalog();

  if (error) return <p className="text-xs text-rose-400">{error}</p>;
  if (!catalog) return <p className="text-xs text-zinc-500">Loading tools…</p>;

  const counts = countEnabled(catalog, settings);

  return (
    <div className="space-y-2">
      <p className="text-[11px] text-zinc-500">
        {counts.on} of {counts.total} tools will be offered to the model. A newly connected app is on unless it is switched off by default in Plugins.
      </p>
      <p className="text-[11px] text-zinc-500">
        A tool marked <span className="text-amber-400/90">asks first</span> shows you a card before it runs. Click the mark to{' '}
        <span className="text-emerald-300">trust</span> it for this bot: it then runs without asking, whatever the auto-approval mode, so trust only what
        this bot may do unsupervised.
        {trusted.length > 0 && ` ${trusted.length} trusted.`}
      </p>
      {catalog.groups.map((group) => (
        <Section
          key={group.id}
          title={group.label}
          on={isGroupOn(settings, group.id)}
          onToggle={(on) => onChange(withGroup(settings, group.id, on))}
          tools={group.tools}
          settings={settings}
          onChange={onChange}
          trusted={trusted}
          onTrustedChange={onTrustedChange}
        />
      ))}
      {catalog.toolkits.map((toolkit) => {
        const defaultOn = toolkitDefaultOn(catalog, toolkit.slug);
        const on = isToolkitOn(settings, toolkit.slug, defaultOn);
        return (
          <Section
            key={toolkit.slug}
            title={toolkit.name}
            subtitle={!defaultOn && !on ? 'Off by default (Plugins); switch on for this bot' : undefined}
            on={on}
            onToggle={(next) => onChange(withToolkit(settings, toolkit.slug, next, defaultOn))}
            tools={toolkit.tools}
            settings={settings}
            onChange={onChange}
            trusted={trusted}
            onTrustedChange={onTrustedChange}
          />
        );
      })}
      {!catalog.toolkits.length && <p className="text-[11px] text-zinc-500">No connected apps yet. Connect some in Plugins and their tools appear here.</p>}
    </div>
  );
}
