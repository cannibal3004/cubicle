'use client';

import React, { useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { FiX, FiHelpCircle, FiSearch, FiZoomIn } from 'react-icons/fi';
import { fetchHelpIndex, fetchHelpPage, searchHelp, helpImageUrl } from '../lib/api';

// GitHub's anchor for a heading: the docs link to sections this way (#letting-things-run-without-asking).
export function anchorOf(text) {
  return String(text)
    .trim()
    .toLowerCase()
    .replace(/[`*_]/g, '')
    .replace(/[^\w\- ]/g, '')
    .replace(/ /g, '-');
}

function textOf(children) {
  return React.Children.toArray(children)
    .map((child) => (typeof child === 'string' || typeof child === 'number' ? String(child) : textOf(child?.props?.children)))
    .join('');
}

// A link in a page, relative to that page: another page (and section), a section here, or the web.
function resolveLink(currentPath, href) {
  if (!href || /^[a-z][a-z0-9+.-]*:/i.test(href)) return { external: href };
  if (href.startsWith('/help')) {
    const [path, anchor] = href.replace(/^\/help\/?/, '').split('#');
    return { path: `${path || 'README'}.md`.replace(/\.md\.md$/, '.md'), anchor: anchor || '' };
  }
  if (href.startsWith('/')) return { external: href };
  const [target, anchor] = href.split('#');
  if (!target) return { path: currentPath, anchor: anchor || '' };
  const parts = currentPath.split('/').slice(0, -1);
  for (const piece of target.split('/')) {
    if (piece === '..') parts.pop();
    else if (piece && piece !== '.') parts.push(piece);
  }
  const path = parts.join('/');
  return path.endsWith('.md') ? { path, anchor: anchor || '' } : { external: href };
}

function heading(Tag, className) {
  return function Heading({ children }) {
    return (
      <Tag id={anchorOf(textOf(children))} className={`scroll-mt-6 ${className}`}>
        {children}
      </Tag>
    );
  };
}

// A picture opened from a page: as large as the window allows (the page shows it at about half its
// size, too small to read); a click on it shows it at full size, scrolling, when it is larger still.
// Escape or a click outside closes it.
function ImageViewer({ image, onClose }) {
  const [actual, setActual] = useState(false);
  useEffect(() => {
    const onKey = (event) => event.key === 'Escape' && onClose();
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);
  return (
    <div
      className={`fixed inset-0 z-50 bg-black/85 backdrop-blur-sm flex flex-col animate-fade-in ${actual ? 'overflow-auto' : 'items-center justify-center p-6'}`}
      onClick={onClose}
      role="dialog"
      aria-label={image.alt || 'Picture'}
    >
      <button type="button" onClick={onClose} className="fixed top-3 right-3 p-2 rounded-lg text-zinc-300 hover:text-white bg-black/50 hover:bg-black/70 transition" title="Close (Esc)">
        <FiX className="text-lg" />
      </button>
      <img
        src={image.src}
        alt={image.alt}
        onClick={(event) => {
          event.stopPropagation();
          setActual((value) => !value);
        }}
        className={`rounded-xl border border-[#26262b] shadow-2xl ${actual ? 'max-w-none flex-shrink-0 self-start mx-auto my-6 cursor-zoom-out' : 'max-w-full max-h-[calc(100vh-5rem)] object-contain cursor-zoom-in'}`}
      />
      {image.alt && !actual && <p className="mt-3 text-xs text-zinc-400 text-center">{image.alt}</p>}
    </div>
  );
}

// Cubicle's help pages (docs/): the index on the left with a search above it, the page on the right.
// Links between pages stay in here; each page has its own address (/help/<page>).
export default function HelpPage({ path = 'README.md', anchor = '', onNavigate, onClose }) {
  const [groups, setGroups] = useState(null);
  const [page, setPage] = useState(null);
  const [error, setError] = useState('');
  const [query, setQuery] = useState('');
  const [results, setResults] = useState(null);
  // The picture opened large: { src, alt } or null.
  const [zoomed, setZoomed] = useState(null);
  const bodyRef = useRef(null);

  useEffect(() => {
    fetchHelpIndex()
      .then((data) => setGroups(data.groups || []))
      .catch((err) => setError(err.message));
  }, []);

  useEffect(() => {
    let cancelled = false;
    setError('');
    fetchHelpPage(path)
      .then((data) => !cancelled && setPage(data))
      .catch((err) => !cancelled && setError(err.message));
    return () => {
      cancelled = true;
    };
  }, [path]);

  // After a page (or a section of it) is opened: to the section, or to the top.
  useEffect(() => {
    if (!page || !bodyRef.current) return;
    const target = anchor ? bodyRef.current.querySelector(`#${CSS.escape(anchor)}`) : null;
    if (target) target.scrollIntoView({ block: 'start' });
    else bodyRef.current.scrollTop = 0;
  }, [page, anchor]);

  useEffect(() => {
    const q = query.trim();
    if (q.length < 3) {
      setResults(null);
      return undefined;
    }
    const timer = window.setTimeout(() => {
      searchHelp(q)
        .then((data) => setResults(data.results || []))
        .catch(() => setResults([]));
    }, 300);
    return () => window.clearTimeout(timer);
  }, [query]);

  const open = (nextPath, nextAnchor = '') => onNavigate?.(nextPath, nextAnchor);

  const components = {
    h1: heading('h1', 'text-xl font-bold text-zinc-50 mb-4'),
    h2: heading('h2', 'text-base font-bold text-zinc-100 mt-8 mb-3 pt-4 border-t border-[#1f1f23]'),
    h3: heading('h3', 'text-sm font-semibold text-zinc-100 mt-6 mb-2'),
    p: ({ node, ...props }) => <p className="my-3 leading-relaxed" {...props} />,
    ul: ({ node, ...props }) => <ul className="my-3 ml-5 list-disc space-y-1.5" {...props} />,
    ol: ({ node, ...props }) => <ol className="my-3 ml-5 list-decimal space-y-1.5" {...props} />,
    li: ({ node, ...props }) => <li className="leading-relaxed pl-1" {...props} />,
    blockquote: ({ node, ...props }) => <blockquote className="my-3 pl-3 border-l-2 border-amber-500/40 text-zinc-400" {...props} />,
    table: ({ node, ...props }) => (
      <div className="my-4 overflow-x-auto rounded-xl border border-[#26262b]">
        <table className="w-full text-xs" {...props} />
      </div>
    ),
    th: ({ node, ...props }) => <th className="text-left font-semibold text-zinc-200 bg-[#141417] px-3 py-2 border-b border-[#26262b]" {...props} />,
    td: ({ node, ...props }) => <td className="align-top px-3 py-2 border-b border-[#1c1c20] text-zinc-300" {...props} />,
    pre: ({ node, ...props }) => <pre className="my-3 p-3 rounded-xl bg-[#0c0c0e] border border-[#26262b] overflow-x-auto text-[12px] leading-relaxed" {...props} />,
    code: ({ node, inline, className, children, ...props }) => {
      const isBlock = Boolean(className) || String(children).includes('\n');
      return isBlock ? (
        <code className="font-mono text-zinc-200" {...props}>{children}</code>
      ) : (
        <code className="font-mono text-[12px] px-1 py-0.5 rounded bg-[#1c1c20] text-cyan-200" {...props}>{children}</code>
      );
    },
    img: ({ node, src, alt }) => {
      const name = String(src || '').split('/').pop();
      const local = src && !/^[a-z]+:/i.test(src) && !src.startsWith('/');
      const url = local ? helpImageUrl(name) : src;
      return (
        <button
          type="button"
          onClick={() => setZoomed({ src: url, alt: alt || '' })}
          className="group relative my-4 block max-w-full cursor-zoom-in"
          title="Click to enlarge"
        >
          <img src={url} alt={alt || ''} loading="lazy" className="max-w-full rounded-xl border border-[#26262b] shadow-lg group-hover:border-zinc-500 transition" />
          <span className="absolute top-2 right-2 p-1.5 rounded-lg bg-black/60 text-zinc-200 opacity-0 group-hover:opacity-100 transition">
            <FiZoomIn className="text-sm" />
          </span>
        </button>
      );
    },
    a: ({ node, href, children }) => {
      const link = resolveLink(page?.path || path, href);
      if (link.external) {
        return (
          <a href={link.external} target="_blank" rel="noreferrer" className="text-cyan-300 underline underline-offset-2 hover:text-cyan-200">
            {children}
          </a>
        );
      }
      return (
        <a
          href={`/help/${link.path.replace(/\.md$/, '')}${link.anchor ? `#${link.anchor}` : ''}`}
          onClick={(event) => {
            event.preventDefault();
            open(link.path, link.anchor);
          }}
          className="text-cyan-300 underline underline-offset-2 hover:text-cyan-200"
        >
          {children}
        </a>
      );
    },
  };

  return (
    <section className="absolute inset-0 z-30 bg-[#0d0d10] flex flex-col animate-fade-in font-sans text-zinc-100">
      <div className="px-6 py-3.5 border-b border-[#1c1c20] flex items-center justify-between bg-[#09090b]/80 backdrop-blur-md">
        <div className="flex items-center gap-2.5 min-w-0">
          <FiHelpCircle className="text-amber-300" />
          <h2 className="text-sm font-bold text-zinc-100 tracking-wide truncate">Help{page && page.path !== 'README.md' ? ` · ${page.title}` : ''}</h2>
        </div>
        <button type="button" onClick={onClose} className="p-1 rounded-lg text-zinc-400 hover:text-white hover:bg-[#1c1c20] transition" title="Close the help">
          <FiX className="text-base" />
        </button>
      </div>

      <div className="flex-1 flex min-h-0">
        <nav className="w-60 flex-shrink-0 border-r border-[#1c1c20] p-3 overflow-y-auto select-none">
          <div className="relative mb-3">
            <FiSearch className="absolute left-2.5 top-1/2 -translate-y-1/2 text-xs text-zinc-500" />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search the help"
              className="w-full bg-[#18181b] border border-[#27272a] rounded-lg pl-7 pr-2 py-1.5 text-xs text-zinc-100 placeholder-zinc-500 focus:outline-none focus:border-zinc-500"
              aria-label="Search the help"
            />
          </div>
          {results ? (
            <div className="space-y-1">
              {!results.length && <p className="px-2 text-[11px] text-zinc-500">Nothing matches.</p>}
              {results.map((result) => (
                <button
                  key={result.link}
                  type="button"
                  onClick={() => {
                    const [, rest] = result.link.split('/help/');
                    const [target, section] = (rest || '').split('#');
                    open(`${target}.md`, section || '');
                  }}
                  className="w-full text-left px-2 py-1.5 rounded-lg hover:bg-[#1c1c20] transition"
                >
                  <div className="text-xs text-zinc-100">{result.title}</div>
                  <div className="text-[10.5px] text-zinc-500 line-clamp-2">{result.text}</div>
                </button>
              ))}
            </div>
          ) : (
            <div className="space-y-4">
              <button
                type="button"
                onClick={() => open('README.md')}
                className={`w-full text-left px-2 py-1 rounded-lg text-xs transition ${path === 'README.md' ? 'text-white bg-[#27272a]' : 'text-zinc-300 hover:bg-[#1c1c20]'}`}
              >
                Contents
              </button>
              {(groups || []).map((group) => (
                <div key={group.group}>
                  <div className="px-2 mb-1 text-[10px] uppercase tracking-wider text-zinc-500">{group.group}</div>
                  {group.pages.map((item) => (
                    <button
                      key={item.path}
                      type="button"
                      onClick={() => open(item.path)}
                      title={item.summary}
                      className={`w-full text-left px-2 py-1 rounded-lg text-xs transition ${
                        item.path === path ? 'text-white bg-[#27272a]' : 'text-zinc-400 hover:text-zinc-100 hover:bg-[#1c1c20]'
                      }`}
                    >
                      {item.title}
                    </button>
                  ))}
                </div>
              ))}
            </div>
          )}
        </nav>

        <div ref={bodyRef} className="flex-1 overflow-y-auto px-8 py-6">
          <article className="max-w-3xl text-[13px] text-zinc-300">
            {error && <p className="text-rose-300 text-xs">{error}</p>}
            {page && (
              <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
                {page.markdown}
              </ReactMarkdown>
            )}
          </article>
        </div>
      </div>
      {zoomed && <ImageViewer image={zoomed} onClose={() => setZoomed(null)} />}
    </section>
  );
}
