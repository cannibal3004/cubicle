'use client';

import React, { useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { FiX, FiChevronRight, FiTool, FiZap } from 'react-icons/fi';
import FileCard from './FileCard';

function formatMsgTime(createdAt) {
  if (!createdAt) return '';
  const d = new Date(createdAt);
  if (isNaN(d.getTime())) return '';
  return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true });
}

// Collapsible "Thinking" block shown above an assistant reply. While the model
// is still thinking (no answer text yet) it stays open and follows the stream;
// once the answer starts it collapses unless the user has opened it.
function ThinkingBlock({ reasoning, answerStarted, isStreaming }) {
  const [userToggled, setUserToggled] = useState(null);
  const bodyRef = useRef(null);

  const liveThinking = isStreaming && !answerStarted;
  const open = userToggled !== null ? userToggled : liveThinking;

  useEffect(() => {
    if (open && liveThinking && bodyRef.current) {
      bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
    }
  }, [reasoning, open, liveThinking]);

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
        {liveThinking && <span className="w-1.5 h-1.5 rounded-full bg-purple-400 animate-pulse" />}
        <span className="ml-auto font-mono text-[10px] text-zinc-500">{words} words</span>
      </button>
      {open && (
        <div
          ref={bodyRef}
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
          title={[call.error || call.summary || call.name, gateTitle(call.gate)].filter(Boolean).join('\n')}
          className="inline-flex items-center gap-1.5 rounded-lg border border-[#2b2b32] bg-[#141416]/70 px-2 py-1 text-[10px] font-mono text-zinc-300"
        >
          <FiTool className="text-[10px] text-zinc-500" />
          <span>{call.name}</span>
          <span className={TOOL_STATUS_STYLES[call.status] || 'text-zinc-500'}>{call.status}</span>
          {call.gate?.outcome === 'auto_approved' && <FiZap className="text-[10px] text-emerald-400" aria-label="auto-approved" />}
        </span>
      ))}
    </div>
  );
}

function formatTokenCount(n) {
  if (!Number.isFinite(n)) return '?';
  return n < 1000 ? String(n) : `${(n / 1000).toFixed(1)}k`;
}

function MessageItem({ message, botId }) {
  const isUser = message.sender === 'user';
  const isError = message.isError || message.text?.toLowerCase().startsWith('error:');
  const formattedTime = formatMsgTime(message.created_at);
  // Live streams carry `reasoning`; persisted history carries it in raw_payload.
  const reasoning = message.reasoning || message.raw_payload?.reasoning || '';
  const toolCalls = message.toolCalls || message.raw_payload?.tool_calls || [];
  const attachments = message.attachments || message.raw_payload?.attachments || [];
  const usage = message.usage || message.raw_payload?.usage || null;

  if (isUser) {
    return (
      <div className="flex justify-end my-1.5">
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
  // new notes from this point of the conversation.
  if (message.sender === 'system' && message.item_type === 'memory') {
    return (
      <details className="my-3 mx-auto max-w-xl rounded-xl border border-amber-500/20 bg-amber-500/5 text-[11px] text-zinc-300 shadow-sm">
        <summary className="cursor-pointer select-none px-3 py-1.5 font-mono text-amber-300/90">
          Notes edited{message.text ? ` · ${message.text.length} characters` : ' · cleared'}{formattedTime ? ` · ${formattedTime}` : ''}
        </summary>
        <div className="px-3 pb-3 leading-relaxed whitespace-pre-wrap break-words">{message.text || 'The notes are empty.'}</div>
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
          To-do list edited{items.length ? ` · ${done} of ${items.length} done` : ' · cleared'}{formattedTime ? ` · ${formattedTime}` : ''}
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
        {reasoning && (
          <ThinkingBlock
            reasoning={reasoning}
            answerStarted={Boolean(message.text)}
            isStreaming={Boolean(message.isStreaming)}
          />
        )}
        {toolCalls.length > 0 && <ToolCallList calls={toolCalls} />}
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          components={{
            p: ({ node, ...props }) => <div className="mb-2 last:mb-0 leading-relaxed" {...props} />,
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
          }}
        >
          {message.text}
        </ReactMarkdown>
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
