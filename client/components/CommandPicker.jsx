'use client';

import React, { useEffect, useRef } from 'react';
import { FiCornerDownLeft, FiShield } from 'react-icons/fi';

// The `/` picker above the composer. Pure presentation: the composer owns the
// query, the highlighted row and the keyboard, so Enter in the textarea can
// mean "pick this" while the picker is open and "send" when it is not.
export default function CommandPicker({ commands, activeIndex, onPick, onHover }) {
  const listRef = useRef(null);

  useEffect(() => {
    const row = listRef.current?.children?.[activeIndex];
    row?.scrollIntoView?.({ block: 'nearest' });
  }, [activeIndex]);

  if (!commands.length) return null;

  return (
    <div className="w-full max-w-2xl mb-2 rounded-2xl border border-[#2b2b32] bg-[#141416] shadow-2xl overflow-hidden animate-fade-in">
      <div className="flex items-center justify-between px-3 py-1.5 border-b border-[#232328] text-[10px] uppercase tracking-wider text-zinc-500">
        <span>Commands</span>
        <span className="flex items-center gap-1 normal-case tracking-normal">
          <kbd className="px-1 rounded bg-[#1f1f23] text-zinc-400">↑↓</kbd> pick
          <kbd className="ml-1 px-1 rounded bg-[#1f1f23] text-zinc-400">Tab</kbd> fill
          <kbd className="ml-1 px-1 rounded bg-[#1f1f23] text-zinc-400">Esc</kbd> close
        </span>
      </div>
      <ul ref={listRef} role="listbox" className="max-h-64 overflow-y-auto py-1">
        {commands.map((command, index) => {
          const active = index === activeIndex;
          return (
            <li
              key={command.name}
              role="option"
              aria-selected={active}
              onMouseEnter={() => onHover(index)}
              onMouseDown={(event) => {
                event.preventDefault(); // keep focus in the textarea
                onPick(command);
              }}
              className={`flex items-start gap-3 px-3 py-2 cursor-pointer transition ${active ? 'bg-[#1f1f25]' : 'hover:bg-[#1a1a1e]'}`}
            >
              <div className="flex-1 min-w-0">
                <div className="flex items-baseline gap-2">
                  <span className="font-mono text-xs text-zinc-100">/{command.name}</span>
                  {command.args && <span className="font-mono text-[11px] text-zinc-500 truncate">{command.args}</span>}
                  {command.kind === 'action' && (
                    <span className="ml-auto flex items-center gap-1 text-[10px] text-amber-300/80" title="Runs through the action gateway and asks for approval">
                      <FiShield className="text-[10px]" /> approval
                    </span>
                  )}
                </div>
                <p className="text-[11px] text-zinc-400 mt-0.5 leading-snug">{command.description}</p>
              </div>
              {active && <FiCornerDownLeft className="text-zinc-500 text-sm mt-1 flex-shrink-0" />}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
