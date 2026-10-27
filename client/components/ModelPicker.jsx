'use client';

import React, { useState, useRef, useEffect, useMemo } from 'react';
import { FiChevronDown, FiCheck, FiSearch, FiRefreshCw } from 'react-icons/fi';

// ─── Catalog helpers (shared with AppSettingsDrawer) ───────────────────────

// Group a flat ModelInfo list (from GET /models) by its provider label.
export function groupModelsByProvider(models = []) {
  const groups = new Map();
  for (const model of models) {
    const key = model.provider || 'Other';
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(model);
  }
  return Array.from(groups.entries()).map(([provider, items]) => ({ provider, models: items }));
}

export function findModel(models = [], modelId) {
  if (!modelId) return null;
  return models.find((m) => m.id === modelId) || null;
}

// Deterministic accent colour per provider so the rail stays readable even
// with the sixty-odd providers OpenRouter exposes.
const ACCENTS = ['#a78bfa', '#34d399', '#f59e0b', '#60a5fa', '#f87171', '#22d3ee', '#fb923c', '#e879f9'];
export function providerAccent(provider = '') {
  let hash = 0;
  for (let i = 0; i < provider.length; i += 1) hash = (hash * 31 + provider.charCodeAt(i)) >>> 0;
  return ACCENTS[hash % ACCENTS.length];
}

function matches(model, query) {
  if (!query) return true;
  const haystack = `${model.id} ${model.name} ${model.provider}`.toLowerCase();
  return query
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean)
    .every((term) => haystack.includes(term));
}

function ModelTags({ model, accent }) {
  const tags = [];
  if (model.recommended) tags.push('Default');
  if (model.supports_reasoning) tags.push('Reasoning');
  if (model.supports_vision) tags.push('Vision');
  if (model.is_available === false) tags.push('Unverified');
  if (!tags.length) return null;
  return (
    <span className="flex items-center gap-1 flex-shrink-0">
      {tags.map((tag) => (
        <span
          key={tag}
          className="text-[9px] px-1.5 py-0.5 rounded-full font-semibold"
          style={{ background: `${accent}22`, color: accent }}
        >
          {tag}
        </span>
      ))}
    </span>
  );
}

// ─── ModelPicker ───────────────────────────────────────────────────────────
// `models` is the list returned by the backend catalog. The picker always
// lets the user type a model ID that is not in the list, because some local
// servers do not advertise what they can load.
export default function ModelPicker({
  currentModel,
  models = [],
  onSelectModel,
  onRefresh,
  catalogError,
  align = 'right',
  direction = 'down',
  triggerClassName,
  emptyLabel = 'Select Model',
}) {
  const [isOpen, setIsOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [activeProvider, setActiveProvider] = useState(null);
  const [refreshing, setRefreshing] = useState(false);
  const dropdownRef = useRef(null);
  const searchRef = useRef(null);

  const groups = useMemo(() => groupModelsByProvider(models), [models]);
  const currentInfo = findModel(models, currentModel);

  // Follow the selected model's provider whenever the selection changes.
  useEffect(() => {
    if (currentInfo?.provider) setActiveProvider(currentInfo.provider);
  }, [currentInfo?.provider]);

  useEffect(() => {
    function handleClickOutside(event) {
      if (dropdownRef.current && !dropdownRef.current.contains(event.target)) {
        setIsOpen(false);
      }
    }
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  useEffect(() => {
    if (isOpen) {
      setQuery('');
      setTimeout(() => searchRef.current?.focus(), 0);
    }
  }, [isOpen]);

  const trimmedQuery = query.trim();
  const searching = trimmedQuery.length > 0;

  // While searching, show matches across every provider. Otherwise show the
  // active provider's models.
  const visibleModels = useMemo(() => {
    if (searching) return models.filter((m) => matches(m, trimmedQuery));
    const group = groups.find((g) => g.provider === activeProvider) || groups[0];
    return group ? group.models : [];
  }, [models, groups, activeProvider, searching, trimmedQuery]);

  const activeGroup = groups.find((g) => g.provider === activeProvider) || groups[0];
  const exactMatch = searching && models.some((m) => m.id === trimmedQuery);

  const displayName = currentInfo?.name || currentModel || emptyLabel;
  const displayProvider = currentInfo?.provider || (currentModel?.includes('/') ? currentModel.split('/')[0] : '');
  const displayColor = providerAccent(displayProvider || displayName);

  const choose = (modelId) => {
    if (!modelId) return;
    onSelectModel(modelId);
    setIsOpen(false);
  };

  const handleRefresh = async () => {
    if (!onRefresh || refreshing) return;
    setRefreshing(true);
    try {
      await onRefresh();
    } finally {
      setRefreshing(false);
    }
  };

  const popoverPosition = `${direction === 'up' ? 'bottom-full mb-2' : 'mt-2'} ${align === 'left' ? 'left-0' : 'right-0'}`;

  return (
    <div className="relative z-50" ref={dropdownRef} suppressHydrationWarning={true}>
      {/* Trigger Button */}
      <button
        suppressHydrationWarning={true}
        type="button"
        onClick={() => setIsOpen(!isOpen)}
        title={currentModel}
        className={
          triggerClassName ||
          'flex items-center gap-1.5 px-3 py-1.5 rounded-xl bg-[#1c1c20] hover:bg-[#242429] border border-[#2b2b32] text-xs text-zinc-200 transition shadow-sm font-medium'
        }
      >
        <span className="w-2 h-2 rounded-full flex-shrink-0" style={{ background: displayColor }} />
        <span className="font-medium text-zinc-200 max-w-[180px] truncate">{displayName}</span>
        <FiChevronDown
          className={`text-zinc-400 text-xs transition-transform duration-200 ${isOpen ? 'rotate-180' : ''}`}
        />
      </button>

      {/* Floating Popover */}
      {isOpen && (
        <div
          className={`absolute ${popoverPosition} w-[380px] max-w-[90vw] rounded-2xl shadow-2xl border border-[#2c2c34] z-50 flex flex-col overflow-hidden animate-fade-in`}
          style={{ background: '#141417' }}
          suppressHydrationWarning={true}
        >
          {/* Search */}
          <div className="px-3 pt-3 pb-2 border-b border-[#1e1e22] flex items-center gap-2">
            <div className="relative flex-1">
              <FiSearch className="absolute left-2.5 top-1/2 -translate-y-1/2 text-zinc-500 text-xs" />
              <input
                ref={searchRef}
                suppressHydrationWarning={true}
                type="text"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && trimmedQuery) {
                    e.preventDefault();
                    choose(visibleModels.length === 1 ? visibleModels[0].id : trimmedQuery);
                  }
                  if (e.key === 'Escape') setIsOpen(false);
                }}
                placeholder={models.length ? `Search ${models.length} models or type an ID…` : 'Type a model ID…'}
                className="w-full bg-[#1c1c20] border border-[#2b2b32] rounded-lg pl-7 pr-2 py-1.5 text-xs text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-zinc-500 transition"
              />
            </div>
            {onRefresh && (
              <button
                suppressHydrationWarning={true}
                type="button"
                onClick={handleRefresh}
                title="Reload model list from the server"
                className="p-1.5 rounded-lg text-zinc-400 hover:text-white hover:bg-[#1f1f23] transition"
              >
                <FiRefreshCw className={`text-xs ${refreshing ? 'animate-spin' : ''}`} />
              </button>
            )}
          </div>

          {catalogError && (
            <div className="px-3 py-2 text-[10px] leading-relaxed text-amber-300 bg-amber-500/10 border-b border-amber-500/20 break-words">
              {catalogError}
            </div>
          )}

          <div className="flex min-h-0" style={{ height: 300 }}>
            {/* Provider Rail (hidden while searching) */}
            {!searching && groups.length > 1 && (
              <div
                className="w-[120px] bg-[#101013] border-r border-[#26262b] flex flex-col py-1.5 flex-shrink-0 overflow-y-auto"
                style={{ scrollbarWidth: 'thin', scrollbarColor: '#27272a transparent' }}
              >
                {groups.map((group) => {
                  const isSelected = activeGroup?.provider === group.provider;
                  const accent = providerAccent(group.provider);
                  return (
                    <button
                      key={group.provider}
                      suppressHydrationWarning={true}
                      type="button"
                      onClick={() => setActiveProvider(group.provider)}
                      title={`${group.provider} (${group.models.length})`}
                      className="mx-1.5 my-0.5 px-2 py-1.5 rounded-lg flex items-center gap-1.5 text-[11px] text-left transition-all"
                      style={
                        isSelected
                          ? { background: `${accent}20`, color: accent, boxShadow: `0 0 0 1px ${accent}40` }
                          : { color: '#a1a1aa' }
                      }
                    >
                      <span className="w-1.5 h-1.5 rounded-full flex-shrink-0" style={{ background: accent }} />
                      <span className="truncate flex-1">{group.provider}</span>
                      <span className="text-[9px] text-zinc-500">{group.models.length}</span>
                    </button>
                  );
                })}
              </div>
            )}

            {/* Model List */}
            <div className="flex-1 flex flex-col min-h-0">
              <div className="px-3.5 pt-2.5 pb-1.5 border-b border-[#1e1e22] flex-shrink-0">
                <h4 className="text-xs font-bold text-white tracking-wide truncate">
                  {searching ? 'Search results' : activeGroup?.provider || 'Models'}
                </h4>
                <p className="text-[10px] text-zinc-500 mt-0.5">
                  {visibleModels.length} {visibleModels.length === 1 ? 'model' : 'models'}
                </p>
              </div>

              <div
                className="overflow-y-auto flex-1 p-2 space-y-0.5"
                style={{ scrollbarWidth: 'thin', scrollbarColor: '#27272a transparent' }}
              >
                {visibleModels.map((model) => {
                  const isSelected = currentModel === model.id;
                  const accent = providerAccent(model.provider);
                  return (
                    <div
                      key={model.id}
                      onClick={() => choose(model.id)}
                      title={model.description ? `${model.id}\n${model.description}` : model.id}
                      className="px-3 py-2 rounded-xl cursor-pointer transition-all text-xs"
                      style={isSelected ? { background: `${accent}1a`, color: accent, fontWeight: 600 } : { color: '#a1a1aa' }}
                      onMouseEnter={(e) => {
                        if (!isSelected) {
                          e.currentTarget.style.background = '#1e1e23';
                          e.currentTarget.style.color = '#e4e4e7';
                        }
                      }}
                      onMouseLeave={(e) => {
                        if (!isSelected) {
                          e.currentTarget.style.background = '';
                          e.currentTarget.style.color = '#a1a1aa';
                        }
                      }}
                    >
                      <div className="flex items-center justify-between gap-2 min-w-0">
                        <span className="truncate">{model.name}</span>
                        <span className="flex items-center gap-1.5 flex-shrink-0">
                          <ModelTags model={model} accent={accent} />
                          {isSelected && <FiCheck className="text-sm" style={{ color: accent }} />}
                        </span>
                      </div>
                      {model.name !== model.id && (
                        <div className="text-[10px] text-zinc-500 font-mono truncate mt-0.5">{model.id}</div>
                      )}
                    </div>
                  );
                })}

                {/* Free-text model ID, for servers that do not list what they serve */}
                {searching && !exactMatch && (
                  <div
                    onClick={() => choose(trimmedQuery)}
                    className="px-3 py-2 rounded-xl cursor-pointer text-xs text-zinc-300 hover:bg-[#1e1e23] border border-dashed border-[#2c2c34] mt-1"
                  >
                    Use <span className="font-mono text-zinc-100">{trimmedQuery}</span> as the model ID
                  </div>
                )}

                {!visibleModels.length && !searching && (
                  <div className="px-3 py-6 text-center text-[11px] text-zinc-500 leading-relaxed">
                    No models were listed by the server. Type a model ID above to use it anyway.
                  </div>
                )}
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
