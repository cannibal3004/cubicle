'use client';

import React, { useCallback, useState, useEffect, useRef } from 'react';
import Sidebar from './Sidebar';
import ChatWindow from './ChatWindow';
import ComputerPanel from './ComputerPanel';
import Marketplace from './Marketplace';
import AuditPanel from './AuditPanel';
import SettingsPage from './SettingsPage';
import BotEditorModal, { ConfirmDialog } from './BotEditorModal';

import {
  fetchBot,
  fetchBots,
  fetchModels,
  fetchChatHistory,
  fetchSettings,
  createBot,
  updateBot,
  deleteBot,
} from '../lib/api';
import { establishSession, fetchAuthStatus, setAuthFailureHandler, fetchTurnsOverview } from '../lib/api';
import LoginScreen from './LoginScreen';

export default function Dashboard() {
  const [bots, setBots] = useState([]);
  const [models, setModels] = useState([]);
  const [catalogError, setCatalogError] = useState(null);
  // 'checking' -> 'ready' | 'login'. Any later 401 flips back to 'login'.
  const [authState, setAuthState] = useState('checking');
  // The signed-in account (id, username, role), from the session check.
  const [currentUser, setCurrentUser] = useState(null);
  const [activeBotId, setActiveBotId] = useState('');
  const [activeTab, setActiveTab] = useState('chat'); // 'chat' | 'computer' | 'marketplace' | 'audit' | 'settings'
  // Transcripts are cached per bot so a reply that is still streaming for
  // one bot survives switching to another bot or another tab.
  const [messagesByBot, setMessagesByBot] = useState({});
  const [streamingBots, setStreamingBots] = useState({});
  // The server's view of running turns and pending approvals, by bot.
  const [turnStates, setTurnStates] = useState({});
  const lastTurnStates = useRef({});
  // Text to drop into the chat input, e.g. after handing a computer back.
  const [chatPrefill, setChatPrefill] = useState(null);
  // How the computer page should open: which tab, and whether maximized (a takeover).
  const [computerView, setComputerView] = useState({ tab: 'display', maximize: false, nonce: 0 });
  const openComputer = (tab = 'display', maximize = false) => {
    setComputerView({ tab, maximize, nonce: Date.now() });
    setActiveTab('computer');
  };
  // Bot editor: null, { mode: 'create' } or { mode: 'edit', bot }.
  const [botEditor, setBotEditor] = useState(null);
  const [botToDelete, setBotToDelete] = useState(null);
  const [defaultModel, setDefaultModel] = useState('');
  const [userName, setUserName] = useState('You');

  // Session check: a valid cookie goes straight in; otherwise the sign-in
  // page, or the setup page when no account exists yet.
  const toSignIn = () => {
    fetchAuthStatus()
      .then((status) => setAuthState(status.setup_required ? 'setup' : 'login'))
      .catch(() => setAuthState('login'));
  };
  useEffect(() => {
    setAuthFailureHandler(toSignIn);
    establishSession()
      .then((session) => {
        setCurrentUser(session?.user || null);
        setUserName(session?.user?.display_name || session?.user?.username || 'You');
        setAuthState('ready');
      })
      .catch(toSignIn);
    return () => setAuthFailureHandler(null);
  }, []);

  // Poll the server for turns that are running or waiting on an approval.
  // This is what lets another machine (or a reloaded page) see and attach
  // to work in progress, and what drives the sidebar indicators.
  useEffect(() => {
    if (authState !== 'ready') return undefined;
    let disposed = false;
    const poll = async () => {
      const overview = await fetchTurnsOverview();
      if (disposed) return;
      const next = {};
      for (const turn of overview.turns || []) next[turn.thread_id] = turn;
      // A turn that just finished (a scheduled run, or one driven from another
      // device) may have changed the bot's notes or to-do list: refresh that bot.
      const previous = lastTurnStates.current;
      lastTurnStates.current = next;
      for (const [threadId, turn] of Object.entries(previous)) {
        const now = next[threadId];
        if (turn.status === 'running' && (!now || now.status !== 'running')) {
          fetchBot(threadId)
            .then((fresh) => setBots((prev) => prev.map((b) => (b.id === threadId ? { ...b, ...fresh } : b))))
            .catch(() => {});
        }
      }
      setTurnStates((prev) => (JSON.stringify(prev) === JSON.stringify(next) ? prev : next));
    };
    poll();
    const interval = window.setInterval(poll, 4000);
    return () => {
      disposed = true;
      window.clearInterval(interval);
    };
  }, [authState]);

  // Initial Data Fetch
  useEffect(() => {
    if (authState !== 'ready') return;
    async function initData() {
      try {
        const [botsData, catalog, settingsData] = await Promise.all([fetchBots(), fetchModels(), fetchSettings()]);
        setBots(botsData);
        setModels(catalog.models);
        setCatalogError(catalog.error);
        if (settingsData?.default_model) {
          setDefaultModel(settingsData.default_model);
        }
        const firstVisible = botsData.find((b) => !b.archived) || botsData[0];
        if (firstVisible) {
          setActiveBotId(firstVisible.id);
        }
      } catch (err) {
        console.error('Initialization error:', err);
      }
    }
    initData();
  }, [authState]);

  // Reload the model list, bypassing the server cache (used after the
  // connection settings change or from the picker's refresh button).
  const refreshModels = async () => {
    const catalog = await fetchModels(true);
    setModels(catalog.models);
    setCatalogError(catalog.error);
    return catalog;
  };

  const setMessagesFor = useCallback((botId, updater) => {
    setMessagesByBot((prev) => {
      const current = prev[botId] || [];
      const next = typeof updater === 'function' ? updater(current) : updater;
      return { ...prev, [botId]: next };
    });
  }, []);

  const handleStreamingChange = useCallback((botId, value) => {
    setStreamingBots((prev) => (Boolean(prev[botId]) === Boolean(value) ? prev : { ...prev, [botId]: Boolean(value) }));
  }, []);

  const activeBotStreaming = Boolean(activeBotId && streamingBots[activeBotId]);

  // Fetch chat history whenever the active bot changes, and again once a
  // reply finishes so the cache picks up the persisted message ids. A bot
  // that is mid-reply keeps its live transcript.
  useEffect(() => {
    if (!activeBotId || activeBotStreaming) return;
    let cancelled = false;
    fetchChatHistory(activeBotId)
      .then((history) => {
        if (!cancelled) setMessagesFor(activeBotId, history);
      })
      .catch((err) => console.error('Failed to load history:', err));
    return () => {
      cancelled = true;
    };
  }, [activeBotId, activeBotStreaming, setMessagesFor]);

  const activeBot = bots.find((b) => b.id === activeBotId) || bots[0];

  const handleUpdateBotModel = async (botId, newModel) => {
    try {
      const updated = await updateBot(botId, { model: newModel });
      setBots((prev) => prev.map((b) => (b.id === botId ? updated : b)));
      // Keep defaultModel in sync whenever the active bot's model changes
      if (botId === activeBotId) {
        setDefaultModel(newModel);
      }
    } catch (err) {
      console.error('Failed to update bot model:', err);
    }
  };

  // When the active bot disappears from the list, move to the next visible one.
  const selectNextVisible = (list, removedId) => {
    if (activeBotId !== removedId) return;
    const next = list.find((b) => !b.archived && b.id !== removedId);
    setActiveBotId(next ? next.id : '');
    setActiveTab('chat');
  };

  // Any partial update to a bot (tool settings from the chat header, etc.).
  const handleUpdateBot = async (botId, updates) => {
    const updated = await updateBot(botId, updates);
    setBots((prev) => prev.map((b) => (b.id === updated.id ? updated : b)));
    return updated;
  };

  const handleSaveBot = async (values) => {
    if (botEditor?.mode === 'edit' && botEditor.bot) {
      const updated = await updateBot(botEditor.bot.id, values);
      setBots((prev) => prev.map((b) => (b.id === updated.id ? updated : b)));
      if (updated.id === activeBotId && values.model) setDefaultModel(values.model);
    } else {
      const created = await createBot(values);
      setBots((prev) => [...prev, created]);
      setActiveBotId(created.id);
      setActiveTab('chat');
    }
    setBotEditor(null);
  };

  const handleArchiveBot = async (bot, archived) => {
    const updated = await updateBot(bot.id, { archived });
    setBots((prev) => {
      const list = prev.map((b) => (b.id === updated.id ? updated : b));
      if (archived) selectNextVisible(list, bot.id);
      return list;
    });
    if (!archived) setActiveBotId(bot.id);
    setBotEditor(null);
  };

  const handleDeleteBot = async (bot) => {
    await deleteBot(bot.id);
    setBots((prev) => {
      const list = prev.filter((b) => b.id !== bot.id);
      selectNextVisible(list, bot.id);
      return list;
    });
    setMessagesByBot((prev) => {
      const next = { ...prev };
      delete next[bot.id];
      return next;
    });
    setBotToDelete(null);
    setBotEditor(null);
  };

  if (authState === 'login' || authState === 'setup') {
    return (
      <LoginScreen
        setupRequired={authState === 'setup'}
        onAuthenticated={(session) => {
          setCurrentUser(session?.user || null);
          setUserName(session?.user?.display_name || session?.user?.username || 'You');
          setAuthState('ready');
        }}
      />
    );
  }

  if (authState === 'checking') {
    return (
      <div className="h-screen w-screen flex items-center justify-center bg-[#09090b] text-zinc-500 text-xs font-sans select-none">
        Connecting to the API…
      </div>
    );
  }

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-[#09090b] text-zinc-100 font-sans">
      {/* Sidebar Navigation & Bot Roster */}
      <Sidebar
        bots={bots}
        activeBotId={activeBotId}
        userName={userName}
        onSelectBot={(id) => {
          setActiveBotId(id);
          setActiveTab('chat');
        }}
        activeTab={activeTab}
        onSelectTab={setActiveTab}
        turnStates={turnStates}
        onOpenSettings={() => setActiveTab((tab) => (tab === 'settings' ? 'chat' : 'settings'))}
        onOpenNewBot={() => setBotEditor({ mode: 'create' })}
        onEditBot={(bot) => setBotEditor({ mode: 'edit', bot })}
        onArchiveBot={(bot, archived) => handleArchiveBot(bot, archived).catch((err) => console.error('Archive failed:', err))}
        onDeleteBot={(bot) => setBotToDelete(bot)}
      />

      {/* Main Workspace Display Area */}
      <main className="flex-1 flex flex-col h-screen overflow-hidden relative">
        {/* Settings is a view like the others; the chat stays mounted beneath, so streams go on. */}
        {activeTab === 'settings' && (
          <SettingsPage
            user={currentUser}
            isOpen
            onClose={() => setActiveTab('chat')}
            currentModel={defaultModel}
            models={models}
            catalogError={catalogError}
            onRefreshModels={refreshModels}
            onConnectionSaved={async (saved) => {
              if (saved?.default_model) setDefaultModel(saved.default_model);
              await refreshModels();
            }}
            onUpdateDefaultModel={(newModel) => {
              setDefaultModel(newModel);
              if (activeBotId) {
                handleUpdateBotModel(activeBotId, newModel);
              }
            }}
            onProfileUpdate={(name) => setUserName(name || 'You')}
            onSignOut={() => {
              setActiveTab('chat');
              setAuthState('login');
            }}
          />
        )}
        {/* The chat stays mounted on every tab so the draft, the open SSE
            stream, approvals, and tool events survive a visit to the
            Computer tab. `contents` keeps it out of the layout when shown. */}
        <div className={activeTab === 'chat' ? 'contents' : 'hidden'}>
          <ChatWindow
            bot={activeBot}
            models={models}
            catalogError={catalogError}
            onRefreshModels={refreshModels}
            messages={activeBotId ? messagesByBot[activeBotId] || [] : []}
            setMessagesFor={setMessagesFor}
            streamingBots={streamingBots}
            onStreamingChange={handleStreamingChange}
            turnStates={turnStates}
            onUpdateBotModel={handleUpdateBotModel}
            onEditBot={() => activeBot && setBotEditor({ mode: 'edit', bot: activeBot })}
            onUpdateBot={handleUpdateBot}
            onBotPatched={(botId, patch) => setBots((prev) => prev.map((b) => (b.id === botId ? { ...b, ...patch } : b)))}
            onToggleComputer={() => openComputer('display')}
            onOpenFiles={() => openComputer('files')}
            onOpenTakeover={() => openComputer('display', true)}
            onOpenAudit={() => setActiveTab('audit')}
            onOpenSettings={() => setActiveTab('settings')}
            defaultModel={defaultModel}
            prefill={chatPrefill}
          />
        </div>

        {activeTab === 'computer' && (
          <ComputerPanel
            bot={activeBot}
            initial={computerView}
            onBackToChat={() => setActiveTab('chat')}
            onDemonstrated={({ recordingId, text }) => {
              // The user's words plus the recording; the server adds the steps and the bot's turn starts.
              setChatPrefill({ text, recordingId, nonce: Date.now(), send: true });
              setActiveTab('chat');
            }}
            onHandBack={() => {
              // Sent as a message, so the bot's next turn starts without a click.
              setChatPrefill({
                text: "I'm done on the computer and have handed control back to you. Please continue.",
                nonce: Date.now(),
                send: true,
              });
              setActiveTab('chat');
            }}
          />
        )}

        {activeTab === 'marketplace' && (
          <Marketplace onOpenSettings={() => setActiveTab('settings')} />
        )}

        {activeTab === 'audit' && <AuditPanel />}
      </main>

      {botEditor && (
        <BotEditorModal
          bot={botEditor.mode === 'edit' ? bots.find((b) => b.id === botEditor.bot.id) || botEditor.bot : null}
          models={models}
          catalogError={catalogError}
          onRefreshModels={refreshModels}
          defaultModel={defaultModel}
          onClose={() => setBotEditor(null)}
          onSave={handleSaveBot}
          onArchive={handleArchiveBot}
          onDelete={handleDeleteBot}
        />
      )}

      {botToDelete && (
        <ConfirmDialog
          title={`Delete ${botToDelete.name}?`}
          body="The bot, its whole conversation, and its computer are removed. This cannot be undone. Archiving keeps everything and just hides the bot."
          onCancel={() => setBotToDelete(null)}
          onConfirm={() => handleDeleteBot(botToDelete).catch((err) => console.error('Delete failed:', err))}
        />
      )}

    </div>
  );
}
