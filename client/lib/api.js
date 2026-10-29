// Behind the compose reverse proxy the API shares the page's origin and the
// client is built with NEXT_PUBLIC_API_URL=/api/v1. The absolute default is
// for `npm run dev` next to a locally running API.
const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL || 'http://127.0.0.1:8000/api/v1';

let sessionPromise = null;
let authFailureHandler = null;

// Authentication is cookie-based. On loopback without APP_AUTH_TOKEN the API
// hands out the session automatically; everywhere else the user signs in with
// the token once and the HttpOnly cookie carries the session. The token is
// never embedded in the client bundle.

// Dashboard registers a handler so a missing or expired session shows the
// login screen instead of silently returning empty data.
export function setAuthFailureHandler(handler) {
  authFailureHandler = handler;
}

export function resetSession() {
  sessionPromise = null;
}

function notifyAuthFailure(err) {
  if (authFailureHandler) authFailureHandler(err);
}

export async function establishSession() {
  if (typeof window === 'undefined') return null;
  if (!sessionPromise) {
    sessionPromise = fetch(`${API_BASE_URL}/auth/session`, { credentials: 'include' })
      .then((res) => {
        if (!res.ok) throw new Error('Authentication required');
        return res.json();
      })
      .catch((err) => {
        sessionPromise = null;
        notifyAuthFailure(err);
        throw err;
      });
  }
  return sessionPromise;
}

export async function fetchAuthStatus() {
  const res = await fetch(`${API_BASE_URL}/auth/status`, { credentials: 'include' });
  if (!res.ok) throw new Error('The API server is unreachable.');
  return res.json();
}

// Sign in or set up: the server answers 401 with a reason (invalid,
// totp_required, totp_invalid), 429 when the username is locked, 409 when
// setup state does not match. The thrown error carries `reason`.
async function authPost(path, body) {
  let res;
  try {
    res = await fetch(`${API_BASE_URL}${path}`, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch (err) {
    throw new Error('The API server is unreachable.');
  }
  if (!res.ok) {
    let detail = null;
    try {
      detail = (await res.json()).detail;
    } catch (err) {
      /* no body */
    }
    const message = typeof detail === 'string' ? detail : detail?.message || `Sign in failed (HTTP ${res.status}).`;
    const error = new Error(message);
    error.reason = typeof detail === 'object' && detail ? detail.reason : null;
    error.status = res.status;
    throw error;
  }
  const data = await res.json();
  sessionPromise = Promise.resolve(data);
  return data;
}

export async function loginWithPassword(username, password, code = null) {
  return authPost('/auth/login', { username, password, code: code || null });
}

export async function setupAccount(username, password) {
  return authPost('/auth/setup', { username, password });
}

async function accountJson(path, options = {}) {
  const res = await apiFetch(`${API_BASE_URL}${path}`, options);
  if (!res.ok) {
    let detail = null;
    try {
      detail = (await res.json()).detail;
    } catch (err) {
      /* no body */
    }
    throw new Error(typeof detail === 'string' ? detail : detail?.message || `Request failed (HTTP ${res.status})`);
  }
  return res.json();
}

const jsonBody = (body) => ({ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

export const fetchAccount = () => accountJson('/auth/me');
export const updateProfile = (displayName, email, about) =>
  accountJson('/auth/profile', jsonBody({ display_name: displayName, email, ...(about !== undefined ? { about } : {}) }));
export const changePassword = (currentPassword, newPassword) =>
  accountJson('/auth/password', jsonBody({ current_password: currentPassword, new_password: newPassword }));
export const startTotp = () => accountJson('/auth/totp/start', { method: 'POST' });
export const confirmTotp = (code) => accountJson('/auth/totp/confirm', jsonBody({ code }));
export const disableTotp = (password) => accountJson('/auth/totp/disable', jsonBody({ password }));
export const createApiToken = (name) => accountJson('/auth/tokens', jsonBody({ name }));
export const deleteApiToken = (id) => accountJson(`/auth/tokens/${encodeURIComponent(id)}`, { method: 'DELETE' });
export const signOutOtherSessions = () => accountJson('/auth/sessions/sign-out-others', { method: 'POST' });

// Admin: the accounts on this server.
export const fetchAdminUsers = () => accountJson('/admin/users');
export const createAdminUser = (username, password, role = 'member') => accountJson('/admin/users', jsonBody({ username, password, role }));
export const updateAdminUser = (id, changes) =>
  accountJson(`/admin/users/${encodeURIComponent(id)}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(changes) });

export async function logout() {
  try {
    await fetch(`${API_BASE_URL}/auth/logout`, { method: 'POST', credentials: 'include' });
  } finally {
    resetSession();
  }
}

async function apiFetch(url, options = {}) {
  await establishSession();
  const res = await fetch(url, { ...options, credentials: 'include' });
  if (res.status === 401) {
    resetSession();
    notifyAuthFailure(new Error('Authentication required'));
  }
  return res;
}

// Fetch a file a bot shared into the chat; the session cookie travels with
// it, so the result is a Blob the caller saves or previews.
export async function fetchSharedFile(attachment, botId, { inline = false } = {}) {
  const params = new URLSearchParams({
    source: attachment.source,
    path: attachment.path,
    bot_id: botId || '',
    ...(inline ? { inline: '1' } : {}),
  });
  const res = await apiFetch(`${API_BASE_URL}/files/download?${params.toString()}`);
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      detail = (await res.json()).detail || detail;
    } catch (err) {
      /* not JSON */
    }
    throw new Error(detail);
  }
  return res.blob();
}

// The file browser: one folder of the computer's /workspace or the shared folder.
async function browseCall(url, options = {}) {
  const res = await apiFetch(url, options);
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      detail = (await res.json()).detail || detail;
    } catch (err) {
      /* not JSON */
    }
    throw new Error(detail);
  }
  return res.json();
}

export function listBrowsedFiles(source, path, botId) {
  const params = new URLSearchParams({ source, path: path || '', bot_id: botId || '' });
  return browseCall(`${API_BASE_URL}/files/list?${params.toString()}`);
}

export function uploadBrowsedFiles(source, path, botId, files) {
  const body = new FormData();
  body.append('source', source);
  body.append('path', path || '');
  body.append('bot_id', botId || '');
  for (const file of files) body.append('files', file, file.name);
  return browseCall(`${API_BASE_URL}/files/upload`, { method: 'POST', body });
}

export function makeBrowsedFolder(source, path, botId, name) {
  const body = new FormData();
  body.append('source', source);
  body.append('path', path || '');
  body.append('bot_id', botId || '');
  body.append('name', name);
  return browseCall(`${API_BASE_URL}/files/folder`, { method: 'POST', body });
}

export function deleteBrowsedFile(source, path, botId) {
  const params = new URLSearchParams({ source, path, bot_id: botId || '' });
  return browseCall(`${API_BASE_URL}/files?${params.toString()}`, { method: 'DELETE' });
}

export async function fetchBots() {
  try {
    const res = await apiFetch(`${API_BASE_URL}/bots`);
    if (!res.ok) return [];
    return await res.json();
  } catch (err) {
    console.warn('Backend server offline or unreachable:', err);
    return [];
  }
}

export async function createBot(botData) {
  const res = await apiFetch(`${API_BASE_URL}/bots`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(botData),
  });
  if (!res.ok) throw new Error('Failed to create bot');
  return res.json();
}

export async function fetchBot(botId) {
  const res = await apiFetch(`${API_BASE_URL}/bots/${botId}`);
  if (!res.ok) throw new Error('Could not load the bot.');
  return res.json();
}

export async function updateBot(botId, updates) {
  const res = await apiFetch(`${API_BASE_URL}/bots/${botId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(updates),
  });
  if (!res.ok) throw new Error('Failed to update bot');
  return res.json();
}

// Starter prompts drafted by the model from the bot's prompt, notes and procedures (not saved).
export async function draftBotSuggestions(botId) {
  const res = await apiFetch(`${API_BASE_URL}/bots/${botId}/suggestions/draft`, { method: 'POST' });
  if (!res.ok) {
    let detail = '';
    try {
      detail = (await res.json()).detail;
    } catch {
      // not JSON
    }
    throw new Error(typeof detail === 'string' && detail ? detail : 'Could not draft suggestions.');
  }
  return (await res.json()).suggestions || [];
}

export async function deleteBot(botId) {
  const res = await apiFetch(`${API_BASE_URL}/bots/${botId}`, { method: 'DELETE' });
  if (!res.ok) throw new Error('Failed to delete bot');
  return res.json();
}

const EMPTY_CATALOG = { provider: '', base_url: '', source: 'fallback', error: null, models: [] };

// Returns { provider, base_url, source, error, models }. `refresh` bypasses
// the server-side cache after the connection settings change.
export async function fetchModels(refresh = false) {
  try {
    const res = await apiFetch(`${API_BASE_URL}/models${refresh ? '?refresh=true' : ''}`);
    if (!res.ok) return { ...EMPTY_CATALOG, error: `Model catalog request failed (HTTP ${res.status}).` };
    const data = await res.json();
    if (Array.isArray(data)) return { ...EMPTY_CATALOG, source: 'remote', models: data };
    return { ...EMPTY_CATALOG, ...data, models: Array.isArray(data?.models) ? data.models : [] };
  } catch (err) {
    console.warn('Models catalog API offline:', err);
    return { ...EMPTY_CATALOG, error: 'The API server is offline or unreachable.' };
  }
}

// ---- a bot's conversations -------------------------------------------------------------

async function conversationRequest(path, options, failure) {
  const res = await apiFetch(`${API_BASE_URL}${path}`, options);
  if (!res.ok) {
    let detail = '';
    try {
      detail = (await res.json()).detail;
    } catch {
      // not JSON
    }
    throw new Error(typeof detail === 'string' && detail ? detail : failure);
  }
  return res.json();
}

export function fetchConversations(botId) {
  return conversationRequest(`/bots/${encodeURIComponent(botId)}/conversations`, {}, 'Could not load the conversations.');
}

export function createConversation(botId, title = '', projectId = null) {
  return conversationRequest(
    `/bots/${encodeURIComponent(botId)}/conversations`,
    { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title, ...(projectId ? { project_id: projectId } : {}) }) },
    'Could not start a conversation.'
  );
}

// ---- a bot's projects ---------------------------------------------------------------------

export function fetchProjects(botId) {
  return conversationRequest(`/bots/${encodeURIComponent(botId)}/projects`, {}, 'Could not load the projects.');
}

export function createProject(botId, name, notes = '') {
  return conversationRequest(
    `/bots/${encodeURIComponent(botId)}/projects`,
    { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, notes }) },
    'Could not create the project.'
  );
}

export function updateProject(projectId, changes) {
  return conversationRequest(
    `/projects/${encodeURIComponent(projectId)}`,
    { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(changes) },
    'Could not update the project.'
  );
}

export function deleteProject(projectId) {
  return conversationRequest(`/projects/${encodeURIComponent(projectId)}`, { method: 'DELETE' }, 'Could not delete the project.');
}

export function fetchProjectLog(projectId, limit = 100) {
  return conversationRequest(`/projects/${encodeURIComponent(projectId)}/log?limit=${limit}`, {}, 'Could not load the log.');
}

export function fetchProject(projectId) {
  return conversationRequest(`/projects/${encodeURIComponent(projectId)}`, {}, 'Could not load the project.');
}

// Makes sure the project's folder exists in the bot's computer; resolves to {path} for the file viewer.
export function openProjectFolder(projectId) {
  return conversationRequest(`/projects/${encodeURIComponent(projectId)}/folder`, { method: 'POST' }, 'Could not open the project folder.');
}

// One conversation (any of the person's bots'), as a link to it is resolved.
export function fetchConversation(conversationId) {
  return conversationRequest(`/conversations/${encodeURIComponent(conversationId)}`, {}, 'Could not load the conversation.');
}

// The thinking levels to offer (the model's own) and the two Auto picks between; fetched once.
let thinkingLevelsPromise = null;
export function fetchThinkingLevels() {
  if (!thinkingLevelsPromise) {
    thinkingLevelsPromise = apiFetch(`${API_BASE_URL}/chat/thinking/levels`)
      .then((res) => (res.ok ? res.json() : null))
      .catch(() => null)
      .then((data) => {
        if (!data) thinkingLevelsPromise = null; // try again next time
        return data;
      });
  }
  return thinkingLevelsPromise;
}

export function updateConversation(conversationId, changes) {
  return conversationRequest(
    `/conversations/${encodeURIComponent(conversationId)}`,
    { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(changes) },
    'Could not update the conversation.'
  );
}

export function deleteConversation(conversationId) {
  return conversationRequest(`/conversations/${encodeURIComponent(conversationId)}`, { method: 'DELETE' }, 'Could not delete the conversation.');
}

export async function fetchChatHistory(threadId) {
  try {
    const res = await apiFetch(`${API_BASE_URL}/chat/history/${threadId}`);
    if (!res.ok) return [];
    return await res.json();
  } catch (err) {
    console.warn('Chat history API offline:', err);
    return [];
  }
}

export async function sendMessage(threadId, botId, text, model = null, imageUrl = null, attachments = null, recordingId = null) {
  try {
    const res = await apiFetch(`${API_BASE_URL}/chat/send`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        thread_id: threadId,
        bot_id: botId,
        user_text: text,
        model,
        image_url: imageUrl,
        attachments: attachments && attachments.length ? attachments : null,
        recording_id: recordingId || null,
      }),
    });
    if (!res.ok) throw new Error('Failed to send message');
    return await res.json();
  } catch (err) {
    console.warn('Send message API call error:', err);
    return { status: 'error', detail: err.message };
  }
}

export async function uploadImage(file) {
  const formData = new FormData();
  formData.append('file', file);
  const res = await apiFetch(`${API_BASE_URL}/upload`, {
    method: 'POST',
    body: formData,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Failed to upload image' }));
    throw new Error(err.detail || 'Failed to upload image');
  }
  return res.json();
}

// A file for the next message: saved where the bot can reach it, returned as
// the same card a shared file gets ({source, path, name, size, mime, ...}).
export async function uploadAttachment(file, botId) {
  const formData = new FormData();
  formData.append('file', file);
  formData.append('bot_id', botId || '');
  const res = await apiFetch(`${API_BASE_URL}/attachments`, { method: 'POST', body: formData });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Failed to upload the file' }));
    throw new Error(err.detail || 'Failed to upload the file');
  }
  return res.json();
}

// Built-in tool groups and every connected app's tools, for tool settings.
export async function fetchToolCatalog() {
  const res = await apiFetch(`${API_BASE_URL}/tools/catalog`);
  if (!res.ok) throw new Error('Failed to load the tool catalog');
  return res.json();
}

export async function fetchConnectorCatalog(refresh = false) {
  try {
    const res = await apiFetch(`${API_BASE_URL}/connectors/catalog${refresh ? '?refresh=1' : ''}`);
    if (!res.ok) return { cards: [], source: 'curated', configured: false };
    return await res.json();
  } catch (err) {
    console.warn('Connector catalog offline:', err);
    return { cards: [], source: 'curated', configured: false };
  }
}

export async function fetchConnectionStatus(slugs = []) {
  if (!slugs.length) return { services: {} };
  try {
    const res = await apiFetch(`${API_BASE_URL}/connectors?services=${encodeURIComponent(slugs.join(','))}`);
    if (!res.ok) return { services: {} };
    return await res.json();
  } catch (err) {
    console.warn('Connection status offline:', err);
    return { services: {} };
  }
}

export async function authorizeConnector(slug) {
  const res = await apiFetch(`${API_BASE_URL}/connectors/${slug}/authorize`, { method: 'POST' });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.error || err.detail || `Failed to authorize ${slug}`);
  }
  return res.json();
}

export async function disconnectConnector(slug) {
  const res = await apiFetch(`${API_BASE_URL}/connectors/${slug}`, { method: 'DELETE' });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.error || err.detail || `Failed to disconnect ${slug}`);
  }
  return res.json();
}

export async function fetchAuditEvents(limit = 100) {
  try {
    const res = await apiFetch(`${API_BASE_URL}/audit?limit=${encodeURIComponent(limit)}`);
    if (!res.ok) return [];
    return await res.json();
  } catch (err) {
    console.warn('Audit API offline:', err);
    return [];
  }
}

// Turns run on the server. Start one for the latest message; a 409 means a
// turn is already running and carries it, so callers attach instead.
export async function startTurn(threadId, model) {
  const res = await apiFetch(`${API_BASE_URL}/chat/turns/${encodeURIComponent(threadId)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ model: model || null }),
  });
  const payload = await res.json().catch(() => ({}));
  if (res.status === 409) return { busy: true, turn: payload.turn || null };
  if (!res.ok) throw new Error(payload.detail || 'Could not start the reply');
  return { busy: false, turn: payload.turn };
}

export async function fetchTurnStatus(threadId) {
  const res = await apiFetch(`${API_BASE_URL}/chat/turns/${encodeURIComponent(threadId)}`);
  if (!res.ok) return { turn: null };
  return res.json();
}

// Running turns and pending approvals for every bot (sidebar indicators).
// Semantic search over messages, attachment passages and notes.
export async function searchEverything(query, botId = '', limit = 10) {
  const params = new URLSearchParams({ q: query, bot_id: botId || '', limit: String(limit) });
  const res = await apiFetch(`${API_BASE_URL}/search?${params.toString()}`);
  if (!res.ok) throw new Error('Search is unavailable.');
  return res.json();
}

export async function fetchTurnsOverview() {
  try {
    const res = await apiFetch(`${API_BASE_URL}/chat/turns`);
    if (!res.ok) return { turns: [] };
    return await res.json();
  } catch (err) {
    return { turns: [] };
  }
}

export async function cancelTurn(threadId) {
  const res = await apiFetch(`${API_BASE_URL}/chat/turns/${encodeURIComponent(threadId)}/cancel`, { method: 'POST' });
  const payload = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(payload.detail || 'Could not cancel the reply');
  return payload;
}

// Follow a turn's events. The server numbers them and honours Last-Event-ID,
// so the browser's automatic reconnects resume where they left off. The
// caller closes the subscription on terminal events (turn.completed, etc.);
// otherwise the browser would keep reconnecting after the server ends it.
export function subscribeToChatStream(threadId, options, onEvent, onError) {
  const { turnId = null, after = 0 } = options || {};
  const params = new URLSearchParams();
  if (turnId) params.set('turn', turnId);
  if (after) params.set('after', String(after));
  const query = params.toString();
  const url = `${API_BASE_URL}/chat/stream/${encodeURIComponent(threadId)}${query ? `?${query}` : ''}`;
  let eventSource = null;
  let cancelled = false;

  establishSession()
    .then(() => {
      if (cancelled) return;
      eventSource = new EventSource(url, { withCredentials: true });

      eventSource.onmessage = (e) => {
        try {
          const data = JSON.parse(e.data);
          if (onEvent) onEvent(data);
        } catch (err) {
          console.warn('Failed to parse SSE payload:', err);
        }
      };

      eventSource.onerror = (err) => {
        // CONNECTING means the browser is retrying by itself with
        // Last-Event-ID; only a closed source is a real failure.
        if (eventSource && eventSource.readyState === EventSource.CLOSED) {
          if (onError && typeof onError === 'function') onError(err);
        }
      };
    })
    .catch((err) => {
      console.warn('Authentication or EventSource initialization error:', err);
      if (onError && typeof onError === 'function') {
        onError(err);
      }
    });

  return () => {
    cancelled = true;
    if (eventSource) {
      eventSource.close();
    }
  };
}

// Answer a question a bot asked mid-reply (an ask_user card).
export async function answerQuestion(requestId, answer, optionIndex = null) {
  const res = await apiFetch(`${API_BASE_URL}/questions/answer`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ request_id: requestId, answer, option_index: optionIndex }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Failed to send the answer' }));
    throw new Error(err.detail || 'Failed to send the answer');
  }
  return res.json();
}

export async function respondApproval(requestId, action) {
  const res = await apiFetch(`${API_BASE_URL}/approvals/respond`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ request_id: requestId, action }),
  });
  if (!res.ok) throw new Error('Failed to respond approval');
  return res.json();
}

// The auto-approval gate: probe a decision server, and read what it decided.
export async function checkDecider(deciderUrl = '') {
  const res = await apiFetch(`${API_BASE_URL}/gate/check`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ decider_url: deciderUrl }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data?.detail || 'Could not reach the decision server');
  return data;
}

export async function fetchGateDecisions(limit = 200) {
  const res = await apiFetch(`${API_BASE_URL}/gate/decisions?limit=${encodeURIComponent(limit)}`);
  if (!res.ok) throw new Error('Failed to load gate decisions');
  return res.json();
}

export async function fetchCommands() {
  const res = await apiFetch(`${API_BASE_URL}/commands`);
  if (!res.ok) throw new Error('Failed to load commands');
  return res.json();
}

// Undo and redo. A rewind removes one of the user's messages and everything
// after it (stopping a reply in progress); redo puts the last one back.
async function rewindCall(path, options = {}) {
  const res = await apiFetch(`${API_BASE_URL}${path}`, options);
  const payload = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(payload.detail || 'The conversation could not be changed');
  return payload;
}

export function previewRewind(threadId, messageId = null) {
  const query = messageId ? `?message_id=${encodeURIComponent(messageId)}` : '';
  return rewindCall(`/chat/rewind/${encodeURIComponent(threadId)}${query}`);
}

export function rewindThread(threadId, messageId = null) {
  return rewindCall(`/chat/rewind/${encodeURIComponent(threadId)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message_id: messageId }),
  });
}

export function fetchRedoState(threadId) {
  return rewindCall(`/chat/redo/${encodeURIComponent(threadId)}`);
}

export function redoThread(threadId) {
  return rewindCall(`/chat/redo/${encodeURIComponent(threadId)}`, { method: 'POST' });
}

export async function clearChatHistory(threadId) {
  const res = await apiFetch(`${API_BASE_URL}/chat/history/${encodeURIComponent(threadId)}`, { method: 'DELETE' });
  if (!res.ok) {
    let detail = '';
    try {
      detail = (await res.json()).detail || '';
    } catch (err) {
      detail = '';
    }
    throw new Error(detail || 'Failed to clear the conversation');
  }
  return res.json();
}

export async function compactThread(threadId) {
  const res = await apiFetch(`${API_BASE_URL}/chat/compact/${encodeURIComponent(threadId)}`, { method: 'POST' });
  if (!res.ok) {
    let detail = '';
    try {
      detail = (await res.json()).detail || '';
    } catch (err) {
      detail = '';
    }
    throw new Error(detail || 'Failed to compact the conversation');
  }
  return res.json();
}

// The gate in front of the model server: limit, running, waiting.
export async function fetchModelSlots() {
  const res = await apiFetch(`${API_BASE_URL}/models/slots`);
  if (!res.ok) throw new Error('Could not read the model slots.');
  return res.json();
}

export async function fetchSettings() {
  try {
    const res = await apiFetch(`${API_BASE_URL}/settings`);
    if (!res.ok) return null;
    return await res.json();
  } catch (err) {
    console.warn('Fetch settings API offline:', err);
    return null;
  }
}

export async function saveSettings(settingsData) {
  const res = await apiFetch(`${API_BASE_URL}/settings`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(settingsData),
  });
  if (!res.ok) throw new Error('Failed to save settings');
  return res.json();
}

export async function fetchComputerStatus(botId) {
  const res = await apiFetch(`${API_BASE_URL}/computers/${encodeURIComponent(botId)}`);
  if (!res.ok) throw new Error('Failed to load computer status');
  return res.json();
}

export async function fetchComputerHealth(botId) {
  const res = await apiFetch(`${API_BASE_URL}/computers/${encodeURIComponent(botId)}/health`);
  if (!res.ok) throw new Error('Failed to load computer health');
  return res.json();
}

export async function fetchComputerScreenshot(botId) {
  const res = await apiFetch(`${API_BASE_URL}/computers/${encodeURIComponent(botId)}/screenshot`);
  if (!res.ok) throw new Error('Failed to load computer screen state');
  return res.json();
}

async function runComputerLifecycleAction(botId, action) {
  const res = await apiFetch(`${API_BASE_URL}/computers/${encodeURIComponent(botId)}/${action}`, {
    method: 'POST',
  });
  if (!res.ok) {
    const error = await res.json().catch(() => ({}));
    throw new Error(error.detail || `Computer ${action} failed`);
  }
  return res.json();
}

export function createComputer(botId) {
  return runComputerLifecycleAction(botId, 'create');
}

export function startComputer(botId) {
  return runComputerLifecycleAction(botId, 'start');
}

export function pauseComputer(botId) {
  return runComputerLifecycleAction(botId, 'pause');
}

export function stopComputer(botId) {
  return runComputerLifecycleAction(botId, 'stop');
}

export function resetComputer(botId) {
  return runComputerLifecycleAction(botId, 'reset');
}

// Hand the sandbox desktop to the user ("user") or back to the bot ("bot").
export async function setComputerControl(botId, owner) {
  const res = await apiFetch(`${API_BASE_URL}/computers/${encodeURIComponent(botId)}/control`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ owner }),
  });
  const payload = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(payload.detail || 'Could not change computer control');
  return payload;
}

// WebSocket URL for the live desktop (noVNC). Same origin and cookie as the API.
// Demonstrations: record what you do in the bot's browser, then hand the
// recording to the bot with a message. Starting gives you control.
export async function startComputerRecording(botId) {
  const res = await apiFetch(`${API_BASE_URL}/computers/${encodeURIComponent(botId)}/record/start`, { method: 'POST' });
  const payload = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(payload.detail || 'Could not start recording');
  return payload;
}

export async function stopComputerRecording(botId) {
  const res = await apiFetch(`${API_BASE_URL}/computers/${encodeURIComponent(botId)}/record/stop`, { method: 'POST' });
  const payload = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(payload.detail || 'Could not stop recording');
  return payload;
}

export async function fetchComputerRecording(botId) {
  const res = await apiFetch(`${API_BASE_URL}/computers/${encodeURIComponent(botId)}/record`);
  if (!res.ok) throw new Error('Could not read the recording state');
  return res.json();
}

export function computerVncUrl(botId) {
  if (typeof window === 'undefined') return '';
  const base = API_BASE_URL.startsWith('http') ? new URL(API_BASE_URL) : new URL(API_BASE_URL, window.location.origin);
  const protocol = base.protocol === 'https:' ? 'wss:' : 'ws:';
  const path = base.pathname.replace(/\/$/, '');
  return `${protocol}//${base.host}${path}/computers/${encodeURIComponent(botId)}/vnc`;
}

export async function runComputerAction(botId, action, argumentsData = {}) {
  const res = await apiFetch(`${API_BASE_URL}/computers/${encodeURIComponent(botId)}/actions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action, arguments: argumentsData }),
  });
  const payload = await res.json().catch(() => ({}));
  if (!res.ok && res.status !== 202) {
    throw new Error(payload.detail || 'Computer action failed');
  }
  return payload;
}

export async function executeComputerAction(botId, requestId) {
  const res = await apiFetch(
    `${API_BASE_URL}/computers/${encodeURIComponent(botId)}/actions/${encodeURIComponent(requestId)}/execute`,
    { method: 'POST' },
  );
  const payload = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(payload.detail || 'Computer action execution failed');
  return payload;
}

// ---- secrets: names in, names out; values are write-only -------------------

async function secretsCall(path, options = {}) {
  const res = await apiFetch(`${API_BASE_URL}${path}`, options);
  if (!res.ok) {
    let detail = 'The request failed.';
    try {
      const body = await res.json();
      detail = typeof body?.detail === 'string' ? body.detail : detail;
    } catch (_) {
      // not JSON
    }
    throw new Error(detail);
  }
  return res.json();
}

export async function fetchSecrets() {
  return secretsCall('/secrets');
}

export async function setSecret(name, value) {
  return secretsCall(`/secrets/${encodeURIComponent(name)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ value }),
  });
}

export async function deleteSecret(name) {
  return secretsCall(`/secrets/${encodeURIComponent(name)}`, { method: 'DELETE' });
}

export async function fetchBotSchedules(botId) {
  return secretsCall(`/bots/${encodeURIComponent(botId)}/schedules`);
}

export async function createBotSchedule(botId, schedule) {
  return secretsCall(`/bots/${encodeURIComponent(botId)}/schedules`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(schedule),
  });
}

export async function updateBotSchedule(botId, scheduleId, changes) {
  return secretsCall(`/bots/${encodeURIComponent(botId)}/schedules/${encodeURIComponent(scheduleId)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(changes),
  });
}

export async function deleteBotSchedule(botId, scheduleId) {
  return secretsCall(`/bots/${encodeURIComponent(botId)}/schedules/${encodeURIComponent(scheduleId)}`, { method: 'DELETE' });
}

// ---- sharing a bot -----------------------------------------------------------------------------

export async function fetchBotMembers(botId) {
  return secretsCall(`/bots/${encodeURIComponent(botId)}/members`);
}

export async function addBotMember(botId, username, role = 'member') {
  return secretsCall(`/bots/${encodeURIComponent(botId)}/members`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, role }),
  });
}

export async function changeBotMember(botId, userId, role) {
  return secretsCall(`/bots/${encodeURIComponent(botId)}/members/${encodeURIComponent(userId)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ role }),
  });
}

// userId 'me' leaves a bot shared with you.
export async function removeBotMember(botId, userId) {
  return secretsCall(`/bots/${encodeURIComponent(botId)}/members/${encodeURIComponent(userId)}`, { method: 'DELETE' });
}

export async function fetchPeople() {
  return secretsCall('/people');
}

// What teammates' conversations with a shared bot may use; the owner also gets the choices.
export async function fetchShareSettings(botId) {
  return secretsCall(`/bots/${encodeURIComponent(botId)}/sharing`);
}

export async function saveShareSettings(botId, settings) {
  return secretsCall(`/bots/${encodeURIComponent(botId)}/sharing`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(settings),
  });
}

export async function runBotSchedule(botId, scheduleId) {
  return secretsCall(`/bots/${encodeURIComponent(botId)}/schedules/${encodeURIComponent(scheduleId)}/run`, { method: 'POST' });
}

// A bot's own MCP servers: commands run inside its computer, offered to that bot only.
export const fetchBotMcpServers = (botId) => secretsCall(`/mcp/bots/${encodeURIComponent(botId)}/servers`);
export const createBotMcpServer = (botId, server) =>
  secretsCall(`/mcp/bots/${encodeURIComponent(botId)}/servers`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(server) });
export const updateBotMcpServer = (botId, id, changes) =>
  secretsCall(`/mcp/bots/${encodeURIComponent(botId)}/servers/${encodeURIComponent(id)}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(changes) });
export const deleteBotMcpServer = (botId, id) => secretsCall(`/mcp/bots/${encodeURIComponent(botId)}/servers/${encodeURIComponent(id)}`, { method: 'DELETE' });
export const testBotMcpServer = (botId, id) => secretsCall(`/mcp/bots/${encodeURIComponent(botId)}/servers/${encodeURIComponent(id)}/test`, { method: 'POST' });

export async function fetchMcpServers() {
  return secretsCall('/mcp/servers');
}

export async function createMcpServer(server) {
  return secretsCall('/mcp/servers', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(server) });
}

export async function updateMcpServer(serverId, changes) {
  return secretsCall(`/mcp/servers/${encodeURIComponent(serverId)}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(changes) });
}

export async function deleteMcpServer(serverId) {
  return secretsCall(`/mcp/servers/${encodeURIComponent(serverId)}`, { method: 'DELETE' });
}

export async function testMcpServer(serverId) {
  return secretsCall(`/mcp/servers/${encodeURIComponent(serverId)}/test`, { method: 'POST' });
}

export async function reconnectMcpServer(serverId) {
  return secretsCall(`/mcp/servers/${encodeURIComponent(serverId)}/reconnect`, { method: 'POST' });
}

export async function testMcpDefinition(server) {
  return secretsCall('/mcp/test', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(server) });
}

export async function fetchBotSecrets(botId) {
  return secretsCall(`/bots/${encodeURIComponent(botId)}/secrets`);
}

export async function setBotSecret(botId, name, value) {
  return secretsCall(`/bots/${encodeURIComponent(botId)}/secrets/${encodeURIComponent(name)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ value }),
  });
}

export async function deleteBotSecret(botId, name) {
  return secretsCall(`/bots/${encodeURIComponent(botId)}/secrets/${encodeURIComponent(name)}`, { method: 'DELETE' });
}
