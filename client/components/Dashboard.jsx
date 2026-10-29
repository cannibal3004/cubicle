'use client';

import React, { useCallback, useState, useEffect, useRef } from 'react';
import Sidebar from './Sidebar';
import ChatWindow from './ChatWindow';
import ComputerPanel from './ComputerPanel';
import Marketplace from './Marketplace';
import AuditPanel from './AuditPanel';
import SettingsPage from './SettingsPage';
import BotSettingsPage from './BotSettingsPage';
import ConfirmDialog from './ConfirmDialog';

import {
  fetchBot,
  fetchBots,
  fetchModels,
  fetchChatHistory,
  fetchSettings,
  createBot,
  updateBot,
  deleteBot,
  removeBotMember,
  fetchConversations,
  createConversation,
  updateConversation,
  deleteConversation,
  sendMessage,
  startTurn,
  fetchProjects,
  createProject,
  updateProject,
  deleteProject,
} from '../lib/api';

// What the bot is asked in the conversation that sorts its notes into two tiers.
const SORT_NOTES_REQUEST = `Your notes now come in two tiers, and I'd like you to sort the ones you have.

1. Your notes (memory) are for durable facts about your job that every conversation needs: the systems you look after and how to reach them, access, house rules, how things are done here, lessons learned.
2. Work notes say where one piece of work stands: status, decisions, next steps. Each project has its own. What happened along the way goes in the project's log.

Please:
- Read your notes (memory_read) and your projects (projects_list).
- For each piece of ongoing work in your notes, create a project (project_create) with a short name, and give it work notes that say where it stands now (work_notes_write with project set to it). Move dated history about it into its log (project_log with project set to it), as a few entries rather than one long one. If you keep project folders or trackers in your workspace, use them too.
- Then rewrite your notes (memory_write) so they keep only the durable facts. Nothing should be lost: what is neither durable nor current goes in a project's log.
- Finish with a short summary: the projects you made and what stayed in your notes.`;

// Which conversation was open for each bot, remembered in this browser.
const THREADS_KEY = 'cubicle.conversationByBot';
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
  const [activeTab, setActiveTab] = useState('chat'); // 'chat' | 'computer' | 'marketplace' | 'audit' | 'settings' | 'bot'
  // Transcripts are cached per bot so a reply that is still streaming for
  // one bot survives switching to another bot or another tab.
  // Messages and streaming are kept per conversation (thread id; a bot's
  // first conversation has the bot's own id).
  const [messagesByBot, setMessagesByBot] = useState({});
  const [streamingBots, setStreamingBots] = useState({});
  // Each bot's conversations, most recently active first, and the one open for it.
  const [conversationsByBot, setConversationsByBot] = useState({});
  const [threadByBot, setThreadByBot] = useState({});
  useEffect(() => {
    try {
      const saved = JSON.parse(window.localStorage.getItem(THREADS_KEY) || '{}');
      if (saved && typeof saved === 'object') setThreadByBot(saved);
    } catch {
      // private window or blocked storage: start from each bot's latest conversation
    }
  }, []);
  const rememberThread = useCallback((botId, threadId) => {
    setThreadByBot((prev) => {
      const next = { ...prev, [botId]: threadId };
      try {
        window.localStorage.setItem(THREADS_KEY, JSON.stringify(next));
      } catch {
        // not kept across reloads, which is fine
      }
      return next;
    });
  }, []);
  // Each bot's projects (pieces of work with work notes and a log).
  const [projectsByBot, setProjectsByBot] = useState({});
  const loadProjects = useCallback((botId) => {
    if (!botId) return Promise.resolve([]);
    return fetchProjects(botId)
      .then((list) => {
        setProjectsByBot((prev) => ({ ...prev, [botId]: list }));
        return list;
      })
      .catch(() => []);
  }, []);
  const loadConversations = useCallback((botId) => {
    if (!botId) return Promise.resolve([]);
    return fetchConversations(botId)
      .then((list) => {
        setConversationsByBot((prev) => ({ ...prev, [botId]: list }));
        return list;
      })
      .catch(() => []);
  }, []);
  // The server's view of running turns and pending approvals, by bot.
  const [turnStates, setTurnStates] = useState({});
  // Approval cards from teammates' conversations on shared bots this person answers.
  const [waitingApprovals, setWaitingApprovals] = useState([]);
  const lastTurnStates = useRef({});
  const conversationsRef = useRef({});
  // Conversations whose first reply just finished: a model is naming them in
  // the background, so their bot's list is refreshed until the title arrives.
  const awaitingTitles = useRef({});
  // Bots that appeared on the server (an orchestrator's bots_create, another
  // device) join the list; known ones keep their loaded state.
  const adoptNewBots = useCallback(() => {
    fetchBots()
      .then((all) =>
        setBots((prev) => {
          const known = new Set(prev.map((b) => b.id));
          const added = (all || []).filter((b) => !known.has(b.id));
          // A bot a teammate stopped sharing leaves the list.
          const present = new Set((all || []).map((b) => b.id));
          const kept = prev.filter((b) => !b.shared || present.has(b.id));
          return added.length || kept.length !== prev.length ? [...kept, ...added] : prev;
        })
      )
      .catch(() => {});
  }, []);
  // Text to drop into the chat input, e.g. after handing a computer back.
  const [chatPrefill, setChatPrefill] = useState(null);
  // How the computer page should open: which tab, and whether maximized (a takeover).
  const [computerView, setComputerView] = useState({ tab: 'display', maximize: false, nonce: 0 });
  const openComputer = (tab = 'display', maximize = false) => {
    setComputerView({ tab, maximize, nonce: Date.now() });
    setActiveTab('computer');
  };
  // The bot settings page: null, or { mode: 'create' | 'edit', botId, section: {id, nonce} }. It stays
  // mounted (hidden) while another view is shown, so unsaved edits wait for you.
  const [botPage, setBotPage] = useState(null);
  const [botPageDirty, setBotPageDirty] = useState(false);
  // Another bot asked for while the page has unsaved edits: opened once those are discarded.
  const [pendingBotPage, setPendingBotPage] = useState(null);
  const openBotPage = (bot, sectionId) => {
    const next = { mode: bot ? 'edit' : 'create', botId: bot?.id || null, section: { id: sectionId || 'general', nonce: Date.now() } };
    if (botPage && botPageDirty && (botPage.mode !== next.mode || botPage.botId !== next.botId)) {
      setPendingBotPage(next);
      return;
    }
    setBotPage(next);
    setActiveTab('bot');
  };
  const closeBotPage = () => {
    setBotPage(null);
    setBotPageDirty(false);
    setActiveTab((tab) => (tab === 'bot' ? 'chat' : tab));
  };
  const [botToDelete, setBotToDelete] = useState(null);
  const [botToLeave, setBotToLeave] = useState(null);
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
      // A turn in a conversation this tab has not listed yet (a schedule's, another device's): list it.
      const known = new Set(Object.values(conversationsRef.current).flat().map((c) => c.id));
      for (const turn of overview.turns || []) {
        if (!known.has(turn.thread_id) && turn.bot_id) loadConversations(turn.bot_id);
      }
      // A turn that just finished (a scheduled run, or one driven from another
      // device) may have changed the bot's notes or to-do list: refresh that bot.
      const previous = lastTurnStates.current;
      lastTurnStates.current = next;
      let finished = false;
      for (const [threadId, turn] of Object.entries(previous)) {
        const now = next[threadId];
        if (turn.status === 'running' && (!now || now.status !== 'running')) {
          finished = true;
          const botId = turn.bot_id || threadId;
          fetchBot(botId)
            .then((fresh) => setBots((prev) => prev.map((b) => (b.id === botId ? { ...b, ...fresh } : b))))
            .catch(() => {});
          loadConversations(botId);
          loadProjects(botId);
          const conversation = (conversationsRef.current[botId] || []).find((c) => c.id === threadId);
          if (!conversation || !['generated', 'user', 'fixed'].includes(conversation.title_source)) {
            awaitingTitles.current[threadId] = { botId, until: Date.now() + 90000 };
          }
        }
      }
      if (finished) adoptNewBots(); // a turn may have created a bot
      const titleBots = new Set();
      for (const [threadId, wait] of Object.entries(awaitingTitles.current)) {
        const conversation = (conversationsRef.current[wait.botId] || []).find((c) => c.id === threadId);
        if (Date.now() > wait.until || (conversation && ['generated', 'user', 'fixed'].includes(conversation.title_source))) {
          delete awaitingTitles.current[threadId];
        } else if (!finished) {
          titleBots.add(wait.botId);
        }
      }
      titleBots.forEach((botId) => loadConversations(botId));
      setTurnStates((prev) => (JSON.stringify(prev) === JSON.stringify(next) ? prev : next));
      const waiting = overview.waiting || [];
      setWaitingApprovals((prev) => (JSON.stringify(prev) === JSON.stringify(waiting) ? prev : waiting));
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

  useEffect(() => {
    if (authState === 'ready' && activeBotId) {
      loadConversations(activeBotId);
      loadProjects(activeBotId);
    }
  }, [authState, activeBotId, loadConversations, loadProjects]);

  // The open conversation: the one remembered for the bot, else its latest
  // one the user started (scheduled runs and an orchestrator's tasks have
  // conversations of their own), else its first.
  const activeConversations = conversationsByBot[activeBotId] || [];
  const rememberedThread = threadByBot[activeBotId];
  const latestThread = (() => {
    const open = activeConversations.filter((c) => !c.archived);
    const typed = open.find((c) => !c.purpose) || open[0];
    // A shared bot's first conversation is its owner's: wait for the person's own.
    if (!typed && bots.find((b) => b.id === activeBotId)?.shared) return '';
    return typed ? typed.id : activeBotId;
  })();
  const activeThreadId = !activeBotId
    ? ''
    : rememberedThread && (!activeConversations.length || activeConversations.some((c) => c.id === rememberedThread))
      ? rememberedThread
      : latestThread;
  const activeConversation = activeConversations.find((c) => c.id === activeThreadId) || null;
  const activeProjects = projectsByBot[activeBotId] || [];
  const activeProject = activeConversation?.project_id ? activeProjects.find((p) => p.id === activeConversation.project_id) || null : null;

  const activeBotStreaming = Boolean(activeThreadId && streamingBots[activeThreadId]);

  // Fetch chat history whenever the open conversation changes, and again once
  // a reply finishes so the cache picks up the persisted message ids. A
  // conversation that is mid-reply keeps its live transcript.
  useEffect(() => {
    if (!activeThreadId || activeBotStreaming) return;
    let cancelled = false;
    fetchChatHistory(activeThreadId)
      .then((history) => {
        if (!cancelled) setMessagesFor(activeThreadId, history);
      })
      .catch((err) => console.error('Failed to load history:', err));
    return () => {
      cancelled = true;
    };
  }, [activeThreadId, activeBotStreaming, setMessagesFor]);

  // ---- conversations ------------------------------------------------------------------

  const ownerOf = (threadId) =>
    Object.keys(conversationsByBot).find((botId) => (conversationsByBot[botId] || []).some((c) => c.id === threadId)) || activeBotId;

  const mergeConversation = (botId, saved) =>
    setConversationsByBot((prev) => {
      const list = prev[botId] || [];
      return { ...prev, [botId]: list.some((c) => c.id === saved.id) ? list.map((c) => (c.id === saved.id ? { ...c, ...saved } : c)) : [saved, ...list] };
    });

  const handleSelectConversation = (botId, threadId) => {
    rememberThread(botId, threadId);
    setActiveBotId(botId);
    setActiveTab('chat');
  };

  const handleNewConversation = async (botId = activeBotId, projectId = null) => {
    if (!botId) return null;
    const created = await createConversation(botId, '', projectId);
    mergeConversation(botId, created);
    rememberThread(botId, created.id);
    setActiveBotId(botId);
    setActiveTab('chat');
    return created;
  };

  // From the chat: { todo } from a tool result, { refresh: true } after a first
  // message (the server names the conversation), or a saved edit ({ save: true }).
  const handleConversationPatched = async (threadId, patch, options = {}) => {
    const botId = ownerOf(threadId);
    if (patch?.refresh) {
      await loadConversations(botId);
      return null;
    }
    if (options.save) {
      const saved = await updateConversation(threadId, patch);
      mergeConversation(botId, saved);
      return saved;
    }
    setConversationsByBot((prev) => ({ ...prev, [botId]: (prev[botId] || []).map((c) => (c.id === threadId ? { ...c, ...patch } : c)) }));
    return null;
  };

  const handleRenameConversation = async (conversation, title) => {
    const saved = await updateConversation(conversation.id, { title });
    mergeConversation(conversation.bot_id, saved);
  };

  const handleArchiveConversation = async (conversation, archived) => {
    const saved = await updateConversation(conversation.id, { archived });
    mergeConversation(conversation.bot_id, saved);
    if (archived && conversation.id === activeThreadId) {
      const next = (conversationsByBot[conversation.bot_id] || []).find((c) => !c.archived && c.id !== conversation.id);
      rememberThread(conversation.bot_id, next ? next.id : conversation.bot_id);
    }
  };

  // A conversation of its own in which the bot sorts its notes into notes and projects.
  const handleSortNotes = async (bot) => {
    const created = await createConversation(bot.id, 'Sorting notes into projects');
    mergeConversation(bot.id, created);
    await sendMessage(created.id, bot.id, SORT_NOTES_REQUEST);
    await startTurn(created.id, null);
    rememberThread(bot.id, created.id);
    setActiveBotId(bot.id);
    setActiveTab('chat');
    closeBotPage();
  };

  // ---- projects --------------------------------------------------------------------------

  const mergeProject = (botId, saved) =>
    setProjectsByBot((prev) => {
      const list = prev[botId] || [];
      return { ...prev, [botId]: list.some((p) => p.id === saved.id) ? list.map((p) => (p.id === saved.id ? saved : p)) : [...list, saved] };
    });

  const handleNewProject = async (botId, name) => {
    const created = await createProject(botId, name);
    mergeProject(botId, created);
    return created;
  };

  const handleRenameProject = async (project, name) => mergeProject(project.bot_id, await updateProject(project.id, { name }));
  const handleArchiveProject = async (project, archived) => mergeProject(project.bot_id, await updateProject(project.id, { archived }));

  const handleDeleteProject = async (project) => {
    await deleteProject(project.id);
    await Promise.all([loadProjects(project.bot_id), loadConversations(project.bot_id)]);
  };

  const handleMoveConversation = async (conversation, projectId) => {
    const saved = await updateConversation(conversation.id, { project_id: projectId || '' });
    mergeConversation(conversation.bot_id, saved);
    loadProjects(conversation.bot_id);
  };

  // The panel's work notes: the project's, or the conversation's own.
  const handleSaveWorkNotes = async (text) => {
    if (activeProject) {
      mergeProject(activeProject.bot_id, await updateProject(activeProject.id, { notes: text }));
    } else if (activeConversation) {
      mergeConversation(activeConversation.bot_id, await updateConversation(activeConversation.id, { work_notes: text }));
    }
  };

  const handleDeleteConversation = async (conversation) => {
    await deleteConversation(conversation.id);
    setMessagesByBot((prev) => {
      const next = { ...prev };
      delete next[conversation.id];
      return next;
    });
    const list = await loadConversations(conversation.bot_id);
    if (conversation.id === activeThreadId) {
      const next = (list || []).find((c) => !c.archived && c.id !== conversation.id);
      rememberThread(conversation.bot_id, next ? next.id : conversation.bot_id);
    }
  };

  const activeBot = bots.find((b) => b.id === activeBotId) || bots[0];
  // The bot the settings page edits, live; when it goes (deleted, left elsewhere) the page closes.
  const pageBot = botPage?.mode === 'edit' ? bots.find((b) => b.id === botPage.botId) : null;
  useEffect(() => {
    if (botPage?.mode === 'edit' && !pageBot) closeBotPage();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [botPage, pageBot]);

  conversationsRef.current = conversationsByBot;

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

  // The settings page stays open after a save; a new bot becomes the page's bot, on the same section.
  const handleSaveBot = async (values, sectionId) => {
    if (botPage?.mode === 'edit' && botPage.botId) {
      const updated = await updateBot(botPage.botId, values);
      setBots((prev) => prev.map((b) => (b.id === updated.id ? updated : b)));
      if (updated.id === activeBotId && values.model) setDefaultModel(values.model);
    } else {
      const created = await createBot(values);
      setBots((prev) => [...prev, created]);
      setActiveBotId(created.id);
      setBotPage({ mode: 'edit', botId: created.id, section: { id: sectionId || 'general', nonce: Date.now() } });
    }
  };

  const handleArchiveBot = async (bot, archived) => {
    const updated = await updateBot(bot.id, { archived });
    setBots((prev) => {
      const list = prev.map((b) => (b.id === updated.id ? updated : b));
      if (archived) selectNextVisible(list, bot.id);
      return list;
    });
    if (!archived) setActiveBotId(bot.id);
    if (botPage?.botId === bot.id) closeBotPage();
  };

  const handleDeleteBot = async (bot) => {
    await deleteBot(bot.id);
    forgetBot(bot);
  };

  // Stop using a bot a teammate shared: it leaves the list; their bot is untouched.
  const handleLeaveBot = async (bot) => {
    await removeBotMember(bot.id, 'me');
    forgetBot(bot);
  };

  const forgetBot = (bot) => {
    setBots((prev) => {
      const list = prev.filter((b) => b.id !== bot.id);
      selectNextVisible(list, bot.id);
      return list;
    });
    const gone = new Set([bot.id, ...(conversationsByBot[bot.id] || []).map((c) => c.id)]);
    setMessagesByBot((prev) => Object.fromEntries(Object.entries(prev).filter(([threadId]) => !gone.has(threadId))));
    setConversationsByBot((prev) => {
      const next = { ...prev };
      delete next[bot.id];
      return next;
    });
    setProjectsByBot((prev) => {
      const next = { ...prev };
      delete next[bot.id];
      return next;
    });
    setBotToDelete(null);
    setBotToLeave(null);
    if (botPage?.botId === bot.id) closeBotPage();
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
        waitingApprovals={waitingApprovals}
        conversations={activeConversations}
        activeThreadId={activeThreadId}
        onSelectConversation={handleSelectConversation}
        onNewConversation={(botId, projectId) => handleNewConversation(botId, projectId).catch((err) => console.error('Could not start a conversation:', err))}
        onRenameConversation={(c, title) => handleRenameConversation(c, title).catch((err) => console.error('Rename failed:', err))}
        onArchiveConversation={(c, archived) => handleArchiveConversation(c, archived).catch((err) => console.error('Archive failed:', err))}
        onDeleteConversation={(c) => handleDeleteConversation(c).catch((err) => window.alert(err.message || 'Could not delete the conversation.'))}
        onMoveConversation={(c, projectId) => handleMoveConversation(c, projectId).catch((err) => window.alert(err.message || 'Could not move the conversation.'))}
        projects={activeProjects}
        onNewProject={(botId, name) => handleNewProject(botId, name).catch((err) => window.alert(err.message || 'Could not create the project.'))}
        onRenameProject={(p, name) => handleRenameProject(p, name).catch((err) => window.alert(err.message || 'Could not rename the project.'))}
        onArchiveProject={(p, archived) => handleArchiveProject(p, archived).catch((err) => console.error('Archive failed:', err))}
        onDeleteProject={(p) => handleDeleteProject(p).catch((err) => window.alert(err.message || 'Could not delete the project.'))}
        onOpenSettings={() => setActiveTab((tab) => (tab === 'settings' ? 'chat' : 'settings'))}
        onOpenNewBot={() => openBotPage(null)}
        onEditBot={(bot) => openBotPage(bot)}
        onArchiveBot={(bot, archived) => handleArchiveBot(bot, archived).catch((err) => console.error('Archive failed:', err))}
        onDeleteBot={(bot) => setBotToDelete(bot)}
        onLeaveBot={(bot) => setBotToLeave(bot)}
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
            threadId={activeThreadId}
            conversation={activeConversation}
            project={activeProject}
            onProjectsChanged={(botId) => {
              loadProjects(botId);
              loadConversations(botId);
            }}
            onSaveWorkNotes={handleSaveWorkNotes}
            onConversationPatched={handleConversationPatched}
            onNewConversation={() => handleNewConversation(activeBotId, activeConversation?.project_id || null)}
            models={models}
            catalogError={catalogError}
            onRefreshModels={refreshModels}
            messages={activeThreadId ? messagesByBot[activeThreadId] || [] : []}
            setMessagesFor={setMessagesFor}
            streamingBots={streamingBots}
            onStreamingChange={handleStreamingChange}
            turnStates={turnStates}
            onUpdateBotModel={handleUpdateBotModel}
            onEditBot={(sectionId) => activeBot && openBotPage(activeBot, sectionId)}
            onUpdateBot={handleUpdateBot}
            onBotPatched={(botId, patch) => setBots((prev) => prev.map((b) => (b.id === botId ? { ...b, ...patch } : b)))}
            onBotsChanged={adoptNewBots}
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

        {/* The bot settings page: a view like Settings, kept mounted while hidden so a draft survives a visit to the chat. */}
        {botPage && (botPage.mode === 'create' || pageBot) && (
          <div className={activeTab === 'bot' ? 'contents' : 'hidden'}>
            <BotSettingsPage
              key={botPage.botId || 'new'}
              bot={botPage.mode === 'edit' ? pageBot : null}
              sectionRequest={botPage.section}
              models={models}
              catalogError={catalogError}
              onRefreshModels={refreshModels}
              defaultModel={defaultModel}
              onClose={closeBotPage}
              onSave={handleSaveBot}
              onArchive={handleArchiveBot}
              onDelete={handleDeleteBot}
              onLeave={(bot) => setBotToLeave(bot)}
              onSortNotes={(bot) => handleSortNotes(bot).catch((err) => window.alert(err.message || 'Could not start the conversation.'))}
              onDirtyChange={setBotPageDirty}
            />
          </div>
        )}
      </main>

      {pendingBotPage && (
        <ConfirmDialog
          title="Discard unsaved changes?"
          body={`Your changes to ${pageBot?.name || 'the new bot'} are not saved. Open the settings of ${
            pendingBotPage.botId ? bots.find((b) => b.id === pendingBotPage.botId)?.name || 'the other bot' : 'a new bot'
          } anyway?`}
          confirmLabel="Discard"
          onCancel={() => {
            setPendingBotPage(null);
            setActiveTab('bot');
          }}
          onConfirm={() => {
            setBotPage(pendingBotPage);
            setBotPageDirty(false);
            setPendingBotPage(null);
            setActiveTab('bot');
          }}
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

      {botToLeave && (
        <ConfirmDialog
          title={`Leave ${botToLeave.name}?`}
          body={`It stops being shared with you; ${botToLeave.shared?.owner_name || 'its owner'} can share it again. Your conversations with it stay with the bot, out of sight.`}
          confirmLabel="Leave"
          onCancel={() => setBotToLeave(null)}
          onConfirm={() => handleLeaveBot(botToLeave).catch((err) => window.alert(err.message || 'Could not leave the bot.'))}
        />
      )}

    </div>
  );
}
