'use client';

import React from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

import CodeView from './CodeView';

// Rendered Markdown for documents (file previews), as opposed to the compact
// styling chat replies use in MessageItem. GitHub-flavoured: tables, task
// lists and strikethrough work, which reports lean on.
const components = {
  h1: ({ node, ...props }) => <h1 className="text-xl font-bold text-white mt-6 mb-3 first:mt-0" {...props} />,
  h2: ({ node, ...props }) => <h2 className="text-lg font-bold text-white mt-5 mb-2 first:mt-0 border-b border-[#26262b] pb-1" {...props} />,
  h3: ({ node, ...props }) => <h3 className="text-base font-semibold text-zinc-100 mt-4 mb-2 first:mt-0" {...props} />,
  h4: ({ node, ...props }) => <h4 className="text-sm font-semibold text-zinc-100 mt-3 mb-1.5" {...props} />,
  p: ({ node, ...props }) => <p className="mb-3 leading-relaxed text-zinc-200" {...props} />,
  a: ({ node, ...props }) => <a className="text-cyan-300 underline underline-offset-2 hover:text-cyan-200" target="_blank" rel="noreferrer" {...props} />,
  strong: ({ node, ...props }) => <strong className="font-semibold text-white" {...props} />,
  em: ({ node, ...props }) => <em className="italic text-zinc-200" {...props} />,
  del: ({ node, ...props }) => <del className="text-zinc-500" {...props} />,
  ul: ({ node, ...props }) => <ul className="list-disc pl-5 space-y-1 mb-3 text-zinc-200" {...props} />,
  ol: ({ node, ...props }) => <ol className="list-decimal pl-5 space-y-1 mb-3 text-zinc-200" {...props} />,
  li: ({ node, ...props }) => <li className="leading-relaxed" {...props} />,
  blockquote: ({ node, ...props }) => <blockquote className="border-l-2 border-[#3f3f46] pl-3 my-3 text-zinc-400 italic" {...props} />,
  hr: ({ node, ...props }) => <hr className="my-5 border-[#26262b]" {...props} />,
  img: ({ node, ...props }) => <img className="max-w-full rounded-lg border border-[#26262b] my-3" alt="" {...props} />,
  table: ({ node, ...props }) => (
    <div className="my-3 overflow-x-auto rounded-lg border border-[#26262b]">
      <table className="w-full text-left text-xs border-collapse" {...props} />
    </div>
  ),
  thead: ({ node, ...props }) => <thead className="bg-[#1a1a1e] text-zinc-100" {...props} />,
  th: ({ node, ...props }) => <th className="px-3 py-2 font-semibold border-b border-[#26262b] whitespace-nowrap" {...props} />,
  td: ({ node, ...props }) => <td className="px-3 py-1.5 border-b border-[#1f1f23] align-top text-zinc-200" {...props} />,
  input: ({ node, ...props }) => <input className="mr-1.5 accent-cyan-400 align-middle" {...props} />,
  pre: ({ node, children }) => <>{children}</>,
  code: ({ node, inline, className, children, ...props }) => {
    const isInline = inline || (!className && typeof children === 'string' && !children.includes('\n'));
    if (isInline) {
      return (
        <code className="bg-[#2a2a30] text-cyan-300 px-1.5 py-0.5 rounded font-mono text-[11px]" {...props}>
          {children}
        </code>
      );
    }
    // A fenced block: coloured for the language the fence names.
    const fence = /language-([\w+#.-]+)/.exec(className || '');
    return <CodeView compact text={String(children).replace(/\n$/, '')} language={fence ? fence[1].toLowerCase() : 'plaintext'} />;
  },
};

export default function Markdown({ text }) {
  return (
    <div className="text-sm font-sans">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {text || ''}
      </ReactMarkdown>
    </div>
  );
}
