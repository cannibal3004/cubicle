'use client';

import React, { useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { FiX, FiChevronRight, FiTool, FiZap, FiShield, FiEdit2, FiCopy, FiCheck } from 'react-icons/fi';
import FileCard from './FileCard';
import { apiUrl } from '../lib/api';

// The steps a user message carries after a demonstration on the bot's computer.
function DemonstrationChip({ demonstration }) {
  const [open, setOpen] = useState(false);
  const steps = demonstration.steps || 0;
  return (
    <span className="block mb-1">
      <button type="button" onClick={() => setOpen((v) => !v)} className="text-[10px] font-mono text-red-200/80 hover:text-white flex items-center gap-1" title="Recorded on the bot's computer">
        <span className="w-1.5 h-1.5 rounded-full bg-red-400" /> showed the bot · {steps} step{steps === 1 ? '' : 's'} · {open ? 'hide' : 'show'}
      </button>
      {open && demonstration.text && (
        <pre className="mt-1 max-h-56 overflow-auto rounded-lg bg-black/30 p-2 text-[10px] font-mono text-zinc-200 whitespace-pre-wrap text-left">{demonstration.text}</pre>
      )}
    </span>
  );
}

function formatMsgTime(createdAt) {
  if (!createdAt) return '';
  const d = new Date(createdAt);
  if (isNaN(d.getTime())) return '';
  return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true });
}

// Within this many pixels of the bottom, a live thinking block follows the stream.
const FOLLOW_SLACK_PX = 24;

// Collapsible "Thinking" block: one round's thinking, where it came in the reply.
// While the model is still thinking (nothing after it yet) it stays open and
// follows the stream, as long as the reader is at its bottom: scrolling up to
// read stops the following, scrolling back down resumes it. Once the round goes
// on (text or a tool call) it collapses unless the user has opened it.
function ThinkingBlock({ reasoning, live, level }) {
  const [userToggled, setUserToggled] = useState(null);
  const bodyRef = useRef(null);
  const followRef = useRef(true);

  const liveThinking = Boolean(live);
  const open = userToggled !== null ? userToggled : liveThinking;

  useEffect(() => {
    if (open) followRef.current = true; // opening starts at the newest
  }, [open]);
  useEffect(() => {
    const el = bodyRef.current;
    if (open && liveThinking && el && followRef.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [reasoning, open, liveThinking]);
  const onScroll = () => {
    const el = bodyRef.current;
    if (el) followRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < FOLLOW_SLACK_PX;
  };

  const words = reasoning.trim() ? reasoning.trim().split(/\s+/).length : 0;
  const label = liveThinking ? 'Thinking…' : 'Thinking';

  return (
    <div className="mb-2.5 rounded-xl border border-[#2b2b32] bg-[#141416]/70 overflow-hidden">
      <button
        suppressHydrationWarning={true}
        type="button"
        onClick={() => setUserToggled(!open)}
        className="w-full flex items-center gap-2 px-3 py-1.5 text-[11px] text-zinc-400 hover:text-zinc-200 hover:bg-[#1a1a1e] transition text-left"
      >
        <FiChevronRight className={`text-xs transition-transform duration-200 ${open ? 'rotate-90' : ''}`} />
        <span className="font-semibold tracking-wide">{label}</span>
        {level?.level && (
          <span className="text-[10px] text-zinc-500" title={level.auto ? 'Chosen for this message by Auto' : `Set by the ${level.source === 'app' ? 'App Settings' : level.source}`}>
            {level.level}{level.auto ? ' · auto' : ''}
          </span>
        )}
        {liveThinking && <span className="w-1.5 h-1.5 rounded-full bg-purple-400 animate-pulse" />}
        <span className="ml-auto font-mono text-[10px] text-zinc-500">{words} words</span>
      </button>
      {open && (
        <div
          ref={bodyRef}
          onScroll={onScroll}
          className="px-3.5 pb-3 pt-1 max-h-64 overflow-y-auto text-[11px] leading-relaxed text-zinc-400 whitespace-pre-wrap break-words font-sans border-t border-[#1f1f24]"
          style={{ scrollbarWidth: 'thin', scrollbarColor: '#27272a transparent' }}
        >
          {reasoning}
        </div>
      )}
    </div>
  );
}

const TOOL_STATUS_STYLES = {
  completed: 'text-emerald-400',
  started: 'text-blue-300',
  running: 'text-blue-300',
  failed: 'text-rose-400',
  denied: 'text-amber-400',
  expired: 'text-amber-400',
  rejected: 'text-rose-400',
  // Another conversation of the bot is using its computer.
  waiting: 'text-amber-300',
};

// How a call got approved, for the tool chip. Only auto-approvals get a mark;
// a user's own decision needs none.
function gateTitle(gate) {
  if (!gate) return '';
  const scores = Object.entries(gate.scores || {}).map(([k, v]) => `${k} ${Number(v).toFixed(2)}`).join(', ');
  return `${gate.outcome === 'auto_approved' ? 'Auto-approved' : 'Scored'} (${gate.mode}): ${gate.reason}${scores ? `\n${scores}` : ''}`;
}

// Compact list of the tools the model called while producing a reply.
function ToolCallList({ calls }) {
  return (
    <div className="mb-2.5 flex flex-wrap gap-1.5">
      {calls.map((call, index) => (
        <span
          key={call.id || `${call.name}-${index}`}
          title={[call.error || call.summary || call.name, gateTitle(call.gate), call.trusted ? (call.trusted_by === 'allowed_command' || call.trustedBy === 'allowed_command' ? 'An always-allowed command: ran without asking' : 'Trusted for this bot: ran without asking') : ''].filter(Boolean).join('\n')}
          className="inline-flex items-center gap-1.5 rounded-lg border border-[#2b2b32] bg-[#141416]/70 px-2 py-1 text-[10px] font-mono text-zinc-300"
        >
          <FiTool className="text-[10px] text-zinc-500" />
          <span>{call.name}</span>
          <span className={TOOL_STATUS_STYLES[call.status] || 'text-zinc-500'}>{call.status}</span>
          {call.gate?.outcome === 'auto_approved' && <FiZap className="text-[10px] text-emerald-400" aria-label="auto-approved" />}
          {call.trusted && <FiShield className="text-[10px] text-emerald-400" aria-label="trusted" />}
        </span>
      ))}
    </div>
  );
}

// The markdown styling of a bot reply, shared by every text segment of it.
const BOT_MARKDOWN = {
  p: ({ node, ...props }) => <div className="mb-2 last:mb-0 leading-relaxed" {...props} />,
  // An image in a reply: a help page's screenshot (/api/v1/help/images/...) or one from the web.
  img: ({ node, src, alt }) => <img src={apiUrl(src)} alt={alt || ''} loading="lazy" className="my-2 max-w-full rounded-xl border border-[#2b2b32] shadow-md" />,
  h1: ({ node, ...props }) => <h1 className="text-sm font-bold text-white mt-3 mb-1.5 first:mt-0" {...props} />,
  h2: ({ node, ...props }) => <h2 className="text-sm font-bold text-white mt-3 mb-1.5 first:mt-0" {...props} />,
  h3: ({ node, ...props }) => <h3 className="text-xs font-semibold text-zinc-100 mt-2.5 mb-1 first:mt-0" {...props} />,
  h4: ({ node, ...props }) => <h4 className="text-xs font-semibold text-zinc-100 mt-2 mb-1" {...props} />,
  a: ({ node, ...props }) => <a className="text-cyan-300 underline underline-offset-2 hover:text-cyan-200" target="_blank" rel="noreferrer" {...props} />,
  del: ({ node, ...props }) => <del className="text-zinc-500" {...props} />,
  blockquote: ({ node, ...props }) => <blockquote className="border-l-2 border-[#3f3f46] pl-3 my-2 text-zinc-400 italic" {...props} />,
  hr: ({ node, ...props }) => <hr className="my-3 border-[#26262b]" {...props} />,
  table: ({ node, ...props }) => (
    <div className="my-2 overflow-x-auto rounded-lg border border-[#2b2b32]">
      <table className="w-full text-left text-[11px] border-collapse" {...props} />
    </div>
  ),
  thead: ({ node, ...props }) => <thead className="bg-[#1a1a1e] text-zinc-100" {...props} />,
  th: ({ node, ...props }) => <th className="px-2.5 py-1.5 font-semibold border-b border-[#2b2b32] whitespace-nowrap" {...props} />,
  td: ({ node, ...props }) => <td className="px-2.5 py-1 border-b border-[#1f1f23] align-top text-zinc-200" {...props} />,
  input: ({ node, ...props }) => <input className="mr-1.5 accent-cyan-400 align-middle" {...props} />,
  strong: ({ node, ...props }) => <strong className="font-bold text-white" {...props} />,
  code: ({ node, inline, className, children, ...props }) => {
    const isInline = inline || (!className && typeof children === 'string' && !children.includes('\n'));
    if (isInline) {
      return (
        <code className="bg-[#2a2a30] text-cyan-300 px-1.5 py-0.5 rounded font-mono text-[11px]" {...props}>
          {children}
        </code>
      );
    }
    return (
      <pre className="bg-[#141416] p-3 rounded-xl border border-[#2b2b32] text-zinc-300 font-mono text-[11px] overflow-x-auto my-2">
        <code {...props}>{children}</code>
      </pre>
    );
  },
  ul: ({ node, ...props }) => <ul className="list-disc list-inside space-y-1 my-1 text-zinc-300" {...props} />,
  ol: ({ node, ...props }) => <ol className="list-decimal list-inside space-y-1 my-1 text-zinc-300" {...props} />,
};

function BotText({ text }) {
  if (!text || !String(text).trim()) return null;
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={BOT_MARKDOWN}>
      {text}
    </ReactMarkdown>
  );
}

// The action's description without the bot id the server adds for the audit
// trail ("Run in the computer for bot-1a2b3c: ls" reads "Run: ls").
function stepSummary(summary) {
  return String(summary || '')
    .replace(/\s+(?:in|from|on|to|of)\s+the computer for bot-[\w-]+/g, '')
    .replace(/\s+(?:for|of)\s+bot-[\w-]+/g, '')
    .trim();
}

// ---- a step's details: what it was given, what came back, how it was allowed -----------------

function parseMaybeJson(value) {
  if (typeof value !== 'string') return value;
  const text = value.trim();
  if (!text || !'{["'.includes(text[0])) return value;
  try {
    return JSON.parse(text);
  } catch {
    return value;
  }
}

// A stored result: JSON, cut at the storage limit with "...[trimmed N characters]" when long.
// A cut one is closed again where it broke off, so long terminal output still reads as output;
// `trimmed` says how much is missing.
function parseResult(value) {
  if (typeof value !== 'string') return { value, trimmed: 0 };
  const cut = /\n\.\.\.\[trimmed (\d+) characters\]$/.exec(value);
  if (!cut) return { value: parseMaybeJson(value), trimmed: 0 };
  const body = value.slice(0, cut.index);
  for (const start of [body, body.replace(/\\+$/, ''), body.replace(/\\u[0-9a-fA-F]{0,3}$/, '')]) {
    for (const close of ['"}', '}', '"]}', ']}', '"}}', '}}']) {
      try {
        return { value: JSON.parse(start + close), trimmed: Number(cut[1]) };
      } catch {
        // try the next way of closing it
      }
    }
  }
  return { value, trimmed: 0 };
}

function formatDuration(ms) {
  if (!Number.isFinite(ms)) return '';
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.floor(ms / 60000)} min ${Math.round((ms % 60000) / 1000)} s`;
}

// How the step was allowed to run, in a sentence; '' when it needed nobody's say.
function approvalText(call) {
  if (call.trusted) {
    return call.trusted_by === 'allowed_command' || call.trustedBy === 'allowed_command'
      ? 'An always-allowed command: ran without asking'
      : 'Trusted for this bot: ran without asking';
  }
  if (call.gate?.outcome === 'auto_approved') return `Auto-approved by the decision model (${call.gate.mode}): ${call.gate.reason}`;
  if (call.status === 'denied') return 'Denied by you';
  if (call.status === 'expired') return 'Nobody answered the approval card in time';
  if (call.gate) return `Approved by you${call.gate.reason ? ` · the decision model said: ${call.gate.reason}` : ''}`;
  return '';
}

// Fields the server adds for its own bookkeeping, not what the bot asked for or got back.
const HIDDEN_ARGUMENTS = new Set(['bot_id', 'thread_id', 'computer_id', 'provider', 'operation']);

function CopyButton({ text }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      onClick={(event) => {
        event.stopPropagation();
        navigator.clipboard?.writeText(text).then(() => {
          setCopied(true);
          setTimeout(() => setCopied(false), 1200);
        });
      }}
      className="flex-shrink-0 p-1 rounded text-zinc-500 hover:text-white hover:bg-[#222226] transition"
      title="Copy"
    >
      {copied ? <FiCheck className="text-[11px] text-emerald-400" /> : <FiCopy className="text-[11px]" />}
    </button>
  );
}

// One value: short text inline, long or multi-line text as it is written (`block` always so),
// the rest as JSON.
function DetailValue({ value, tone = 'text-zinc-300', block = false }) {
  if (value === null || value === undefined || value === '') return <span className="text-zinc-600">(empty)</span>;
  const structured = typeof value === 'object';
  const text = typeof value === 'string' ? value.replace(/\n$/, '') : structured ? JSON.stringify(value, null, 2) : String(value);
  if (block || structured || text.includes('\n') || text.length > 90) {
    return (
      <div className="relative group/val">
        <pre className={`max-h-60 overflow-auto rounded-md bg-black/30 border border-[#26262b] px-2.5 py-1.5 text-[11px] leading-relaxed whitespace-pre-wrap break-words ${tone}`}>{text}</pre>
        <span className="absolute top-1 right-1 opacity-0 group-hover/val:opacity-100 transition">
          <CopyButton text={text} />
        </span>
      </div>
    );
  }
  return <span className={`break-words ${tone}`}>{text}</span>;
}

function DetailRows({ values }) {
  const entries = Object.entries(values || {}).filter(([key]) => !HIDDEN_ARGUMENTS.has(key));
  if (!entries.length) return <span className="text-zinc-600">(none)</span>;
  return (
    <div className="space-y-1">
      {entries.map(([key, value]) => (
        <div key={key} className="grid grid-cols-[minmax(0,7rem)_minmax(0,1fr)] gap-2 items-baseline">
          <span className="text-zinc-500 truncate" title={key}>{key}</span>
          <DetailValue value={value} />
        </div>
      ))}
    </div>
  );
}

function DetailSection({ label, children, right }) {
  return (
    <div>
      <div className="flex items-center justify-between mb-1">
        <span className="text-[9.5px] uppercase tracking-wider text-zinc-500">{label}</span>
        {right}
      </div>
      {children}
    </div>
  );
}

// The highlighter the chat's code blocks use (lib/highlight), a separate chunk fetched the first
// time it is needed.
let highlighterPromise = null;
function loadHighlighter() {
  if (!highlighterPromise) highlighterPromise = import('../lib/highlight');
  return highlighterPromise;
}

// A terminal command as a command line, in shell colours (plain until the highlighter is in).
function ShellCommand({ command }) {
  const [html, setHtml] = useState(null);
  useEffect(() => {
    let cancelled = false;
    setHtml(null);
    loadHighlighter()
      .then((module) => !cancelled && setHtml(module.highlight(command, 'bash')))
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [command]);
  return (
    <pre className="max-h-60 overflow-auto rounded-md bg-black/40 border border-[#26262b] px-2.5 py-1.5 text-[11px] leading-relaxed whitespace-pre-wrap break-words text-zinc-100">
      <span className="text-emerald-400/80 select-none">$ </span>
      {html !== null ? <code className="hljs" dangerouslySetInnerHTML={{ __html: html }} /> : <code>{command}</code>}
    </pre>
  );
}

// What the bot asked for. A terminal command reads as a command line.
function StepArguments({ call }) {
  const args = parseMaybeJson(call.arguments || call.raw_arguments);
  if (args === null || args === undefined || args === '') return null;
  // Nothing the bot chose (no arguments, or only bookkeeping): no section.
  if (typeof args === 'object' && !Array.isArray(args) && !Object.keys(args).some((key) => !HIDDEN_ARGUMENTS.has(key))) return null;
  if (typeof args !== 'object' || Array.isArray(args)) {
    return (
      <DetailSection label="Arguments">
        <DetailValue value={args} />
      </DetailSection>
    );
  }
  if (typeof args.command === 'string') {
    const { command, ...rest } = args;
    const others = Object.fromEntries(Object.entries(rest).filter(([key]) => !HIDDEN_ARGUMENTS.has(key)));
    return (
      <DetailSection label="Command" right={<CopyButton text={command} />}>
        <ShellCommand command={command} />
        {Object.keys(others).length > 0 && (
          <div className="mt-1.5">
            <DetailRows values={others} />
          </div>
        )}
      </DetailSection>
    );
  }
  return (
    <DetailSection label="Arguments">
      <DetailRows values={args} />
    </DetailSection>
  );
}

// What came back. Terminal output shows its exit code, stdout and stderr.
function StepResult({ call }) {
  if (call.result_dropped) {
    return (
      <DetailSection label="Result">
        <span className="text-zinc-600">Not kept: this reply&apos;s results were too large to store together.</span>
      </DetailSection>
    );
  }
  const { value: result, trimmed } = parseResult(call.result);
  if (result === null || result === undefined || result === '') return null;
  const trimNote = trimmed > 0 && (
    <p className="mt-1 font-sans text-[10px] text-zinc-600">Kept up to the storage limit: the last {trimmed.toLocaleString()} characters were not stored.</p>
  );
  if (result && typeof result === 'object' && !Array.isArray(result) && ('stdout' in result || 'stderr' in result || 'exit_code' in result)) {
    const { stdout, stderr, exit_code: exitCode, ...rest } = result;
    return (
      <DetailSection
        label="Output"
        right={
          exitCode !== undefined && exitCode !== null ? (
            <span className={`text-[10px] px-1.5 rounded border ${exitCode === 0 ? 'text-emerald-300 border-emerald-500/30' : 'text-rose-300 border-rose-500/30'}`}>
              exit {exitCode}
            </span>
          ) : null
        }
      >
        <div className="space-y-1.5">
          {stdout ? <DetailValue value={String(stdout)} block /> : !stderr && <span className="text-zinc-600">(no output)</span>}
          {stderr && <DetailValue value={String(stderr)} tone="text-rose-200/90" block />}
          {Object.keys(rest).some((key) => !HIDDEN_ARGUMENTS.has(key)) && <DetailRows values={rest} />}
        </div>
        {trimNote}
      </DetailSection>
    );
  }
  return (
    <DetailSection label="Result">
      {result && typeof result === 'object' && !Array.isArray(result) ? <DetailRows values={result} /> : <DetailValue value={result} />}
      {trimNote}
    </DetailSection>
  );
}

// A small card on hovering a step: the whole description, how it went, how it was allowed.
// Positioned against the window, so the reply's edges cannot cut it off.
function StepHoverCard({ call, anchor }) {
  const rect = anchor.getBoundingClientRect();
  const below = rect.bottom + 180 < window.innerHeight;
  const style = {
    left: Math.max(8, Math.min(rect.left, window.innerWidth - 380)),
    ...(below ? { top: rect.bottom + 6 } : { bottom: window.innerHeight - rect.top + 6 }),
  };
  const how = approvalText(call);
  return (
    <div style={style} className="fixed z-50 w-[360px] rounded-xl border border-[#2e2e34] bg-[#16161a]/95 backdrop-blur-md shadow-2xl px-3 py-2.5 text-[11px] font-sans text-zinc-300 pointer-events-none animate-fade-in">
      <div className="flex items-center gap-2">
        <span className="font-mono text-zinc-100">{call.name}</span>
        <span className={`ml-auto font-mono text-[10px] ${TOOL_STATUS_STYLES[call.status] || 'text-zinc-500'}`}>{call.status}</span>
      </div>
      {call.summary && <p className="mt-1 leading-relaxed text-zinc-400 break-words">{stepSummary(call.summary)}</p>}
      {call.status === 'waiting' && call.waiting && <p className="mt-1 text-amber-300/90">{call.waiting}</p>}
      {call.error && <p className="mt-1 text-rose-300/90 break-words line-clamp-3">{call.error}</p>}
      {(how || Number.isFinite(call.ran_ms)) && (
        <p className="mt-1.5 pt-1.5 border-t border-[#26262b] text-[10px] text-zinc-500">
          {[how, Number.isFinite(call.ran_ms) ? `ran ${formatDuration(call.ran_ms)}` : ''].filter(Boolean).join(' · ')}
        </p>
      )}
      <p className="mt-1 text-[10px] text-zinc-600">Click for the full details</p>
    </div>
  );
}

// One step the bot took: the tool, what it did, how it went. Click to fold out what it
// was given and what came back.
function ToolStep({ call }) {
  const [open, setOpen] = useState(false);
  const [hover, setHover] = useState(null); // the row, while hovered long enough
  const timer = useRef(null);
  const rowRef = useRef(null);
  useEffect(() => () => clearTimeout(timer.current), []);
  const how = approvalText(call);
  return (
    <div className="min-w-0">
      <button
        ref={rowRef}
        type="button"
        onClick={() => {
          setOpen((v) => !v);
          setHover(null);
        }}
        onMouseEnter={() => {
          clearTimeout(timer.current);
          timer.current = setTimeout(() => setHover(rowRef.current), 350);
        }}
        onMouseLeave={() => {
          clearTimeout(timer.current);
          setHover(null);
        }}
        aria-expanded={open}
        className="w-full flex items-center gap-2 min-w-0 -mx-1 px-1 rounded text-left text-[10.5px] font-mono leading-5 hover:bg-[#1a1a1e] transition"
      >
        <FiChevronRight className={`flex-shrink-0 text-[10px] text-zinc-600 transition-transform ${open ? 'rotate-90' : ''}`} />
        <span className="flex-shrink-0 text-zinc-300">{call.name}</span>
        {call.summary && <span className="min-w-0 truncate text-zinc-500">{stepSummary(call.summary)}</span>}
        {call.written_as_text && (
          <span className="flex-shrink-0 text-amber-300/80">from its {call.written_as_text === 'thinking' ? 'thinking' : 'text'}</span>
        )}
        <span className={`flex-shrink-0 ml-auto ${TOOL_STATUS_STYLES[call.status] || 'text-zinc-500'}`}>{call.status}</span>
        {call.gate?.outcome === 'auto_approved' && <FiZap className="flex-shrink-0 text-[10px] text-emerald-400" aria-label="auto-approved" />}
        {call.trusted && <FiShield className="flex-shrink-0 text-[10px] text-emerald-400" aria-label="trusted" />}
      </button>
      {hover && !open && <StepHoverCard call={call} anchor={hover} />}
      {open && (
        <div className="mt-1 mb-2 ml-4 space-y-2.5 rounded-lg border border-[#26262b] bg-[#0e0e10] px-3 py-2.5 text-[11px] font-mono cursor-text">
          {call.status === 'waiting' && call.waiting && <p className="font-sans text-amber-300/90">{call.waiting}</p>}
          <StepArguments call={call} />
          {call.error && (
            <DetailSection label="Error">
              <DetailValue value={call.error} tone="text-rose-300" />
            </DetailSection>
          )}
          <StepResult call={call} />
          {(how || Number.isFinite(call.ran_ms) || call.called_as || call.written_as_text) && (
            <p className="pt-2 border-t border-[#1f1f24] font-sans text-[10px] text-zinc-500 leading-relaxed">
              {[
                how,
                Number.isFinite(call.ran_ms) ? `Ran ${formatDuration(call.ran_ms)}` : '',
                call.called_as ? `The model called it ${call.called_as}` : '',
                call.written_as_text
                  ? `The model wrote this call in its ${call.written_as_text === 'thinking' ? 'thinking' : 'answer text'} instead of making it; Cubicle ran it so it would not guess the result`
                  : '',
              ]
                .filter(Boolean)
                .join(' · ')}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

// The steps between two pieces of the reply. A long run shows its last few
// until opened, so the reply around it stays in view.
const STEPS_SHOWN = 4;
function ToolSteps({ calls }) {
  const [open, setOpen] = useState(false);
  const hidden = open ? 0 : Math.max(0, calls.length - STEPS_SHOWN);
  return (
    <div className="my-2.5 rounded-lg border border-[#26262b] bg-[#111113]/80 px-2.5 py-1.5">
      {hidden > 0 && (
        <button type="button" onClick={() => setOpen(true)} className="w-full text-left text-[10.5px] font-mono text-zinc-500 hover:text-zinc-300 leading-5">
          … {hidden} earlier step{hidden === 1 ? '' : 's'}
        </button>
      )}
      {calls.slice(hidden).map((call, index) => (
        <ToolStep key={call.id || `${call.name}-${index}`} call={call} />
      ))}
      {open && calls.length > STEPS_SHOWN && (
        <button type="button" onClick={() => setOpen(false)} className="text-[10.5px] font-mono text-zinc-500 hover:text-zinc-300 leading-5">
          show fewer
        </button>
      )}
    </div>
  );
}

// A reply's thinking, one entry per round that thought: {at, after, text}. `parts` are the
// stored (or live) reasoning_parts, each {at, after, start} into the whole `reasoning`; a
// reply stored before those were kept has one part, at the top.
function splitThoughts(reasoning, parts) {
  if (!reasoning) return [];
  if (!Array.isArray(parts) || !parts.length) return [{ at: 0, after: 0, text: reasoning }];
  return parts
    .map((part, index) => ({
      at: Number(part.at) || 0,
      after: Number(part.after) || 0,
      text: reasoning.slice(part.start || 0, index + 1 < parts.length ? parts[index + 1].start : reasoning.length),
    }))
    .filter((thought) => thought.text.trim());
}

// The reply in the order it happened: thinking, text, the steps taken, more
// thinking, more text. Each call and each round's thinking carries `at`, how
// much of the reply text came before it; at the same place, a round's thinking
// comes after the calls before it (`after` counts them). Calls with nothing
// written between them share one block.
function InlineReply({ text, calls, thoughts, isStreaming, level }) {
  const clamp = (at) => Math.max(0, Math.min(text.length, Number(at) || 0));
  const ordered = [
    ...calls.map((call, index) => ({ kind: 'call', call, at: clamp(call.at), order: 2 * index + 1, tie: index })),
    ...thoughts.map((thought, index) => ({ kind: 'thought', thought, index, at: clamp(thought.at), order: 2 * thought.after, tie: index })),
  ].sort((a, b) => a.at - b.at || a.order - b.order || a.tie - b.tie);
  const parts = [];
  let position = 0;
  ordered.forEach((item, index) => {
    const between = text.slice(position, item.at);
    if (between.trim()) parts.push({ kind: 'text', text: between });
    position = Math.max(position, item.at);
    const last = parts[parts.length - 1];
    if (item.kind === 'thought') {
      // Live while the model is still on it: nothing written or called after it yet.
      const live = isStreaming && index === ordered.length - 1 && !text.slice(item.at).trim();
      parts.push({ kind: 'thought', thought: item.thought, first: item.index === 0, live });
    } else if (last && last.kind === 'steps' && !between.trim()) {
      last.calls.push(item.call);
    } else {
      parts.push({ kind: 'steps', calls: [item.call] });
    }
  });
  const rest = text.slice(position);
  if (rest.trim()) parts.push({ kind: 'text', text: rest });
  return (
    <>
      {parts.map((part, index) => {
        if (part.kind === 'text') return <BotText key={`t${index}`} text={part.text} />;
        if (part.kind === 'steps') return <ToolSteps key={`s${index}`} calls={part.calls} />;
        return <ThinkingBlock key={`r${index}`} reasoning={part.thought.text} live={part.live} level={part.first ? level : null} />;
      })}
    </>
  );
}

function formatTokenCount(n) {
  if (!Number.isFinite(n)) return '?';
  return n < 1000 ? String(n) : `${(n / 1000).toFixed(1)}k`;
}

function MessageItem({ message, botId, onEdit }) {
  const isUser = message.sender === 'user';
  const isError = message.isError || message.text?.toLowerCase().startsWith('error:');
  const formattedTime = formatMsgTime(message.created_at);
  // Live streams carry `reasoning`; persisted history carries it in raw_payload.
  const reasoning = message.reasoning || message.raw_payload?.reasoning || '';
  const toolCalls = message.toolCalls || message.raw_payload?.tool_calls || [];
  // Calls that know where in the reply they came are drawn there; older
  // messages (stored before positions were kept) list them on top.
  const placed = toolCalls.length > 0 && toolCalls.every((call) => Number.isFinite(call.at));
  // The thinking, one block per round, placed in the reply like the calls.
  const thoughts = splitThoughts(reasoning, message.reasoningParts || message.raw_payload?.reasoning_parts);
  const thinkingLevel = message.thinking || message.raw_payload?.thinking;
  const attachments = message.attachments || message.raw_payload?.attachments || [];
  const usage = message.usage || message.raw_payload?.usage || null;

  if (isUser) {
    return (
      <div className="group flex justify-end items-start gap-1.5 my-1.5">
        {onEdit && (
          <button
            type="button"
            onClick={() => onEdit(message)}
            className="mt-1.5 p-1 rounded-md text-zinc-500 hover:text-white hover:bg-[#1f1f23] opacity-0 group-hover:opacity-100 focus:opacity-100 transition"
            title="Edit: take this message back (and everything after it) to change and resend it. /redo undoes this."
            aria-label="Edit this message"
          >
            <FiEdit2 className="text-xs" />
          </button>
        )}
        <div className="dark-bubble-user px-3.5 py-2 text-xs font-sans max-w-md shadow-md">
          {message.image_url && (
            <img
              src={message.image_url}
              alt="Uploaded image attachment"
              className="max-w-full max-h-56 rounded-lg object-cover border border-white/10 mb-1.5"
            />
          )}
          {attachments.length > 0 && (
            <div className="space-y-1.5 mb-1.5 min-w-[240px]">
              {attachments.map((attachment) => (
                <FileCard key={`${attachment.source}:${attachment.path}`} attachment={attachment} botId={botId || message.bot_id} />
              ))}
            </div>
          )}
          <div className="flex justify-end gap-3">
            <span className="min-w-0 whitespace-pre-wrap break-words [overflow-wrap:anywhere]">
              {message.raw_payload?.scheduled && (
                <span className="block text-[10px] font-mono text-zinc-300/70 mb-0.5" title={message.raw_payload.scheduled.cadence || ''}>
                  scheduled · {message.raw_payload.scheduled.name}
                  {message.raw_payload.scheduled.check ? ' · the check reported something' : ''}
                </span>
              )}
              {message.raw_payload?.demonstration && <DemonstrationChip demonstration={message.raw_payload.demonstration} />}
              {(() => {
                // What Cubicle looked up for the bot with this message (passages, procedures, earlier exchanges).
                const found = message.raw_payload?.turn_context || {};
                const parts = [
                  found.passages ? `${found.passages} passage${found.passages === 1 ? '' : 's'} from attachments` : '',
                  found.procedures ? `${found.procedures} procedure${found.procedures === 1 ? '' : 's'}` : '',
                  found.recalled ? `${found.recalled} earlier message${found.recalled === 1 ? '' : 's'}` : '',
                  found.log_entries ? `${found.log_entries} project log entr${found.log_entries === 1 ? 'y' : 'ies'}` : '',
                ].filter(Boolean);
                return parts.length ? (
                  <span className="block text-[10px] font-mono text-zinc-300/60 mb-0.5" title="Added after your message for the bot to read; not shown here">
                    looked up for the bot: {parts.join(' · ')}
                  </span>
                ) : null;
              })()}
              {message.raw_payload?.delegated && (
                <span className="block text-[10px] font-mono text-cyan-200/80 mb-0.5" title="Handed over by the orchestrator bot on your behalf">
                  from {message.raw_payload.delegated.from_name || 'the orchestrator'}
                </span>
              )}
              {message.raw_payload?.handback && (
                <span className="block text-[10px] font-mono text-emerald-200/80 mb-0.5" title={`The task: ${message.raw_payload.handback.task || ''}`}>
                  reply from {message.raw_payload.handback.from_name || 'a bot'}
                  {message.raw_payload.handback.status === 'failed' ? ' · failed' : ''} · finished after the wait
                </span>
              )}
              {message.text}
            </span>
            {formattedTime && (
              <span className="text-[10px] text-zinc-300/70 font-mono tracking-tight select-none flex-shrink-0 self-end ml-auto">
                {formattedTime}
              </span>
            )}
          </div>
        </div>
      </div>
    );
  }

  if (isError) {
    return (
      <div className="flex justify-start my-2">
        <div className="dark-bubble-error px-4 py-1.5 text-xs font-mono max-w-md flex items-center justify-between gap-3 shadow-sm">
          <div className="flex items-center gap-2 min-w-0">
            <FiX className="text-red-500 text-sm flex-shrink-0" />
            <span className={message.local ? 'whitespace-pre-wrap break-words' : 'truncate'}>{message.text}</span>
          </div>
          {formattedTime && (
            <span className="text-[10px] text-zinc-500 font-mono flex-shrink-0">{formattedTime}</span>
          )}
        </div>
      </div>
    );
  }

  // Notes the app itself adds to the thread (slash-command results). Not
  // persisted and never sent to the model.
  if (message.sender === 'system' && message.item_type === 'compaction') {
    const count = message.raw_payload?.messages_compacted;
    const manual = message.raw_payload?.reason === 'manual';
    return (
      <details className="my-3 mx-auto max-w-xl rounded-xl border border-sky-500/20 bg-sky-500/5 text-[11px] text-zinc-300 shadow-sm">
        <summary className="cursor-pointer select-none px-3 py-1.5 font-mono text-sky-300/90">
          Context compacted{count ? ` · ${count} messages summarized` : ''}{manual ? ' · /compact' : ''}{formattedTime ? ` · ${formattedTime}` : ''}
        </summary>
        <div className="px-3 pb-3 leading-relaxed whitespace-pre-wrap break-words">
          <ReactMarkdown>{message.text}</ReactMarkdown>
        </div>
      </details>
    );
  }

  // The user edited the bot's notes from the bot editor; the model sees the
  // new notes from this point of the conversation. After a rewind past a
  // change, the same row shows the bot its notes as they stand.
  if (message.sender === 'system' && message.item_type === 'memory') {
    return (
      <details className="my-3 mx-auto max-w-xl rounded-xl border border-amber-500/20 bg-amber-500/5 text-[11px] text-zinc-300 shadow-sm">
        <summary className="cursor-pointer select-none px-3 py-1.5 font-mono text-amber-300/90">
          {{
            rewind: 'Current notes shown to the bot after a rewind',
            start: 'The bot\'s notes, as this conversation started',
            elsewhere: 'Notes changed in another conversation',
          }[message.raw_payload?.by] || 'Notes edited'}
          {message.raw_payload?.by === 'elsewhere' ? '' : message.text ? ` · ${message.text.length} characters` : ' · cleared'}{formattedTime ? ` · ${formattedTime}` : ''}
        </summary>
        <div className="px-3 pb-3 leading-relaxed whitespace-pre-wrap break-words">
          {message.raw_payload?.by === 'elsewhere' ? 'The bot was told to read its notes again before relying on them.' : message.text || 'The notes are empty.'}
        </div>
      </details>
    );
  }

  // Work notes: those a conversation starts with or gets on joining a project, the
  // user's edit, the state after a rewind, or word that they changed elsewhere.
  if (message.sender === 'system' && message.item_type === 'work_notes') {
    const by = message.raw_payload?.by;
    const project = message.raw_payload?.project;
    const label = {
      start: `Work notes of ${project}, as this conversation started`,
      joined: `Moved into project ${project}`,
      left: `Moved out of project ${project}`,
      elsewhere: `${project}'s work notes changed in another conversation`,
      rewind: 'Current work notes shown to the bot after a rewind',
    }[by] || 'Work notes edited';
    return (
      <details className="my-3 mx-auto max-w-xl rounded-xl border border-amber-500/20 bg-amber-500/5 text-[11px] text-zinc-300 shadow-sm">
        <summary className="cursor-pointer select-none px-3 py-1.5 font-mono text-amber-300/90">
          {label}{formattedTime ? ` · ${formattedTime}` : ''}
        </summary>
        <div className="px-3 pb-3 leading-relaxed whitespace-pre-wrap break-words">
          {by === 'elsewhere' ? 'The bot was told to read them again before relying on them.' : message.text || '(empty)'}
        </div>
      </details>
    );
  }

  // The user edited the bot's to-do list from the card above the chat.
  if (message.sender === 'system' && message.item_type === 'todo') {
    const items = message.raw_payload?.items || [];
    const done = items.filter((i) => i.state === 'done').length;
    return (
      <details className="my-3 mx-auto max-w-xl rounded-xl border border-cyan-500/20 bg-cyan-500/5 text-[11px] text-zinc-300 shadow-sm">
        <summary className="cursor-pointer select-none px-3 py-1.5 font-mono text-cyan-300/90">
          {message.raw_payload?.by === 'rewind' ? 'Current to-do list shown to the bot after a rewind' : 'To-do list edited'}{items.length ? ` · ${done} of ${items.length} done` : ' · cleared'}{formattedTime ? ` · ${formattedTime}` : ''}
        </summary>
        <div className="px-3 pb-3 leading-relaxed whitespace-pre-wrap break-words font-mono">{message.text || '(empty)'}</div>
      </details>
    );
  }

  if (message.sender === 'system') {
    return (
      <div className="flex justify-center my-2">
        <div className="px-3 py-1.5 rounded-xl border border-[#27272a] bg-[#121214] text-[11px] font-mono text-zinc-400 max-w-xl whitespace-pre-wrap break-words shadow-sm">
          {message.text}
        </div>
      </div>
    );
  }

  return (
    <div className="flex justify-start my-2">
      <div className="dark-bubble-bot px-5 py-3 text-xs font-sans max-w-2xl w-full shadow-md text-zinc-100 leading-relaxed overflow-hidden">
        {message.isStreaming && message.waiting && (
          <div className="mb-2 flex items-center gap-2 text-[11px] text-zinc-400">
            <span className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse" />
            Waiting for a model slot
            {message.waiting.ahead > 0 ? ` · ${message.waiting.ahead} ahead` : ' · next in line'}
            {message.waiting.limit > 0 ? ` · ${message.waiting.limit} run at a time` : ''}
          </div>
        )}
        {placed || toolCalls.length === 0 ? (
          <InlineReply
            text={message.text || ''}
            calls={toolCalls}
            thoughts={thoughts}
            isStreaming={Boolean(message.isStreaming)}
            level={thinkingLevel}
          />
        ) : (
          <>
            {/* Stored before calls kept their place: the thinking and the calls on top. */}
            {thoughts.map((thought, index) => (
              <ThinkingBlock key={index} reasoning={thought.text} live={false} level={index === 0 ? thinkingLevel : null} />
            ))}
            <ToolCallList calls={toolCalls} />
            <BotText text={message.text} />
          </>
        )}
        {attachments.length > 0 && (
          <div className="mt-3 space-y-2">
            {attachments.map((attachment) => (
              <FileCard key={`${attachment.source}:${attachment.path}`} attachment={attachment} botId={botId || message.bot_id} />
            ))}
          </div>
        )}
        {(formattedTime || usage) && (
          <div className="text-[10px] text-zinc-400 text-right mt-1 font-mono tracking-tight select-none flex items-center justify-end gap-2">
            {usage && (
              <span
                className="text-zinc-600"
                title={`${(usage.prompt_tokens || 0).toLocaleString()} prompt + ${(usage.completion_tokens || 0).toLocaleString()} completion tokens${usage.rounds > 1 ? ` over ${usage.rounds} rounds` : ''}`}
              >
                {formatTokenCount((usage.prompt_tokens || 0) + (usage.completion_tokens || 0))} tok
              </span>
            )}
            {formattedTime && <span>{formattedTime}</span>}
          </div>
        )}
      </div>
    </div>
  );
}

// The thread re-renders on every streamed token and every keystroke in the
// composer. Messages that did not change keep their object identity, so this
// skips re-parsing their markdown each time.
export default React.memo(MessageItem);
