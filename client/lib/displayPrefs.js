'use client';

import { useSyncExternalStore } from 'react';

// Display preferences of the signed-in account that many components read (the
// chat's messages) and one page changes (Settings): kept here so a change shows
// everywhere at once, without passing it down through every component.
let prefs = { showGenerationStats: false };
const listeners = new Set();

export function setDisplayPrefs(changes) {
  prefs = { ...prefs, ...changes };
  listeners.forEach((listener) => listener());
}

function subscribe(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useDisplayPrefs() {
  return useSyncExternalStore(subscribe, () => prefs, () => prefs);
}
