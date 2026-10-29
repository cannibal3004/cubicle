'use client';

import React, { useEffect } from 'react';
import { FiAlertTriangle } from 'react-icons/fi';

function Overlay({ onClose, children, labelledBy }) {
  useEffect(() => {
    const onKey = (event) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/60 backdrop-blur-sm p-4 overflow-y-auto"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
      role="dialog"
      aria-modal="true"
      aria-labelledby={labelledBy}
    >
      {children}
    </div>
  );
}

// A small in-page confirmation. The artifact viewer and many kiosks block
// window.confirm, so destructive actions confirm here instead.
export default function ConfirmDialog({ title, body, confirmLabel = 'Delete', busy = false, onConfirm, onCancel }) {
  return (
    <Overlay onClose={onCancel} labelledBy="confirm-title">
      <div className="w-full max-w-sm my-auto bg-[#111114] border border-[#26262b] rounded-2xl shadow-2xl p-5 text-zinc-200">
        <div className="flex items-start gap-3">
          <div className="w-9 h-9 rounded-xl bg-rose-500/10 border border-rose-500/30 flex items-center justify-center flex-shrink-0">
            <FiAlertTriangle className="text-rose-400" />
          </div>
          <div className="min-w-0">
            <h3 id="confirm-title" className="text-sm font-semibold text-white">
              {title}
            </h3>
            <p className="text-xs text-zinc-400 mt-1 leading-relaxed">{body}</p>
          </div>
        </div>
        <div className="flex justify-end gap-2 mt-5">
          <button
            type="button"
            onClick={onCancel}
            className="px-3 py-1.5 text-xs rounded-lg text-zinc-300 hover:text-white hover:bg-[#1f1f23] transition"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={busy}
            className="px-3 py-1.5 text-xs rounded-lg bg-rose-600 hover:bg-rose-500 text-white font-semibold disabled:opacity-50 transition"
          >
            {busy ? 'Working…' : confirmLabel}
          </button>
        </div>
      </div>
    </Overlay>
  );
}
