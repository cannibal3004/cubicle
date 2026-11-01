'use client';

import React, { useState } from 'react';
import { FiChevronDown, FiChevronUp, FiExternalLink, FiGlobe, FiMaximize2, FiRefreshCw } from 'react-icons/fi';

// The same sandbox the preview's own responses carry: the page gets an origin of its own and
// cannot act as the person viewing it.
const SANDBOX = 'allow-scripts allow-forms allow-popups allow-modals allow-downloads allow-pointer-lock allow-orientation-lock allow-presentation';

function until(iso) {
  if (!iso) return '';
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return '';
  const ended = when.getTime() < Date.now();
  const time = when.toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
  return ended ? `ended ${time}` : `until ${time}`;
}

// A web server running in the bot's computer, at a temporary address in Cubicle (computer_preview).
// Open puts it in a new tab; Show here frames it in the chat.
export default function PreviewCard({ attachment }) {
  const [shown, setShown] = useState(false);
  const [reload, setReload] = useState(0);
  const frameRef = React.useRef(null);
  const ended = attachment.expires_at && new Date(attachment.expires_at).getTime() < Date.now();

  const fullscreen = () => {
    const frame = frameRef.current;
    if (frame?.requestFullscreen) frame.requestFullscreen().catch(() => {});
  };

  return (
    <div className="rounded-xl border border-[#2b2b32] bg-[#141416]/80 overflow-hidden">
      <div className="flex items-center gap-3 px-3 py-2.5">
        <div className="w-9 h-9 rounded-lg bg-[#1f1f24] border border-[#2b2b32] flex items-center justify-center flex-shrink-0 text-cyan-300">
          <FiGlobe />
        </div>
        <div className="min-w-0 flex-1">
          <div className="text-xs font-semibold text-zinc-100 truncate">{attachment.name}</div>
          <div className="text-[10px] text-zinc-500 truncate">
            web preview · port {attachment.port}
            {attachment.expires_at ? ` · ${until(attachment.expires_at)}` : ''}
          </div>
        </div>
        <div className="flex items-center gap-1 flex-shrink-0">
          {!ended && (
            <button
              type="button"
              onClick={() => setShown((v) => !v)}
              className="flex items-center gap-1 px-2 py-1.5 rounded-lg text-[11px] text-zinc-300 hover:text-white hover:bg-[#26262b] transition"
            >
              {shown ? <FiChevronUp /> : <FiChevronDown />} {shown ? 'Hide' : 'Show here'}
            </button>
          )}
          <a
            href={attachment.url}
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg bg-white text-black text-[11px] font-semibold hover:bg-zinc-200 transition"
          >
            <FiExternalLink /> Open
          </a>
        </div>
      </div>
      {shown && !ended && (
        <div className="border-t border-[#2b2b32]">
          <div className="flex items-center justify-end gap-1 px-2 py-1 bg-[#101012]">
            <button type="button" onClick={() => setReload((n) => n + 1)} className="p-1 rounded text-zinc-500 hover:text-white" title="Reload">
              <FiRefreshCw className="text-[11px]" />
            </button>
            <button type="button" onClick={fullscreen} className="p-1 rounded text-zinc-500 hover:text-white" title="Full screen">
              <FiMaximize2 className="text-[11px]" />
            </button>
          </div>
          <iframe
            key={reload}
            ref={frameRef}
            src={attachment.url}
            title={attachment.name}
            sandbox={SANDBOX}
            allow="fullscreen; gamepad; autoplay"
            allowFullScreen
            className="w-full bg-white block resize-y"
            style={{ height: '28rem', minHeight: '12rem' }}
          />
        </div>
      )}
    </div>
  );
}
