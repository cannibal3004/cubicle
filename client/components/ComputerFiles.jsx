'use client';

import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  FiAlertCircle,
  FiChevronRight,
  FiDownload,
  FiEye,
  FiFile,
  FiFileText,
  FiFolder,
  FiFolderPlus,
  FiHardDrive,
  FiImage,
  FiArchive,
  FiRefreshCw,
  FiTrash2,
  FiUpload,
  FiX,
} from 'react-icons/fi';
import { deleteBrowsedFile, fetchSharedFile, listBrowsedFiles, makeBrowsedFolder, uploadBrowsedFiles } from '../lib/api';
import Markdown from './Markdown';
import CodeView from './CodeView';
import { hasNoExtension, isTextName, looksBinary } from '../lib/codeLanguages';

const ROOTS = [
  { id: 'computer', label: '/workspace', hint: "The bot's computer" },
  { id: 'workspace', label: '/shared', hint: 'The shared folder' },
];

function formatSize(bytes) {
  if (!Number.isFinite(bytes)) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function formatWhen(iso) {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function kindOf(mime = '', name = '') {
  if (mime.startsWith('image/')) return 'image';
  if (mime === 'application/pdf') return 'pdf';
  if (mime.startsWith('text/') || isTextName(name) || /(json|xml|javascript|x-sh|x-python|yaml|toml|sql)/.test(mime)) return 'text';
  if (hasNoExtension(name) && !mime.startsWith('application/x-executable')) return 'text';
  if (/zip|tar|gzip|x-7z|rar/.test(mime) || /\.(zip|tar|gz|tgz|7z|rar)$/i.test(name)) return 'archive';
  return 'file';
}

// A text preview, or a note that the file is binary after all.
function readText(text) {
  return looksBinary(text) ? { text: '', binary: true } : { text };
}

const ICONS = { image: FiImage, pdf: FiFileText, text: FiFileText, archive: FiArchive, file: FiFile };

// A file browser for the two folders a bot can reach: its computer's
// /workspace and the shared folder. Works on the host copy, so it needs no
// running computer. Uploads land as plain files: the bot is not told, and
// nothing is extracted or attached to the chat.
// `initialPath` ({path, nonce}) opens a folder of the computer's, again each time it is asked for
// (a project's folder, from its page).
export default function ComputerFiles({ bot, initialPath }) {
  const botId = bot?.id;
  const [source, setSource] = useState('computer');
  const [path, setPath] = useState('');
  const [listing, setListing] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [uploading, setUploading] = useState(0);
  const [dragging, setDragging] = useState(false);
  const [confirming, setConfirming] = useState(null); // entry path awaiting delete confirmation
  const [newFolder, setNewFolder] = useState(null); // null, or the name being typed
  const [preview, setPreview] = useState(null); // { name, kind, url, text }
  const [showSource, setShowSource] = useState(false);
  const inputRef = useRef(null);

  const load = useCallback(
    async (nextSource = source, nextPath = path) => {
      setLoading(true);
      setError('');
      try {
        const data = await listBrowsedFiles(nextSource, nextPath, nextSource === 'computer' ? botId : '');
        setListing(data);
        setPath(data.path || '');
      } catch (err) {
        setError(err?.message || 'Could not list the folder.');
      } finally {
        setLoading(false);
      }
    },
    [source, path, botId]
  );

  const pendingPath = useRef(initialPath?.path || '');
  const seenRequest = useRef(initialPath?.nonce);
  useEffect(() => {
    setPath('');
    setListing(null);
    setConfirming(null);
    const start = pendingPath.current;
    pendingPath.current = '';
    load(source, start);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [source, botId]);
  useEffect(() => {
    if (!initialPath?.path || initialPath.nonce === seenRequest.current) return;
    seenRequest.current = initialPath.nonce;
    if (source === 'computer') {
      load('computer', initialPath.path);
    } else {
      pendingPath.current = initialPath.path;
      setSource('computer');
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialPath?.nonce]);

  useEffect(() => () => {
    if (preview?.url) URL.revokeObjectURL(preview.url);
  }, [preview]);

  const open = (entryPath) => {
    setConfirming(null);
    load(source, entryPath);
  };

  const upload = async (fileList) => {
    const files = Array.from(fileList || []).filter((f) => f && f.size >= 0);
    if (!files.length) return;
    setUploading(files.length);
    setError('');
    setNotice('');
    try {
      const result = await uploadBrowsedFiles(source, path, source === 'computer' ? botId : '', files);
      const count = result?.stored?.length || files.length;
      setNotice(`${count} file${count === 1 ? '' : 's'} uploaded to ${rootLabel()}${path ? `/${path}` : ''}.`);
      await load(source, path);
    } catch (err) {
      setError(err?.message || 'Upload failed.');
    } finally {
      setUploading(0);
      if (inputRef.current) inputRef.current.value = '';
    }
  };

  const remove = async (entry) => {
    setError('');
    try {
      await deleteBrowsedFile(source, entry.path, source === 'computer' ? botId : '');
      setConfirming(null);
      await load(source, path);
    } catch (err) {
      setError(err?.message || 'Could not delete it.');
    }
  };

  const createFolder = async () => {
    const name = (newFolder || '').trim();
    if (!name) {
      setNewFolder(null);
      return;
    }
    setError('');
    try {
      await makeBrowsedFolder(source, path, source === 'computer' ? botId : '', name);
      setNewFolder(null);
      await load(source, path);
    } catch (err) {
      setError(err?.message || 'Could not create the folder.');
    }
  };

  const attachmentFor = (entry) => ({ source, path: entry.path, name: entry.name, mime: entry.mime, size: entry.size });

  const download = async (entry) => {
    setError('');
    try {
      const blob = await fetchSharedFile(attachmentFor(entry), source === 'computer' ? botId : '');
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = entry.name;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10_000);
    } catch (err) {
      setError(err?.message || 'Download failed.');
    }
  };

  const openPreview = async (entry) => {
    setError('');
    const kind = kindOf(entry.mime || '', entry.name);
    try {
      const blob = await fetchSharedFile(attachmentFor(entry), source === 'computer' ? botId : '', { inline: true });
      if (kind === 'text') {
        setShowSource(false);
        setPreview({ name: entry.name, kind, ...readText(await blob.text()) });
      } else {
        const typed = blob.type ? blob : blob.slice(0, blob.size, entry.mime || 'application/octet-stream');
        setPreview({ name: entry.name, kind, url: URL.createObjectURL(typed) });
      }
    } catch (err) {
      setError(err?.message || 'Preview failed.');
    }
  };

  const rootLabel = () => ROOTS.find((r) => r.id === source)?.label || source;
  const crumbs = path ? path.split('/') : [];
  const entries = listing?.entries || [];
  const isMarkdown = /\.(md|markdown)$/i.test(preview?.name || '');

  return (
    <div
      className={`w-full h-full flex flex-col rounded-xl border bg-zinc-950 overflow-hidden ${dragging ? 'border-emerald-500/70 ring-1 ring-emerald-500/40' : 'border-zinc-800'}`}
      onDragOver={(event) => {
        event.preventDefault();
        if (!dragging) setDragging(true);
      }}
      onDragLeave={(event) => {
        if (event.currentTarget.contains(event.relatedTarget)) return;
        setDragging(false);
      }}
      onDrop={(event) => {
        event.preventDefault();
        setDragging(false);
        upload(event.dataTransfer?.files);
      }}
    >
      {/* Root switch, breadcrumb, actions */}
      <div className="flex items-center gap-2 px-3 py-2 border-b border-zinc-800 bg-zinc-900/60 text-xs">
        <div className="flex rounded-lg border border-zinc-800 overflow-hidden">
          {ROOTS.map((root) => (
            <button
              key={root.id}
              type="button"
              title={root.hint}
              onClick={() => setSource(root.id)}
              className={`px-2.5 py-1 font-mono text-[11px] transition ${source === root.id ? 'bg-[#27272a] text-white' : 'text-zinc-500 hover:text-zinc-300'}`}
            >
              {root.label}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-1 min-w-0 flex-1 font-mono text-[11px] text-zinc-400 overflow-x-auto">
          <button type="button" onClick={() => open('')} className="hover:text-white">{rootLabel()}</button>
          {crumbs.map((part, index) => {
            const target = crumbs.slice(0, index + 1).join('/');
            return (
              <React.Fragment key={target}>
                <FiChevronRight className="text-zinc-600 flex-shrink-0" />
                <button type="button" onClick={() => open(target)} className={`hover:text-white truncate ${index === crumbs.length - 1 ? 'text-zinc-200' : ''}`}>
                  {part}
                </button>
              </React.Fragment>
            );
          })}
        </div>
        <button type="button" onClick={() => load(source, path)} className="p-1.5 rounded-lg text-zinc-500 hover:text-white hover:bg-zinc-800 transition" title="Refresh">
          <FiRefreshCw className={loading ? 'animate-spin' : ''} />
        </button>
        <button type="button" onClick={() => setNewFolder('')} className="p-1.5 rounded-lg text-zinc-500 hover:text-white hover:bg-zinc-800 transition" title="New folder">
          <FiFolderPlus />
        </button>
        <button
          type="button"
          onClick={() => inputRef.current?.click()}
          disabled={uploading > 0}
          className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg bg-white text-black text-[11px] font-semibold hover:bg-zinc-200 disabled:opacity-50 transition"
        >
          <FiUpload /> {uploading ? `Uploading ${uploading}…` : 'Upload'}
        </button>
        <input ref={inputRef} type="file" multiple className="hidden" onChange={(event) => upload(event.target.files)} />
      </div>

      {(error || notice) && (
        <div className={`px-3 py-1.5 text-[11px] flex items-center gap-1.5 border-b border-zinc-800 ${error ? 'text-rose-300 bg-rose-500/10' : 'text-emerald-300 bg-emerald-500/10'}`}>
          {error ? <FiAlertCircle /> : null}
          <span className="flex-1">{error || notice}</span>
          <button type="button" onClick={() => { setError(''); setNotice(''); }} className="text-zinc-500 hover:text-white"><FiX /></button>
        </div>
      )}

      {/* Entries */}
      <div className="flex-1 overflow-y-auto">
        {newFolder !== null && (
          <div className="flex items-center gap-2 px-3 py-2 border-b border-zinc-800/60 bg-zinc-900/40">
            <FiFolder className="text-amber-400/80" />
            <input
              autoFocus
              value={newFolder}
              onChange={(event) => setNewFolder(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') createFolder();
                if (event.key === 'Escape') setNewFolder(null);
              }}
              placeholder="folder name"
              className="flex-1 bg-zinc-950 border border-zinc-700 rounded-lg px-2 py-1 text-[11px] font-mono text-zinc-100 focus:outline-none focus:border-zinc-500"
            />
            <button type="button" onClick={createFolder} className="text-[11px] text-emerald-300 hover:text-white">Create</button>
            <button type="button" onClick={() => setNewFolder(null)} className="text-[11px] text-zinc-500 hover:text-white">Cancel</button>
          </div>
        )}
        {listing && !listing.exists && !loading && (
          <p className="px-4 py-6 text-[11px] text-zinc-500 text-center">
            {path ? 'This folder does not exist.' : 'Nothing here yet. The folder is created the first time something is put in it.'}
          </p>
        )}
        {listing?.exists && !entries.length && !loading && (
          <p className="px-4 py-6 text-[11px] text-zinc-500 text-center">Empty folder. Drop files here to upload them.</p>
        )}
        {entries.map((entry) => {
          const isDir = entry.kind === 'dir';
          const kind = isDir ? 'dir' : kindOf(entry.mime || '', entry.name);
          const Icon = isDir ? FiFolder : ICONS[kind] || FiFile;
          const canPreview = !isDir && (kind === 'image' || kind === 'pdf' || (kind === 'text' && entry.size <= 512 * 1024));
          const confirmingThis = confirming === entry.path;
          return (
            <div key={entry.path} className="group flex items-center gap-3 px-3 py-1.5 border-b border-zinc-800/40 hover:bg-zinc-900/60 text-xs">
              <Icon className={`flex-shrink-0 ${isDir ? 'text-amber-400/80' : 'text-zinc-400'}`} />
              {isDir ? (
                <button type="button" onClick={() => open(entry.path)} className="min-w-0 flex-1 text-left text-zinc-100 truncate hover:text-white font-medium">
                  {entry.name}
                </button>
              ) : (
                <button
                  type="button"
                  onClick={() => (canPreview ? openPreview(entry) : download(entry))}
                  className="min-w-0 flex-1 text-left text-zinc-200 truncate hover:text-white"
                  title={canPreview ? 'Preview' : 'Download'}
                >
                  {entry.name}
                </button>
              )}
              <span className="w-16 text-right text-[10px] text-zinc-500 font-mono flex-shrink-0">{isDir ? '' : formatSize(entry.size)}</span>
              <span className="w-28 text-right text-[10px] text-zinc-500 flex-shrink-0 hidden md:block">{formatWhen(entry.modified)}</span>
              <div className="flex items-center gap-0.5 flex-shrink-0 w-24 justify-end">
                {confirmingThis ? (
                  <>
                    <span className="text-[10px] text-rose-300 mr-1">Delete?</span>
                    <button type="button" onClick={() => remove(entry)} className="px-1.5 py-0.5 rounded text-[10px] bg-rose-500/20 text-rose-200 hover:bg-rose-500/40">Yes</button>
                    <button type="button" onClick={() => setConfirming(null)} className="px-1.5 py-0.5 rounded text-[10px] text-zinc-400 hover:text-white">No</button>
                  </>
                ) : (
                  <>
                    {canPreview && (
                      <button type="button" onClick={() => openPreview(entry)} className="p-1 rounded text-zinc-500 hover:text-white opacity-0 group-hover:opacity-100 transition" title="Preview">
                        <FiEye />
                      </button>
                    )}
                    {!isDir && (
                      <button type="button" onClick={() => download(entry)} className="p-1 rounded text-zinc-500 hover:text-white opacity-0 group-hover:opacity-100 transition" title="Download">
                        <FiDownload />
                      </button>
                    )}
                    <button type="button" onClick={() => setConfirming(entry.path)} className="p-1 rounded text-zinc-500 hover:text-rose-300 opacity-0 group-hover:opacity-100 transition" title={isDir ? 'Delete (empty folders only)' : 'Delete'}>
                      <FiTrash2 />
                    </button>
                  </>
                )}
              </div>
            </div>
          );
        })}
        {listing?.truncated && <p className="px-4 py-2 text-[10px] text-amber-300">Only the first {entries.length} entries are shown.</p>}
      </div>

      <div className="px-3 py-1.5 border-t border-zinc-800 text-[10px] text-zinc-500 flex items-center gap-2">
        <FiHardDrive />
        {source === 'computer'
          ? "The bot's /workspace on this server. Uploads simply appear there; the bot is not told and nothing is attached to the chat."
          : 'The shared folder, which every computer sees at /shared.'}
      </div>

      {preview && (
        <div
          className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-6"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) setPreview(null);
          }}
        >
          <div className="relative max-w-5xl w-full max-h-full flex flex-col rounded-2xl border border-[#2b2b32] bg-[#0f0f12] shadow-2xl overflow-hidden">
            <div className="flex items-center justify-between px-4 py-2.5 border-b border-[#2b2b32] text-xs">
              <span className="font-semibold text-zinc-100 truncate">{preview.name}</span>
              <div className="flex items-center gap-2">
                {preview.kind === 'text' && isMarkdown && (
                  <button type="button" onClick={() => setShowSource((v) => !v)} className="px-2 py-1 rounded-lg text-[11px] text-zinc-400 hover:text-white hover:bg-[#26262b]">
                    {showSource ? 'Rendered' : 'Source'}
                  </button>
                )}
                <button type="button" onClick={() => setPreview(null)} className="p-1.5 rounded-lg text-zinc-400 hover:text-white hover:bg-[#26262b]" title="Close">
                  <FiX />
                </button>
              </div>
            </div>
            <div className="flex-1 min-h-0 overflow-auto p-4 flex items-start justify-center">
              {preview.kind === 'image' && <img src={preview.url} alt={preview.name} className="max-w-full max-h-[80vh] rounded-lg shadow-2xl" />}
              {preview.kind === 'pdf' && <iframe title={preview.name} src={preview.url} className="w-full h-[80vh] rounded-lg bg-white" />}
              {preview.kind === 'text' &&
                (isMarkdown && !showSource ? (
                  <div className="w-full text-xs text-zinc-100"><Markdown text={preview.text} /></div>
                ) : (
                  preview.binary ? (
                    <p className="text-xs text-zinc-400 py-10">This is a binary file, not text. Download it to open it with the right program.</p>
                  ) : (
                    <CodeView text={preview.text} name={preview.name} />
                  )
                ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
