'use client';

import React, { useEffect, useState } from 'react';
import { FiDownload, FiEye, FiFile, FiFileText, FiImage, FiArchive, FiAlertCircle, FiX } from 'react-icons/fi';
import { fetchSharedFile } from '../lib/api';

function formatSize(bytes) {
  if (!Number.isFinite(bytes)) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function kindOf(mime = '', name = '') {
  if (mime.startsWith('image/')) return 'image';
  if (mime === 'application/pdf') return 'pdf';
  if (mime.startsWith('text/') || /\.(md|json|csv|yaml|yml|toml|log|py|js|ts|sh|html?)$/i.test(name)) return 'text';
  if (/zip|tar|gzip|x-7z|rar/.test(mime) || /\.(zip|tar|gz|tgz|7z|rar)$/i.test(name)) return 'archive';
  return 'file';
}

const ICONS = { image: FiImage, pdf: FiFileText, text: FiFileText, archive: FiArchive, file: FiFile };

// A file a bot handed to the user. Download saves it; Preview shows images,
// PDFs and text in a lightbox. Both go through the authenticated API.
export default function FileCard({ attachment, botId }) {
  const [busy, setBusy] = useState(null); // 'download' | 'preview'
  const [error, setError] = useState('');
  const [preview, setPreview] = useState(null); // { url, kind, text }

  const kind = kindOf(attachment.mime, attachment.name);
  const Icon = ICONS[kind] || FiFile;
  const canPreview = kind === 'image' || kind === 'pdf' || (kind === 'text' && attachment.size <= 512 * 1024);

  useEffect(() => () => {
    if (preview?.url) URL.revokeObjectURL(preview.url);
  }, [preview]);

  const download = async () => {
    setBusy('download');
    setError('');
    try {
      const blob = await fetchSharedFile(attachment, botId);
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = attachment.name;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10_000);
    } catch (err) {
      setError(err?.message || 'Download failed');
    } finally {
      setBusy(null);
    }
  };

  const open = async () => {
    setBusy('preview');
    setError('');
    try {
      const blob = await fetchSharedFile(attachment, botId, { inline: true });
      if (kind === 'text') {
        setPreview({ kind, text: await blob.text() });
      } else {
        const typed = blob.type ? blob : blob.slice(0, blob.size, attachment.mime);
        setPreview({ kind, url: URL.createObjectURL(typed) });
      }
    } catch (err) {
      setError(err?.message || 'Preview failed');
    } finally {
      setBusy(null);
    }
  };

  return (
    <>
      <div className="flex items-center gap-3 rounded-xl border border-[#2b2b32] bg-[#141416]/80 px-3 py-2.5">
        <div className="w-9 h-9 rounded-lg bg-[#1f1f24] border border-[#2b2b32] flex items-center justify-center flex-shrink-0 text-zinc-300">
          <Icon />
        </div>
        <div className="min-w-0 flex-1">
          <div className="text-xs font-semibold text-zinc-100 truncate" title={attachment.path}>
            {attachment.name}
          </div>
          <div className="text-[10px] text-zinc-500 truncate">
            {formatSize(attachment.size)}
            {attachment.mime ? ` · ${attachment.mime}` : ''}
            {` · ${attachment.source === 'computer' ? 'bot computer' : 'shared folder'}`}
          </div>
          {error && (
            <div className="text-[10px] text-rose-400 mt-0.5 flex items-center gap-1">
              <FiAlertCircle /> {error}
            </div>
          )}
        </div>
        <div className="flex items-center gap-1 flex-shrink-0">
          {canPreview && (
            <button
              type="button"
              onClick={open}
              disabled={Boolean(busy)}
              className="p-1.5 rounded-lg text-zinc-400 hover:text-white hover:bg-[#26262b] disabled:opacity-50 transition"
              title="Preview"
            >
              <FiEye />
            </button>
          )}
          <button
            type="button"
            onClick={download}
            disabled={Boolean(busy)}
            className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg bg-white text-black text-[11px] font-semibold hover:bg-zinc-200 disabled:opacity-50 transition"
          >
            <FiDownload /> {busy === 'download' ? 'Fetching…' : 'Download'}
          </button>
        </div>
      </div>

      {preview && (
        <div
          className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-6"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) setPreview(null);
          }}
          role="dialog"
          aria-modal="true"
          aria-label={`Preview of ${attachment.name}`}
        >
          <button
            type="button"
            onClick={() => setPreview(null)}
            className="absolute top-4 right-4 p-2 rounded-lg text-zinc-300 hover:text-white hover:bg-white/10 transition"
            title="Close"
          >
            <FiX />
          </button>
          {preview.kind === 'image' && (
            <img src={preview.url} alt={attachment.name} className="max-w-full max-h-full rounded-lg shadow-2xl" />
          )}
          {preview.kind === 'pdf' && (
            <iframe src={preview.url} title={attachment.name} className="w-full h-full max-w-5xl rounded-lg bg-white" />
          )}
          {preview.kind === 'text' && (
            <pre className="w-full max-w-4xl max-h-full overflow-auto rounded-lg bg-[#111114] border border-[#26262b] p-4 text-[11px] font-mono text-zinc-200 whitespace-pre-wrap">
              {preview.text}
            </pre>
          )}
        </div>
      )}
    </>
  );
}
