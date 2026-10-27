'use client';

import { useEffect, useState } from 'react';
import { fetchToolCatalog } from './api';

// The tool catalog (built-in groups plus connected apps' tools) is slow to
// build on the server when apps are connected, so it is kept here for the
// whole page: shown at once from memory, refreshed in the background.
let cached = null;
let pending = null;
const listeners = new Set();

export function loadToolCatalog() {
  if (!pending) {
    pending = fetchToolCatalog()
      .then((data) => {
        cached = data;
        listeners.forEach((listener) => listener(data));
        return data;
      })
      .finally(() => {
        pending = null;
      });
  }
  return pending;
}

// Warm the catalog without waiting on it (a failure is retried on next use).
export function preloadToolCatalog() {
  if (!cached) loadToolCatalog().catch(() => {});
}

// The catalog, from memory when there is one; refreshed whenever `active`
// turns true, so a change made elsewhere shows up the next time it is opened.
export function useToolCatalog(active = true) {
  const [catalog, setCatalog] = useState(cached);
  const [error, setError] = useState('');

  useEffect(() => {
    listeners.add(setCatalog);
    return () => listeners.delete(setCatalog);
  }, []);

  useEffect(() => {
    if (!active) return;
    setError('');
    loadToolCatalog().catch((err) => {
      if (!cached) setError(err?.message || 'Could not load the tool list.');
    });
  }, [active]);

  return { catalog, error, loading: !catalog && !error };
}
