'use client';

import React, { useEffect, useMemo, useState } from 'react';
import { FiCheck, FiCopy } from 'react-icons/fi';

import { languageFor } from '../lib/codeLanguages';

// The highlighter is a separate chunk, fetched the first time code is shown.
let highlighterPromise = null;
function loadHighlighter() {
  if (!highlighterPromise) highlighterPromise = import('../lib/highlight');
  return highlighterPromise;
}

const LABELS = {
  bash: 'Shell', python: 'Python', javascript: 'JavaScript', typescript: 'TypeScript', json: 'JSON', yaml: 'YAML', ini: 'INI / TOML',
  xml: 'HTML / XML', css: 'CSS', scss: 'SCSS', less: 'Less', markdown: 'Markdown', sql: 'SQL', pgsql: 'PostgreSQL', go: 'Go',
  rust: 'Rust', java: 'Java', kotlin: 'Kotlin', c: 'C', cpp: 'C++', csharp: 'C#', php: 'PHP', ruby: 'Ruby', dockerfile: 'Dockerfile',
  makefile: 'Makefile', powershell: 'PowerShell', dos: 'Batch', diff: 'Diff', plaintext: 'Text',
};

// Code or plain text: syntax colours for the file's language, line numbers,
// a wrap switch and a copy button. `language` overrides what the file name
// and first line suggest (a Markdown fence names its own). `compact` drops
// the toolbar and the numbers, for code inside a rendered document.
export default function CodeView({ text, name = '', language = null, compact = false }) {
  const source = String(text ?? '');
  const lang = language || languageFor(name, source);
  const [html, setHtml] = useState(null);
  const [wrap, setWrap] = useState(false);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setHtml(null);
    if (!lang || lang === 'plaintext') return undefined;
    loadHighlighter()
      .then((module) => {
        if (!cancelled) setHtml(module.highlight(source, lang));
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [source, lang]);

  const lineCount = useMemo(() => {
    const count = source.split('\n').length;
    return source.endsWith('\n') ? Math.max(1, count - 1) : count;
  }, [source]);
  const numbers = useMemo(() => Array.from({ length: lineCount }, (_, i) => i + 1).join('\n'), [lineCount]);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(source);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // the clipboard may be unavailable (http, permissions)
    }
  };

  const codeClass = `hljs block font-mono text-[11.5px] leading-[1.6] ${wrap ? 'whitespace-pre-wrap break-words' : 'whitespace-pre'}`;
  const code = html !== null
    ? <code className={codeClass} dangerouslySetInnerHTML={{ __html: html }} />
    : <code className={codeClass}>{source}</code>;

  if (compact) {
    return (
      <pre className="bg-[#141416] p-3 rounded-xl border border-[#2b2b32] text-zinc-300 overflow-x-auto my-3">{code}</pre>
    );
  }

  const showNumbers = !wrap;
  return (
    <div className="w-full min-w-0 flex flex-col rounded-xl border border-[#26262b] bg-[#0c0c0e] overflow-hidden">
      <div className="flex items-center justify-between gap-3 px-3 py-1.5 border-b border-[#1f1f23] text-[10px] font-mono text-zinc-500">
        <span>
          {LABELS[lang] || lang} · {lineCount.toLocaleString()} line{lineCount === 1 ? '' : 's'}
        </span>
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={() => setWrap((v) => !v)}
            className={`px-2 py-0.5 rounded-md transition ${wrap ? 'bg-[#26262b] text-white' : 'hover:text-zinc-200 hover:bg-[#1f1f23]'}`}
            title="Wrap long lines (hides line numbers)"
          >
            Wrap
          </button>
          <button type="button" onClick={copy} className="flex items-center gap-1 px-2 py-0.5 rounded-md hover:text-zinc-200 hover:bg-[#1f1f23] transition" title="Copy the whole file">
            {copied ? <FiCheck /> : <FiCopy />} {copied ? 'Copied' : 'Copy'}
          </button>
        </div>
      </div>
      <div className="overflow-auto max-h-[75vh] select-text">
        <div className={`flex ${wrap ? '' : 'min-w-max'}`}>
          {showNumbers && (
            <pre
              aria-hidden="true"
              className="sticky left-0 z-10 select-none text-right font-mono text-[11.5px] leading-[1.6] text-zinc-600 bg-[#0c0c0e] border-r border-[#1f1f23] pl-3 pr-3 py-3"
            >
              {numbers}
            </pre>
          )}
          <pre className={`flex-1 px-4 py-3 text-zinc-200 ${wrap ? 'min-w-0 max-w-full' : ''}`}>{code}</pre>
        </div>
      </div>
    </div>
  );
}
