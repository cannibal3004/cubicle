'use client';

import React, { useState, useRef, useEffect, useMemo, useCallback } from 'react';
import MessageItem from './MessageItem';
import ApprovalCard from './ApprovalCard';
import QuestionCard from './QuestionCard';
import CommandPicker from './CommandPicker';
import ModelPicker from './ModelPicker';
import ToolsPopover from './ToolsPopover';
import ContextMeter from './ContextMeter';
import ThinkingPicker, { thinkingLabel, useThinkingLevels } from './ThinkingPicker';
import TodoCard from './TodoCard';
import WorkNotesSection from './WorkNotesSection';
import MascotAvatar from './MascotAvatar';
import { FiPlus, FiMic, FiMicOff, FiMonitor, FiX, FiImage, FiArrowDown, FiArrowUp, FiSquare, FiSliders, FiFile, FiFolder, FiRotateCw, FiVideo, FiAlertTriangle } from 'react-icons/fi';
import ComputerStatusButton from './ComputerStatusButton';
import {
  sendMessage,
  subscribeToChatStream,
  startTurn,
  fetchTurnStatus,
  cancelTurn,
  uploadImage,
  uploadAttachment,
  respondApproval,
  answerQuestion,
  fetchCommands,
  compactThread,
  fetchChatHistory,
  fetchBot,
  previewRewind,
  rewindThread,
  fetchRedoState,
  redoThread,
} from '../lib/api';

const AUTO_APPROVAL_MODES = ['off', 'shadow', 'on', 'inherit'];

function formatFileSize(bytes) {
  if (!Number.isFinite(bytes)) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// Mirrors the server's commands.command_name: only a leading slash counts.
function commandNameOf(text) {
  const stripped = (text || '').trimStart();
  if (!stripped.startsWith('/')) return null;
  const head = stripped.slice(1).split(/\s/)[0].toLowerCase();
  return head || null;
}

function formatHeaderDate(msgs) {
  const firstWithDate = msgs?.find((m) => m.created_at);
  if (!firstWithDate || !firstWithDate.created_at) {
    return 'Today';
  }
  const d = new Date(firstWithDate.created_at);
  if (isNaN(d.getTime())) return 'Today';

  const now = new Date();
  if (d.toDateString() === now.toDateString()) {
    return 'Today';
  }

  const yesterday = new Date();
  yesterday.setDate(now.getDate() - 1);
  if (d.toDateString() === yesterday.toDateString()) {
    return 'Yesterday';
  }

  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

export default function ChatWindow({ bot, threadId: threadIdProp, conversation, project, onProjectsChanged, onSaveWorkNotes, onOpenProject, onConversationPatched, onNewConversation, models, catalogError, onRefreshModels, messages, setMessagesFor, streamingBots, onStreamingChange, turnStates, onUpdateBotModel, onEditBot, onUpdateBot, onBotPatched, onToggleComputer, onOpenFiles, onOpenTakeover, onOpenAudit, onOpenSettings, defaultModel, prefill, onBotsChanged }) {
  // The conversation shown: its id is the thread id the server keys
  // messages, turns and undo by (a bot's first conversation has the bot's id).
  const threadId = threadIdProp || bot?.id;
  const [inputPrompt, setInputPrompt] = useState('');
  // Streaming is tracked per conversation by the Dashboard so a reply keeps
  // going while another conversation, bot or tab is shown.
  const isStreaming = Boolean(threadId && streamingBots?.[threadId]);
  const setStreamingFor = (key, value) => {
    if (onStreamingChange) onStreamingChange(key, value);
  };

  // One draft per conversation: switching must not carry or lose typed text.
  const draftsRef = useRef({});
  const draftBotRef = useRef(threadId);
  useEffect(() => {
    const previous = draftBotRef.current;
    const next = threadId;
    if (previous === next) return;
    if (previous) draftsRef.current[previous] = inputPrompt;
    setInputPrompt(draftsRef.current[next] || '');
    draftBotRef.current = next;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [threadId]);
  const [isListening, setIsListening] = useState(false);
  const [activeModel, setActiveModel] = useState(bot?.model || defaultModel || '');
  const [selectedImage, setSelectedImage] = useState(null);
  // Files for the next message: uploaded as they are picked, sent as cards.
  const [pendingFiles, setPendingFiles] = useState([]);
  // Undo/redo: whether the server can redo for this bot ({ available, messages }),
  // the text an undo put back (a redo clears it again if it is unchanged), a
  // demonstration recording carried by an edited message, and the confirm
  // dialog for a rewind that passes actions which stay done.
  const [redoInfo, setRedoInfo] = useState(null);
  const rewoundTextRef = useRef(null);
  const [pendingRecording, setPendingRecording] = useState(null);
  const [rewindConfirm, setRewindConfirm] = useState(null);
  const [rewinding, setRewinding] = useState(false);
  const [pendingApprovals, setPendingApprovals] = useState([]);
  // Cards shown when the bot asks the user to take over its computer.
  const [takeoverRequests, setTakeoverRequests] = useState([]);

  // Slash commands: the registry comes from the server so the picker and the
  // server agree on what exists. `pickerDismissed` is Esc; typing resets it.
  const [commands, setCommands] = useState([]);
  const [pickerIndex, setPickerIndex] = useState(0);
  const [pickerDismissed, setPickerDismissed] = useState(false);
  const textareaRef = useRef(null);
  useEffect(() => {
    let cancelled = false;
    fetchCommands()
      .then((data) => { if (!cancelled) setCommands(data?.commands || []); })
      .catch((err) => console.warn('Commands unavailable:', err));
    return () => { cancelled = true; };
  }, []);

  // Text handed in from elsewhere (e.g. "I'm done on the computer" after a
  // hand-back). An object with a nonce so the same text can be re-applied.
  useEffect(() => {
    if (!prefill?.text) return;
    if (prefill.send) {
      submitText(prefill.text, { recordingId: prefill.recordingId || null }).catch((err) => console.error('Could not send the hand-back message:', err));
    } else {
      setInputPrompt(prefill.text);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prefill]);
  const messagesEndRef = useRef(null);
  const scrollContainerRef = useRef(null);
  // Follow the stream only while the reader is already at the bottom. Scrolling
  // up to read something earlier turns following off; the pill below turns it
  // back on. Mutable ref, not state: it changes on every scroll event.
  const followRef = useRef(true);
  const [showJumpPill, setShowJumpPill] = useState(false);
  const fileInputRef = useRef(null);

  const botTitle = bot?.name || 'Grok 4.5 Analyst';

  // Initial welcome greeting fallback for the active bot
  const defaultInitialMessages = [
    {
      id: 'msg-intro',
      sender: 'bot',
      text: `Hello! I am **${botTitle}**. Ask me anything, or give me a task to analyze!`,
      isError: false,
    },
  ];

  const activeMessages = messages && messages.length > 0 ? messages : defaultInitialMessages;
  // Context in use, from the latest reply that reported usage.
  let lastUsage = null;
  for (let i = activeMessages.length - 1; i >= 0; i -= 1) {
    const m = activeMessages[i];
    if (m.sender === 'bot' && (m.usage || m.raw_payload?.usage)) {
      lastUsage = m.usage || m.raw_payload?.usage;
      break;
    }
  }
  const activeModelInfo = (models || []).find((m) => m.id === activeModel);

  useEffect(() => {
    if (bot?.model) {
      setActiveModel(bot.model);
    } else if (defaultModel) {
      setActiveModel(defaultModel);
    }
  }, [bot, defaultModel]);

  const NEAR_BOTTOM_PX = 80;

  const scrollToBottom = (behavior = 'auto') => {
    const el = scrollContainerRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior });
  };

  const handleThreadScroll = () => {
    const el = scrollContainerRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < NEAR_BOTTOM_PX;
    followRef.current = nearBottom;
    setShowJumpPill(!nearBottom);
  };

  const jumpToLatest = () => {
    followRef.current = true;
    setShowJumpPill(false);
    scrollToBottom('smooth');
  };

  const botApprovals = pendingApprovals.filter((approval) => approval.threadId === threadId);
  const botTakeovers = takeoverRequests.filter((request) => request.threadId === threadId);

  // Switching bots or conversations shows a different thread: start it at the bottom.
  useEffect(() => {
    followRef.current = true;
    setShowJumpPill(false);
    scrollToBottom('auto');
  }, [threadId]);

  // New content (tokens, tool cards, approval cards) only moves the view when
  // the reader is following. Instant, not smooth: a smooth scroll per token
  // never finishes before the next one starts.
  useEffect(() => {
    if (followRef.current) scrollToBottom('auto');
    // A content change can also shrink the thread (/clear, a collapsed block)
    // without any scroll event firing, which would leave a stale pill behind.
    // Re-measure: if the end is in view now, we are following again.
    const el = scrollContainerRef.current;
    if (el && el.scrollHeight - el.scrollTop - el.clientHeight < NEAR_BOTTOM_PX) {
      followRef.current = true;
      setShowJumpPill(false);
    }
  }, [activeMessages, isStreaming, botApprovals.length, botTakeovers.length]);

  const handleModelChange = (newModel) => {
    setActiveModel(newModel);
    if (onUpdateBotModel && bot?.id) {
      onUpdateBotModel(bot.id, newModel);
    }
  };

  const handleApprovalResponse = async (requestId, action) => {
    await respondApproval(requestId, action);
  };

  const handleQuestionAnswer = async (requestId, answer, optionIndex) => {
    await answerQuestion(requestId, answer, optionIndex);
  };

  // Attaching: the first image goes to the model as a picture; every other
  // file is uploaded to where the bot can reach it and arrives as a card.
  const attachImage = async (file) => {
    const previewUrl = URL.createObjectURL(file);
    setSelectedImage({ file, previewUrl, isUploading: true, uploadedUrl: null, error: null });
    try {
      const res = await uploadImage(file);
      setSelectedImage((prev) => (prev ? { ...prev, isUploading: false, uploadedUrl: res.url } : null));
    } catch (err) {
      console.error('Failed to upload image:', err);
      setSelectedImage((prev) => (prev ? { ...prev, isUploading: false, error: err.message } : null));
    }
  };

  const attachFile = async (file) => {
    const key = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    setPendingFiles((prev) => [...prev, { key, name: file.name, size: file.size, status: 'uploading', attachment: null, error: null }]);
    try {
      const res = await uploadAttachment(file, bot?.id);
      setPendingFiles((prev) => prev.map((item) => (item.key === key ? { ...item, status: 'ready', attachment: res.attachment } : item)));
    } catch (err) {
      setPendingFiles((prev) => prev.map((item) => (item.key === key ? { ...item, status: 'error', error: err?.message || 'Upload failed' } : item)));
    }
  };

  const addFiles = (files) => {
    let imageTaken = Boolean(selectedImage);
    for (const file of Array.from(files || [])) {
      if (file.type.startsWith('image/') && !imageTaken) {
        imageTaken = true;
        attachImage(file);
      } else {
        attachFile(file);
      }
    }
  };

  const handleFileSelect = (e) => {
    addFiles(e.target.files);
    e.target.value = '';
  };

  const handlePaste = (e) => {
    const files = Array.from(e.clipboardData?.files || []);
    if (files.length === 0) return;
    e.preventDefault();
    addFiles(files);
  };

  const handleDrop = (e) => {
    e.preventDefault();
    addFiles(e.dataTransfer?.files);
  };

  // One live subscription per bot. Attaching replays the turn's events from
  // `after`, so a reload or another machine rebuilds the reply in progress.
  const attachmentsRef = useRef({});
  const attachToTurn = (threadKey, turnId, after = 0, ownerBotId = bot?.id) => {
    const existing = attachmentsRef.current[threadKey];
    if (existing && (!turnId || existing.turnId === turnId)) return;
    if (existing) existing.close();

    let streamingMsgId = null;
    const updateMessages = (updater) => setMessagesFor(threadKey, updater);

    // Tokens arrive one event each. They are collected and applied once per
    // animation frame, so a burst of tokens costs one render, not one each.
    // Every other event flushes first, so ordering is unchanged.
    const pendingDeltas = { text: '', reasoning: '', frame: null };
    const flushDeltas = () => {
      pendingDeltas.frame = null;
      const { text, reasoning } = pendingDeltas;
      if (!text && !reasoning) return;
      pendingDeltas.text = '';
      pendingDeltas.reasoning = '';
      updateMessages((prev) =>
        prev.map((msg) => {
          if (msg.id !== streamingMsgId) return msg;
          // Thinking that resumes after new text or tool calls (the next round) starts a part
          // of its own, shown where it began, as the stored reply's reasoning_parts are.
          let parts = msg.reasoningParts || [];
          const last = parts[parts.length - 1];
          const calls = (msg.toolCalls || []).length;
          if (reasoning && (!last || msg.text.length > last.at || calls > last.after)) {
            parts = [...parts, { at: msg.text.length, after: calls, start: (msg.reasoning || '').length }];
          }
          return { ...msg, text: msg.text + text, reasoning: (msg.reasoning || '') + reasoning, reasoningParts: parts };
        })
      );
    };
    const queueDelta = (field, delta) => {
      pendingDeltas[field] += delta || '';
      if (pendingDeltas.frame === null) {
        pendingDeltas.frame = typeof requestAnimationFrame === 'function'
          ? requestAnimationFrame(flushDeltas)
          : setTimeout(flushDeltas, 16);
      }
    };

    const finish = () => {
      flushDeltas();
      setStreamingFor(threadKey, false);
      const current = attachmentsRef.current[threadKey];
      if (current && current.turnId === turnId) {
        current.close();
        delete attachmentsRef.current[threadKey];
      }
    };

    setStreamingFor(threadKey, true);
    const close = subscribeToChatStream(
      threadKey,
      { turnId, after },
      (event) => {
        if (event.type === 'content.delta') {
          queueDelta('text', event.delta);
          return;
        }
        if (event.type === 'reasoning.delta') {
          // The model's thinking. Shown in a collapsible block, never
          // merged into the answer text.
          queueDelta('reasoning', event.delta);
          return;
        }
        flushDeltas();
        if (event.type === 'turn.started') {
          streamingMsgId = event.botMsgId;
          updateMessages((prev) =>
            prev.some((msg) => msg.id === streamingMsgId)
              ? prev
              : [
                  ...prev,
                  {
                    id: streamingMsgId,
                    sender: 'bot',
                    text: '',
                    reasoning: '',
                    isStreaming: true,
                    created_at: new Date().toISOString(),
                  },
                ]
          );
        } else if (event.type === 'thinking.picked') {
          // How hard the model thinks this turn: shown on the reply and in the header chip.
          const thinking = { level: event.level, auto: event.auto, source: event.source };
          updateMessages((prev) => prev.map((msg) => (msg.id === event.botMsgId ? { ...msg, thinking } : msg)));
          if (event.level && onConversationPatched) onConversationPatched(threadKey, { thinking_last: event.level });
        } else if (event.type === 'request.opened') {
          setPendingApprovals((prev) => [
            ...prev.filter((approval) => approval.requestId !== event.requestId),
            { ...event, threadId: threadKey },
          ]);
        } else if (event.type === 'request.answered' || event.type === 'request.expired') {
          // A question card is done with (answered, or the bot stopped waiting).
          setPendingApprovals((prev) => prev.filter((approval) => approval.requestId !== event.requestId));
        } else if (event.type === 'gate.scored') {
          // The auto-approval gate's verdict on a pending card. If it approved
          // the action, the card is about to be replaced by tool.started.
          setPendingApprovals((prev) =>
            prev.map((approval) => (approval.requestId === event.requestId ? { ...approval, gate: event.verdict } : approval))
          );
        } else if (event.type === 'computer.takeover_requested') {
          setTakeoverRequests((prev) => [
            ...prev.slice(-4),
            { id: `${event.botMsgId}-${Date.now()}`, reason: event.reason || '', threadId: threadKey, threadKey: ownerBotId },
          ]);
        } else if (event.type === 'context.compacted' && event.message) {
          // The stored marker goes where it sits in history: after the user's
          // message, before the reply that is about to stream.
          updateMessages((prev) => {
            const placeholder = prev.find((msg) => msg.id === streamingMsgId);
            const rest = prev.filter((msg) => msg.id !== streamingMsgId && msg.id !== event.message.id);
            return placeholder ? [...rest, event.message, placeholder] : [...rest, event.message];
          });
        } else if (event.type === 'context.compaction_failed') {
          updateMessages((prev) => [
            ...prev,
            { id: `local-${Date.now()}`, sender: 'system', text: `Context compaction failed: ${event.error}. Continuing with the full context.`, isError: true, local: true, created_at: new Date().toISOString() },
          ]);
        } else if (event.type === 'model.queued') {
          // In line for a slot at the model server; shown until the request starts.
          updateMessages((prev) =>
            prev.map((msg) => (msg.id === streamingMsgId ? { ...msg, waiting: { ahead: event.ahead, limit: event.limit } } : msg))
          );
        } else if (event.type === 'model.started') {
          updateMessages((prev) => prev.map((msg) => (msg.id === streamingMsgId && msg.waiting ? { ...msg, waiting: null } : msg)));
        } else if (event.type === 'turn.usage' && event.usage) {
          updateMessages((prev) =>
            prev.map((msg) => (msg.id === streamingMsgId ? { ...msg, usage: event.usage } : msg))
          );
        } else if (event.type === 'attachment.added' && event.attachment) {
          updateMessages((prev) =>
            prev.map((msg) => {
              if (msg.id !== streamingMsgId) return msg;
              const existing = msg.attachments || [];
              const same = (a) => a.source === event.attachment.source && a.path === event.attachment.path;
              return existing.some(same) ? msg : { ...msg, attachments: [...existing, event.attachment] };
            })
          );
        } else if (['tool.waiting', 'tool.started', 'tool.completed', 'tool.failed', 'tool.denied', 'tool.expired'].includes(event.type)) {
          if (event.type === 'tool.completed' && String(event.callName || '').startsWith('todo_') && Array.isArray(event.result?.items) && onBotPatched) {
            // A todo result carries the whole list: show it without a refetch.
            if (onConversationPatched) onConversationPatched(threadKey, { todo: event.result.items });
          }
          if (event.type === 'tool.completed' && /^(work_notes_|project)/.test(String(event.callName || '')) && onProjectsChanged) {
            onProjectsChanged(ownerBotId, threadKey);
          }
          if (event.type === 'tool.completed' && String(event.callName || '') === 'bots_create' && onBotsChanged) {
            // An orchestrator made a bot: the list shows it without a reload.
            onBotsChanged();
          }
          if (event.type === 'tool.completed' && ['memory_write', 'memory_edit'].includes(String(event.callName || '')) && onBotPatched) {
            // The notes changed: fetch them so the editor shows the new text without a reload.
            fetchBot(ownerBotId)
              .then((fresh) => onBotPatched(ownerBotId, { memory: fresh.memory, memory_updated_at: fresh.memory_updated_at }))
              .catch(() => {});
          }
          if (event.requestId) {
            setPendingApprovals((prev) => prev.filter((approval) => approval.requestId !== event.requestId));
          }
          // Keep a per-message record of the tools the model used so the
          // reply shows them, matching what is persisted in raw_payload.
          updateMessages((prev) =>
            prev.map((msg) => {
              if (msg.id !== streamingMsgId) return msg;
              const key = event.requestId || `${event.callName || event.tool}-${Date.now()}`;
              const calls = msg.toolCalls || [];
              const existing = calls.find((call) => call.id === key);
              const entry = {
                id: key,
                name: event.callName || event.tool,
                status: event.type.replace('tool.', ''),
                error: event.error || null,
                waiting: event.type === 'tool.waiting' ? event.reason || 'Waiting for the computer.' : null,
                gate: event.gate || existing?.gate || null,
                trusted: Boolean(event.trusted) || Boolean(existing?.trusted),
                trusted_by: event.trustedBy || existing?.trusted_by || null,
                summary: event.action?.preview || existing?.summary || null,
                // For the step's details: what it was given, what came back, how long it ran.
                arguments: event.action?.arguments || existing?.arguments || null,
                result: event.result ?? existing?.result ?? null,
                ran_ms: event.ranMs ?? existing?.ran_ms ?? null,
                // Where in the reply the call came: the text streamed so far.
                at: existing ? existing.at : (msg.text || '').length,
              };
              return {
                ...msg,
                toolCalls: existing ? calls.map((call) => (call.id === key ? entry : call)) : [...calls, entry],
              };
            })
          );
        } else if (event.type === 'turn.completed' || event.type === 'turn.failed' || event.type === 'turn.cancelled') {
          updateMessages((prev) =>
            prev.map((msg) => (msg.id === streamingMsgId ? { ...msg, isStreaming: false } : msg))
          );
          if (event.type === 'turn.failed' && streamingMsgId) {
            updateMessages((prev) =>
              prev.map((msg) =>
                msg.id === streamingMsgId && !msg.text
                  ? { ...msg, text: `Error: the reply failed on the server: ${event.error || 'unknown error'}` }
                  : msg
              )
            );
          }
          finish();
        } else if (event.type === 'turn.none') {
          finish();
        }
      },
      () => finish()
    );
    attachmentsRef.current[threadKey] = { turnId, close };
  };

  useEffect(() => () => {
    Object.values(attachmentsRef.current).forEach((attachment) => attachment.close());
  }, []);

  // Follow a turn that is already running for this bot: after a reload, a
  // bot switch, or when the server reports one started elsewhere.
  const remoteTurnId = threadId ? turnStates?.[threadId]?.turn_id : undefined;
  useEffect(() => {
    const botId = threadId;
    if (!botId) return undefined;
    let cancelled = false;
    fetchTurnStatus(botId)
      .then((status) => {
        if (cancelled) return;
        const turn = status?.turn;
        if (turn && turn.status === 'running') {
          const attached = attachmentsRef.current[botId];
          if (attached && attached.turnId === turn.turn_id) return; // This tab started it; nothing to pull in.
          // A turn started elsewhere (another machine, a schedule) may have
          // posted messages this tab has not seen: pull them in, ahead of
          // the reply the replayed events are about to build.
          fetchChatHistory(botId)
            .then((history) => {
              if (cancelled) return;
              setMessagesFor(botId, (prev) => {
                const current = prev || [];
                const seen = new Set(current.map((m) => m.id));
                const missing = (history || []).filter((m) => !seen.has(m.id));
                if (!missing.length) return current;
                const at = current.findIndex((m) => m.isStreaming);
                return at === -1 ? [...current, ...missing] : [...current.slice(0, at), ...missing, ...current.slice(at)];
              });
            })
            .catch(() => {});
          attachToTurn(botId, turn.turn_id, 0);
        }
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [threadId, remoteTurnId]);

  // ---- slash commands ---------------------------------------------------------

  // The picker is open while the text is a lone "/word" with no space yet.
  const commandQuery = (() => {
    const stripped = inputPrompt.trimStart();
    if (!stripped.startsWith('/') || /\s/.test(stripped)) return null;
    return stripped.slice(1).toLowerCase();
  })();
  const filteredCommands = commandQuery === null
    ? []
    : commands.filter((c) => c.name.startsWith(commandQuery)).filter((c) => !isStreaming || c.while_streaming);
  const pickerOpen = !pickerDismissed && filteredCommands.length > 0;

  useEffect(() => { setPickerIndex(0); }, [commandQuery, filteredCommands.length]);

  // Auto-grow the composer up to 40% of the window, then scroll inside it.
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, Math.max(160, Math.round(window.innerHeight * 0.4)))}px`;
  }, [inputPrompt]);

  // ---- undo and redo ------------------------------------------------------------

  // The user's own messages after the latest compaction can be edited.
  const editableIds = (() => {
    const list = messages || [];
    let start = 0;
    for (let i = list.length - 1; i >= 0; i -= 1) {
      if (list[i].sender === 'system' && list[i].item_type === 'compaction') { start = i + 1; break; }
    }
    return list.slice(start)
      .filter((m) => m.sender === 'user' && m.id && !String(m.id).startsWith('temp-') && !m.raw_payload?.scheduled && !m.raw_payload?.delegated)
      .map((m) => m.id);
  })();
  const editableKey = editableIds.join(',');
  const editableSet = useMemo(() => new Set(editableKey ? editableKey.split(',') : []), [editableKey]);

  useEffect(() => {
    const botId = threadId;
    setRedoInfo(null);
    setRewindConfirm(null);
    setPendingRecording(null);
    rewoundTextRef.current = null;
    if (!botId) return undefined;
    let cancelled = false;
    fetchRedoState(botId).then((state) => { if (!cancelled) setRedoInfo(state); }).catch(() => {});
    return () => { cancelled = true; };
  }, [threadId]);

  // The message comes back into the composer: its text, files, image and demonstration.
  const restoreToComposer = (message) => {
    const text = message?.text || '';
    setInputPrompt(text);
    rewoundTextRef.current = text;
    const files = Array.isArray(message?.attachments) ? message.attachments : [];
    setPendingFiles(files.map((attachment, index) => ({
      key: `restored-${Date.now()}-${index}`,
      name: attachment.name || String(attachment.path || '').split('/').pop() || 'file',
      size: attachment.size,
      status: 'ready',
      attachment,
      error: null,
    })));
    setSelectedImage(message?.image_url ? { file: null, previewUrl: message.image_url, isUploading: false, uploadedUrl: message.image_url, error: null } : null);
    setPendingRecording(message?.raw_payload?.demonstration || null);
    window.setTimeout(() => textareaRef.current?.focus(), 0);
  };

  const performRewind = async (messageId) => {
    const botId = threadId;
    if (!botId || rewinding) return;
    setRewinding(true);
    setRewindConfirm(null);
    try {
      // A reply in progress is stopped by the server; stop following it here.
      const attached = attachmentsRef.current[botId];
      if (attached) {
        attached.close();
        delete attachmentsRef.current[botId];
      }
      const result = await rewindThread(botId, messageId);
      setStreamingFor(botId, false);
      const history = await fetchChatHistory(botId);
      setMessagesFor(botId, () => history || []);
      restoreToComposer(result.message);
      setRedoInfo(result.redo || null);
    } catch (err) {
      addLocalMessage(`Could not undo: ${err.message}`, true);
    } finally {
      setRewinding(false);
    }
  };

  // Edit (and /undo, and the up arrow): rewind at once, unless the bot did
  // something since that the rewind cannot take back; then ask first.
  const editMessage = async (message) => {
    const botId = threadId;
    if (!botId || !message?.id || rewinding) return;
    try {
      const preview = await previewRewind(botId, message.id);
      if (preview.actions && preview.actions.length) {
        setRewindConfirm({ messageId: message.id, preview });
        return;
      }
    } catch (err) {
      addLocalMessage(`Could not undo: ${err.message}`, true);
      return;
    }
    await performRewind(message.id);
  };
  const editRef = useRef(editMessage);
  editRef.current = editMessage;
  const onEditMessage = useCallback((message) => editRef.current(message), []);

  const undoLast = () => {
    const lastId = editableIds[editableIds.length - 1];
    const last = lastId ? (messages || []).find((m) => m.id === lastId) : null;
    if (!last) {
      addLocalMessage('There is nothing to undo since the last compaction.');
      return;
    }
    editMessage(last);
  };

  const redoLast = async () => {
    const botId = threadId;
    if (!botId) return;
    try {
      const result = await redoThread(botId);
      const history = await fetchChatHistory(botId);
      setMessagesFor(botId, () => history || []);
      // The text the undo put back is now in the thread again.
      if (rewoundTextRef.current !== null && inputPrompt === rewoundTextRef.current) {
        setInputPrompt('');
        setPendingFiles([]);
        setSelectedImage(null);
        setPendingRecording(null);
      }
      rewoundTextRef.current = null;
      setRedoInfo(result.redo || null);
    } catch (err) {
      setRedoInfo(null);
      addLocalMessage(`Could not redo: ${err.message}`, true);
    }
  };

  const addLocalMessage = (text, isError = false) => {
    if (!threadId) return;
    setMessagesFor(threadId, (prev) => [
      ...prev,
      { id: `local-${Date.now()}-${Math.random().toString(16).slice(2, 6)}`, sender: 'system', text, isError, local: true, created_at: new Date().toISOString() },
    ]);
  };

  // This conversation's thinking setting ("" follows the bot's).
  const thinkingLevels = useThinkingLevels();
  const setThinking = async (value) => {
    if (!threadId || !onConversationPatched) return;
    await onConversationPatched(threadId, { thinking: value }, { save: true });
  };

  const runClientCommand = async (spec, args) => {
    const botId = threadId;
    if (!botId) return;
    switch (spec.name) {
      case 'help': {
        const lines = commands.map((c) => `${c.usage}\n    ${c.description}`);
        addLocalMessage(`Slash commands\n\n${lines.join('\n')}\n\nThe full guide is under Help in the sidebar, and you can ask any bot how Cubicle works.`);
        return;
      }
      case 'clear': {
        // A fresh start is a new conversation; this one stays in the list.
        if (!onNewConversation) return;
        try {
          await onNewConversation();
        } catch (err) {
          addLocalMessage(`Could not start a new conversation: ${err.message}`, true);
        }
        return;
      }
      case 'compact': {
        addLocalMessage('Compacting the conversation. The model is writing a summary of the older turns...');
        try {
          const result = await compactThread(botId);
          if (result.status === 'nothing_to_compact') {
            addLocalMessage(`Nothing to compact yet: the newest ${Number(result.keep_recent_tokens).toLocaleString()} tokens of conversation are always kept as they are.`);
            return;
          }
          const history = await fetchChatHistory(botId);
          setMessagesFor(botId, () => history);
        } catch (err) {
          addLocalMessage(`Compaction failed: ${err.message}`, true);
        }
        return;
      }
      case 'model': {
        const query = args.trim();
        if (!query) {
          addLocalMessage(`Current model: ${activeModel || 'default'}\nUsage: /model <model>`);
          return;
        }
        const exact = models.find((m) => m.id.toLowerCase() === query.toLowerCase());
        const matches = exact ? [exact] : models.filter((m) => `${m.id} ${m.name || ''}`.toLowerCase().includes(query.toLowerCase()));
        if (matches.length === 1) {
          handleModelChange(matches[0].id);
          addLocalMessage(`Model set to ${matches[0].id}.`);
        } else if (matches.length === 0 && models.length === 0) {
          handleModelChange(query);
          addLocalMessage(`Model set to ${query}. The model catalog is unavailable, so the id was not checked.`);
        } else if (matches.length === 0) {
          addLocalMessage(`No model matches "${query}".`, true);
        } else {
          const shown = matches.slice(0, 8).map((m) => `  ${m.id}`).join('\n');
          addLocalMessage(`"${query}" matches ${matches.length} models. Be more specific:\n${shown}${matches.length > 8 ? '\n  ...' : ''}`, true);
        }
        return;
      }
      case 'think': {
        const wanted = args.trim().toLowerCase();
        const value = { off: 'none', default: '' }[wanted] ?? wanted;
        const own = conversation?.thinking || '';
        if (!wanted || !(value === '' || thinkingLevels.some((l) => l.value === value))) {
          const now = own ? thinkingLabel(own) : `default (${bot?.thinking ? thinkingLabel(bot.thinking) : 'App Settings'})`;
          const usage = thinkingLevels.map((l) => (l.value === 'none' ? 'off' : l.value)).join('|');
          addLocalMessage(`Thinking in this conversation: ${now}\nUsage: /think ${usage}|default`, wanted !== '');
          return;
        }
        try {
          await setThinking(value);
          addLocalMessage(value ? `Thinking in this conversation: ${thinkingLabel(value)}.` : "Thinking in this conversation follows the bot's setting again.");
        } catch (err) {
          addLocalMessage(`Could not change the thinking: ${err.message}`, true);
        }
        return;
      }
      case 'auto': {
        const mode = args.trim().toLowerCase();
        if (!AUTO_APPROVAL_MODES.includes(mode)) {
          addLocalMessage(`Auto-approval for this bot: ${bot?.auto_approval || 'inherit'}\nUsage: /auto off|shadow|on|inherit`, mode !== '');
          return;
        }
        if (!onUpdateBot) return;
        try {
          await onUpdateBot(bot.id, { auto_approval: mode });
          addLocalMessage(`Auto-approval for ${botTitle}: ${mode}.`);
        } catch (err) {
          addLocalMessage(`Could not change auto-approval: ${err.message}`, true);
        }
        return;
      }
      case 'stop': {
        if (!isStreaming) {
          addLocalMessage('Nothing is streaming.');
          return;
        }
        try {
          await cancelTurn(botId);
          addLocalMessage('Stopping the reply.');
        } catch (err) {
          addLocalMessage(`Could not stop the reply: ${err.message}`, true);
        }
        return;
      }
      case 'undo':
        undoLast();
        return;
      case 'redo':
        if (!redoInfo?.available) {
          addLocalMessage('There is nothing to redo.');
          return;
        }
        await redoLast();
        return;
      case 'computer':
        if (onToggleComputer) onToggleComputer(); else addLocalMessage('The computer is not available here.', true);
        return;
      case 'audit':
        if (onOpenAudit) onOpenAudit(); else addLocalMessage('The audit trail is not available here.', true);
        return;
      case 'settings':
        if (onOpenSettings) onOpenSettings(); else addLocalMessage('Settings are not available here.', true);
        return;
      default:
        addLocalMessage(`/${spec.name} is not implemented in this client.`, true);
    }
  };

  const pickCommand = (spec) => {
    if (!spec) return;
    setPickerDismissed(true);
    if (spec.args) {
      setInputPrompt(`/${spec.name} `);
      textareaRef.current?.focus();
    } else {
      setInputPrompt('');
      submitText(`/${spec.name}`);
    }
  };

  const handleComposerKeyDown = (e) => {
    if (pickerOpen) {
      if (e.key === 'ArrowDown') { e.preventDefault(); setPickerIndex((i) => (i + 1) % filteredCommands.length); return; }
      if (e.key === 'ArrowUp') { e.preventDefault(); setPickerIndex((i) => (i - 1 + filteredCommands.length) % filteredCommands.length); return; }
      if (e.key === 'Tab' || e.key === 'Enter') { e.preventDefault(); pickCommand(filteredCommands[pickerIndex]); return; }
      if (e.key === 'Escape') { e.preventDefault(); setPickerDismissed(true); return; }
    }
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent?.isComposing) {
      e.preventDefault();
      submitText(inputPrompt);
      return;
    }
    // Up arrow in an empty composer edits your last message.
    if (e.key === 'ArrowUp' && !inputPrompt && !pendingFiles.length && !selectedImage && editableIds.length) {
      e.preventDefault();
      undoLast();
    }
  };

  // What the send button reflects: something to send, and no file still uploading.
  const canSend =
    !pendingFiles.some((item) => item.status === 'uploading') &&
    Boolean(inputPrompt.trim() || selectedImage || pendingRecording || pendingFiles.some((item) => item.status === 'ready'));

  const handleSendMessage = async (e) => {
    e?.preventDefault();
    await submitText(inputPrompt);
  };

  // `options.recordingId` names a demonstration the user just recorded on the
  // bot's computer; the server adds its steps to the message.
  const submitText = async (rawText, options = {}) => {
    const name = commandNameOf(rawText);
    if (name) {
      const spec = commands.find((c) => c.name === name);
      if (!spec) {
        addLocalMessage(`Unknown command /${name}. Type / to see the available commands.`, true);
        return;
      }
      if (spec.kind === 'client') {
        if (isStreaming && !spec.while_streaming) {
          addLocalMessage(`/${name} cannot run while a reply is streaming. Use /stop first.`, true);
          return;
        }
        setInputPrompt('');
        setPickerDismissed(false);
        await runClientCommand(spec, rawText.trim().slice(name.length + 1));
        return;
      }
      // Action commands (/shared, /connector) go to the server as a normal
      // message; it parses them and runs them through the gateway.
    }
    if (pendingFiles.some((item) => item.status === 'uploading')) return; // still uploading: Enter again in a moment
    const readyFiles = pendingFiles.filter((item) => item.status === 'ready' && item.attachment);
    const recordingId = options.recordingId || pendingRecording?.id || null;
    if ((!rawText.trim() && !selectedImage && readyFiles.length === 0 && !recordingId) || isStreaming) return;

    const botId = threadId;
    if (!botId || !bot?.id) return;
    const updateMessages = (updater) => setMessagesFor(botId, updater);
    const userText = rawText;
    const currentSelected = selectedImage;

    setInputPrompt('');
    setSelectedImage(null);
    setPendingFiles([]);
    setPendingRecording(null);
    // Sending starts a new branch: the server forgets the redo history.
    setRedoInfo(null);
    rewoundTextRef.current = null;
    followRef.current = true;
    setShowJumpPill(false);

    let finalImageUrl = currentSelected?.uploadedUrl || null;

    // Ensure image upload finishes before dispatching to the backend
    if (currentSelected && !finalImageUrl) {
      try {
        const res = await uploadImage(currentSelected.file);
        finalImageUrl = res.url;
      } catch (err) {
        console.error('Image upload failed on send:', err);
      }
    }

    const userMsgObj = {
      id: `temp-user-${Date.now()}`,
      sender: 'user',
      text: userText,
      image_url: currentSelected?.previewUrl || finalImageUrl,
      attachments: readyFiles.map((item) => item.attachment),
      created_at: new Date().toISOString(),
    };
    updateMessages((prev) => [...prev, userMsgObj]);

    try {
      const sent = await sendMessage(
        botId,
        bot.id,
        userText,
        activeModel,
        finalImageUrl,
        readyFiles.map((item) => ({ source: item.attachment.source, path: item.attachment.path })),
        recordingId
      );
      // Take the server's id for the message, so a later history merge (a
      // turn seen through the poll, a reload) recognises it as the same one.
      if (sent?.message?.id) {
        updateMessages((prev) =>
          prev.map((msg) => (msg.id === userMsgObj.id ? { ...msg, id: sent.message.id, created_at: sent.message.created_at || msg.created_at, text: sent.message.text ?? msg.text, raw_payload: sent.message.raw_payload || msg.raw_payload } : msg))
        );
      }
      const started = await startTurn(botId, activeModel);
      attachToTurn(botId, started?.turn?.turn_id || null, 0, bot.id);
      // The server names a conversation after its first message.
      if (conversation && !conversation.title && onConversationPatched) onConversationPatched(botId, { refresh: true });
    } catch (err) {
      console.error('Send message error:', err);
      setStreamingFor(botId, false);
    }
  };


  const handleVoiceToggle = () => {
    if (!('webkitSpeechRecognition' in window) && !('SpeechRecognition' in window)) {
      alert('Voice recognition is not supported in this browser environment.');
      return;
    }

    if (isListening) {
      setIsListening(false);
    } else {
      try {
        const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
        const recognition = new SpeechRecognition();
        recognition.continuous = false;
        recognition.onstart = () => setIsListening(true);
        recognition.onresult = (event) => {
          const transcript = event.results[0][0].transcript;
          setInputPrompt((prev) => prev + (prev ? ' ' : '') + transcript);
          setIsListening(false);
        };
        recognition.onerror = () => setIsListening(false);
        recognition.onend = () => setIsListening(false);
        recognition.start();
      } catch (err) {
        setIsListening(false);
      }
    }
  };

  return (
    <div className="flex-1 flex flex-col h-screen overflow-hidden bg-[#09090b] relative font-sans text-zinc-100">
      {/* Top Header Bar */}
      <header className="px-6 py-3.5 flex items-center justify-between z-20 bg-[#09090b]/80 backdrop-blur-md border-b border-[#18181c] select-none">
        {/* Left Side: Bot Indicator */}
        <div className="flex items-center gap-2.5 min-w-0">
          <div
            className="w-7 h-7 rounded-lg flex items-center justify-center text-base border flex-shrink-0"
            style={{ background: `${bot?.accent_color || '#3b82f6'}22`, borderColor: `${bot?.accent_color || '#3b82f6'}55` }}
            aria-hidden="true"
          >
            {bot?.avatar || '🤖'}
          </div>
          <h2 className="font-bold text-sm text-zinc-100 tracking-wide truncate">{botTitle}</h2>
          {conversation?.title && (
            <span className="text-xs text-zinc-500 truncate min-w-0" title={conversation.title}>
              · {conversation.title}
            </span>
          )}
          {bot && onEditBot && (
            <button
              type="button"
              onClick={() => onEditBot()}
              className="p-1 rounded-md text-zinc-500 hover:text-white hover:bg-[#1f1f23] transition"
              title="Bot settings"
            >
              <FiSliders className="text-xs" />
            </button>
          )}
        </div>


        {/* Right Side: Model Picker & Computer Monitor Toggle */}
        <div className="flex items-center gap-3">
          <ContextMeter usage={lastUsage} contextWindow={activeModelInfo?.context_length} model={activeModel} />

          <ModelPicker
            currentModel={activeModel}
            models={models}
            catalogError={catalogError}
            onRefresh={onRefreshModels}
            onSelectModel={handleModelChange}
          />

          {bot && onConversationPatched && (
            <ThinkingPicker
              conversation={conversation}
              bot={bot}
              onChange={(value) => setThinking(value).catch((err) => addLocalMessage(`Could not change the thinking: ${err.message}`, true))}
            />
          )}

          {bot && onUpdateBot && (
            <ToolsPopover
              bot={bot}
              onSave={(toolSettings) => onUpdateBot(bot.id, { tool_settings: toolSettings })}
              onSaveAutoApproval={(mode) => onUpdateBot(bot.id, { auto_approval: mode })}
              onSaveGateThreshold={(value) => onUpdateBot(bot.id, { gate_threshold: value === null ? 'inherit' : value })}
              onOpenEditor={onEditBot && (() => onEditBot('tools'))}
            />
          )}

          {bot && onOpenFiles && (
            <button
              suppressHydrationWarning={true}
              onClick={onOpenFiles}
              className="p-1.5 rounded-lg text-zinc-400 hover:text-white hover:bg-[#1f1f23] transition"
              title="Files on this bot's computer"
            >
              <FiFolder className="text-base" />
            </button>
          )}
          <ComputerStatusButton bot={bot} onClick={onToggleComputer} />
        </div>
      </header>

      {/* The thread and the composer, with the bot's to-do list in a panel on the right. */}
      <div className="flex-1 flex min-h-0">
      <div className="flex-1 flex flex-col min-h-0 min-w-0 relative">
      {/* Main Canvas Scrollable Chat Thread */}
      <div ref={scrollContainerRef} onScroll={handleThreadScroll} className="flex-1 overflow-y-auto px-6 py-4 relative">
        <div className="max-w-4xl mx-auto w-full space-y-3 px-12 md:px-20">
          {/* Centered Recorded Timestamp */}
          <div className="text-center my-4">
            <span className="text-[11px] font-medium text-zinc-500 font-sans tracking-wide">
              {formatHeaderDate(activeMessages)}
            </span>
          </div>

          {/* Message Items List */}
          {activeMessages.map((msg) => (
            <MessageItem key={msg.id} message={msg} botId={bot?.id} onEdit={editableSet.has(msg.id) ? onEditMessage : null} />
          ))}

          {/* An empty conversation (only the greeting above) offers the bot's starter prompts. */}
          {(!messages || messages.length === 0) && Array.isArray(bot?.suggestions) && bot.suggestions.length > 0 && (
            <div className="pt-6">
              <div className="text-center text-xs text-zinc-500 mb-3">Not sure where to start? Try one of these.</div>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                {bot.suggestions.map((suggestion) => (
                  <button
                    key={suggestion}
                    type="button"
                    onClick={() => {
                      setInputPrompt(suggestion);
                      window.setTimeout(() => textareaRef.current?.focus(), 0);
                    }}
                    className="text-left text-sm text-zinc-200 rounded-xl border border-[#26262b] bg-[#141417] px-4 py-3 hover:border-zinc-500 hover:bg-[#1a1a1e] transition"
                  >
                    {suggestion}
                  </button>
                ))}
              </div>
            </div>
          )}


          {/* Approval and takeover cards sit where the conversation currently is, not at the top. */}
          {botApprovals.map((approval) =>
            approval.requestType === 'question' ? (
              <QuestionCard key={approval.requestId} question={approval} onAnswer={handleQuestionAnswer} />
            ) : (
              <ApprovalCard
                key={approval.requestId}
                approval={approval}
                onRespond={handleApprovalResponse}
                onAlwaysAllow={
                  bot && onUpdateBot
                    ? (shapes) => onUpdateBot(bot.id, { allowed_commands: [...new Set([...(bot.allowed_commands || []), ...shapes])] })
                    : null
                }
              />
            )
          )}

          {botTakeovers.map((request) => (
            <div
              key={request.id}
              className="my-3 p-4 rounded-2xl border border-purple-500/30 bg-purple-500/10 shadow-xl max-w-xl"
            >
              <div className="flex items-start gap-3">
                <div className="w-8 h-8 rounded-lg bg-purple-500/20 text-purple-300 border border-purple-500/30 flex items-center justify-center flex-shrink-0">
                  <FiMonitor className="text-base" />
                </div>
                <div className="flex-1 min-w-0">
                  <p className="text-xs font-bold text-purple-200 uppercase tracking-wider">
                    {botTitle} needs you on its computer
                  </p>
                  <p className="text-xs text-zinc-200 mt-1">{request.reason || 'The bot asked you to take over its computer.'}</p>
                  <p className="text-[11px] text-zinc-400 mt-1">
                    Open the computer, press Take control, do the step, then press I&apos;m done: the bot carries on from there.
                  </p>
                </div>
              </div>
              <div className="flex items-center justify-end gap-2 mt-3 pt-2 border-t border-purple-500/20">
                <button
                  suppressHydrationWarning={true}
                  type="button"
                  onClick={() => setTakeoverRequests((prev) => prev.filter((item) => item.id !== request.id))}
                  className="px-3 py-1.5 rounded-xl text-xs font-medium text-zinc-400 hover:text-white transition"
                >
                  Dismiss
                </button>
                <button
                  suppressHydrationWarning={true}
                  type="button"
                  onClick={() => {
                    setTakeoverRequests((prev) => prev.filter((item) => item.id !== request.id));
                    if (onOpenTakeover) onOpenTakeover();
                    else if (onToggleComputer) onToggleComputer();
                  }}
                  className="flex items-center gap-1.5 px-4 py-1.5 rounded-xl bg-purple-600 hover:bg-purple-500 text-white text-xs font-semibold shadow-lg shadow-purple-600/30 transition"
                >
                  <FiMonitor className="text-sm" /> Open computer
                </button>
              </div>
            </div>
          ))}

          {isStreaming && (
            <div className="flex justify-start items-center gap-3 my-3 animate-fade-in">
              <MascotAvatar type={bot?.isError ? 'warning' : 'blue'} size="sm" />
              <div className="bg-[#18181b] border border-[#27272a] px-4 py-3 rounded-2xl flex items-center gap-1.5 shadow-sm">
                <span className="w-2 h-2 rounded-full bg-blue-400 animate-bounce" style={{ animationDelay: '-0.32s' }} />
                <span className="w-2 h-2 rounded-full bg-blue-400 animate-bounce" style={{ animationDelay: '-0.16s' }} />
                <span className="w-2 h-2 rounded-full bg-blue-400 animate-bounce" style={{ animationDelay: '0s' }} />
              </div>
            </div>
          )}


          <div ref={messagesEndRef} />
        </div>

        {/* Shown after scrolling up: jump back to the live end of the thread. */}
        {showJumpPill && (
          <div className="sticky bottom-2 h-0 flex justify-center pointer-events-none">
            <button
              suppressHydrationWarning={true}
              type="button"
              onClick={jumpToLatest}
              className={`pointer-events-auto -translate-y-full flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-medium shadow-lg border transition ${
                botApprovals.length > 0
                  ? 'bg-amber-500 text-black border-amber-400 hover:bg-amber-400'
                  : 'bg-[#1c1c20] text-zinc-200 border-[#2b2b32] hover:bg-[#26262c]'
              }`}
            >
              <FiArrowDown className="text-sm" />
              {botApprovals.length > 0
                ? `${botApprovals.length === 1 ? 'Approval' : `${botApprovals.length} approvals`} waiting`
                : isStreaming ? 'Follow reply' : 'Jump to latest'}
            </button>
          </div>
        )}
      </div>


      {/* Bottom Floating Pill Composer Input */}
      <div className="p-6 flex flex-col items-center z-20 bg-gradient-to-t from-[#09090b] via-[#09090b]/90 to-transparent">
        {/* Hidden file input: images and documents, several at once */}
        <input
          ref={fileInputRef}
          type="file"
          multiple
          onChange={handleFileSelect}
          className="hidden"
        />

        {/* Selected Image Thumbnail Preview Chip */}
        {selectedImage && (
          <div className="w-full max-w-2xl flex items-center justify-between bg-[#1c1c20] border border-[#2b2b32] px-3 py-1.5 rounded-xl mb-2 text-xs animate-fade-in shadow-md">
            <div className="flex items-center gap-2.5">
              <img
                src={selectedImage.previewUrl}
                alt="Selected Image Preview"
                className="w-9 h-9 rounded-lg object-cover border border-zinc-700 shadow-sm"
              />
              <div className="flex flex-col">
                <span className="text-zinc-200 font-semibold text-[11px] truncate max-w-[180px]">
                  {selectedImage.file?.name || 'image'}
                </span>
                <span className="text-[10px] text-zinc-400">
                  {selectedImage.isUploading
                    ? 'Uploading image...'
                    : selectedImage.error
                    ? `Upload notice: ${selectedImage.error}`
                    : 'Image ready'}
                </span>
              </div>
            </div>

            <button
              suppressHydrationWarning={true}
              type="button"
              onClick={() => setSelectedImage(null)}
              className="text-zinc-400 hover:text-white p-1 rounded-md hover:bg-[#2a2a30] transition"
              title="Remove image"
            >
              <FiX className="text-sm" />
            </button>
          </div>
        )}

        {/* After an undo: what came out can go back until something new is sent. */}
        {redoInfo?.available && (
          <div className="w-full max-w-2xl flex items-center justify-between gap-2 mb-2 px-3 py-1.5 rounded-xl border border-[#2b2b32] bg-[#141417] text-[11px] text-zinc-400">
            <span>
              Undone · {redoInfo.messages} message{redoInfo.messages === 1 ? '' : 's'} taken out{redoInfo.depth > 1 ? ` (${redoInfo.depth} undos)` : ''}. Sending something new makes this permanent.
            </span>
            <button type="button" onClick={redoLast} disabled={isStreaming} className="flex items-center gap-1 px-2 py-1 rounded-lg text-zinc-200 hover:text-white hover:bg-[#1f1f23] disabled:opacity-50 transition" title="/redo">
              <FiRotateCw className="text-xs" /> Redo
            </button>
          </div>
        )}

        {pendingRecording && (
          <div className="w-full max-w-2xl flex flex-wrap gap-1.5 mb-2">
            <div className="flex items-center gap-2 bg-[#1c1c20] border border-red-500/30 px-2.5 py-1.5 rounded-xl text-[11px] shadow-md">
              <FiVideo className="text-red-300 flex-shrink-0" />
              <span className="text-zinc-200">Demonstration · {pendingRecording.steps || 0} steps</span>
              <button type="button" onClick={() => setPendingRecording(null)} className="text-zinc-500 hover:text-white" title="Send without the demonstration">
                <FiX className="text-xs" />
              </button>
            </div>
          </div>
        )}

        {pendingFiles.length > 0 && (
          <div className="w-full max-w-2xl flex flex-wrap gap-1.5 mb-2">
            {pendingFiles.map((item) => (
              <div
                key={item.key}
                className={`flex items-center gap-2 bg-[#1c1c20] border px-2.5 py-1.5 rounded-xl text-[11px] shadow-md animate-fade-in ${item.status === 'error' ? 'border-rose-500/50' : 'border-[#2b2b32]'}`}
              >
                <FiFile className="text-zinc-400 flex-shrink-0" />
                <span className="text-zinc-200 font-semibold truncate max-w-[180px]" title={item.name}>{item.name}</span>
                <span className={`text-[10px] ${item.status === 'error' ? 'text-rose-300' : 'text-zinc-400'}`}>
                  {item.status === 'uploading' ? 'Uploading…' : item.status === 'error' ? item.error : formatFileSize(item.size)}
                </span>
                <button
                  type="button"
                  onClick={() => setPendingFiles((prev) => prev.filter((p) => p.key !== item.key))}
                  className="text-zinc-400 hover:text-white p-0.5 rounded-md hover:bg-[#2a2a30] transition"
                  title="Remove"
                >
                  <FiX className="text-xs" />
                </button>
              </div>
            ))}
          </div>
        )}

        {pickerOpen && (
          <CommandPicker
            commands={filteredCommands}
            activeIndex={pickerIndex}
            onPick={pickCommand}
            onHover={setPickerIndex}
          />
        )}

        {/* The composer. Its corners have a fixed radius (half its one-line height, so one line
            reads as a pill) and its buttons sit inset by the padding, centred on the corners'
            arcs: 6px padding + 16px button radius = the 22px corner radius, at any height. */}
        <form
          onSubmit={handleSendMessage}
          onDrop={handleDrop}
          onDragOver={(e) => e.preventDefault()}
          className="w-full max-w-2xl flex items-end gap-1 p-1.5 rounded-[22px] bg-[#1c1c20] border border-[#2b2b32] shadow-[0_10px_25px_-5px_rgba(0,0,0,0.5)] transition-colors focus-within:border-zinc-500"
        >
          <button
            suppressHydrationWarning={true}
            type="button"
            onClick={() => fileInputRef.current?.click()}
            className="w-8 h-8 flex-shrink-0 flex items-center justify-center rounded-full text-zinc-400 hover:text-white hover:bg-[#2a2a30] transition"
            title="Attach images or files (or drop and paste them here)"
          >
            <FiPlus className="text-base" />
          </button>

          {/* Enter sends, Shift+Enter adds a line, "/" opens the command picker. */}
          <textarea
            ref={textareaRef}
            suppressHydrationWarning={true}
            rows={1}
            value={inputPrompt}
            onChange={(e) => { setInputPrompt(e.target.value); setPickerDismissed(false); }}
            onKeyDown={handleComposerKeyDown}
            onPaste={handlePaste}
            placeholder={`Message ${botTitle}, or / for commands`}
            aria-autocomplete="list"
            aria-expanded={pickerOpen}
            className="flex-1 min-w-0 self-center bg-transparent px-1.5 py-1 text-[13px] leading-6 text-zinc-100 placeholder-zinc-500 focus:outline-none resize-none overflow-y-auto"
          />

          <button
            suppressHydrationWarning={true}
            type="button"
            onClick={handleVoiceToggle}
            className={`w-8 h-8 flex-shrink-0 flex items-center justify-center rounded-full transition ${
              isListening ? 'bg-rose-500 text-white animate-pulse' : 'text-zinc-400 hover:text-white hover:bg-[#2a2a30]'
            }`}
            title={isListening ? 'Stop dictating' : 'Dictate'}
          >
            {isListening ? <FiMicOff className="text-base" /> : <FiMic className="text-base" />}
          </button>

          {/* Send, or Stop while a reply streams (as /stop). */}
          {isStreaming ? (
            <button
              type="button"
              onClick={() => cancelTurn(threadId).catch((err) => addLocalMessage(`Could not stop the reply: ${err.message}`, true))}
              className="w-8 h-8 flex-shrink-0 flex items-center justify-center rounded-full bg-zinc-200 text-black hover:bg-white transition"
              title="Stop the reply"
            >
              <FiSquare className="text-[11px] fill-current" />
            </button>
          ) : (
            <button
              type="submit"
              disabled={!canSend}
              className="w-8 h-8 flex-shrink-0 flex items-center justify-center rounded-full bg-zinc-100 text-black hover:bg-white disabled:bg-[#2a2a30] disabled:text-zinc-500 transition"
              title="Send (Enter)"
            >
              <FiArrowUp className="text-base" />
            </button>
          )}
        </form>
      </div>
      </div>

      {bot && (
        <TodoCard
          bot={{ ...bot, id: threadId, todo: conversation?.todo || [] }}
          onSave={onConversationPatched ? (items) => onConversationPatched(threadId, { todo: items }, { save: true }) : null}
          hasMore={Boolean(project?.notes || conversation?.work_notes)}
        >
          <WorkNotesSection project={project} conversation={conversation} onSave={onSaveWorkNotes} onOpenProject={onOpenProject} />
        </TodoCard>
      )}
      </div>

      {/* A rewind that passes things the bot did: they stay done, so ask first. */}
      {rewindConfirm && (
        <div className="fixed inset-0 z-50 bg-black/70 flex items-center justify-center p-4" onMouseDown={(e) => { if (e.target === e.currentTarget) setRewindConfirm(null); }}>
          <div className="w-full max-w-lg rounded-2xl border border-[#2b2b32] bg-[#111113] p-5 flex flex-col gap-3 shadow-2xl">
            <div className="flex items-center gap-2 text-sm font-semibold text-zinc-100">
              <FiAlertTriangle className="text-amber-300" /> Take back {rewindConfirm.preview.messages} message{rewindConfirm.preview.messages === 1 ? '' : 's'}?
            </div>
            <p className="text-xs text-zinc-400">
              The conversation goes back to before your message, and the message returns to the box. What {botTitle} did in the meantime is not undone:
            </p>
            <ul className="max-h-56 overflow-auto rounded-xl border border-[#26262b] bg-[#0b0b0d] p-3 space-y-1 text-[11px] font-mono text-zinc-300">
              {rewindConfirm.preview.actions.map((action, index) => (
                <li key={index} className="break-words">
                  <span className="text-zinc-500">{action.tool}</span>{action.summary ? ` · ${action.summary}` : ''}
                </li>
              ))}
            </ul>
            {(rewindConfirm.preview.notes_changed || rewindConfirm.preview.todo_changed) && (
              <p className="text-[11px] text-zinc-500">
                Its {rewindConfirm.preview.notes_changed ? 'notes' : ''}{rewindConfirm.preview.notes_changed && rewindConfirm.preview.todo_changed ? ' and ' : ''}{rewindConfirm.preview.todo_changed ? 'to-do list' : ''} changed too; they stay as they are and the bot is shown them again.
              </p>
            )}
            <div className="flex items-center justify-end gap-2">
              <button type="button" onClick={() => setRewindConfirm(null)} className="px-3 py-1.5 rounded-xl text-xs font-semibold border bg-zinc-800 text-zinc-300 border-zinc-700 hover:bg-zinc-700 transition">
                Cancel
              </button>
              <button type="button" onClick={() => performRewind(rewindConfirm.messageId)} disabled={rewinding} className="px-3 py-1.5 rounded-xl text-xs font-semibold bg-zinc-100 text-zinc-900 hover:bg-white disabled:opacity-50 transition">
                {rewinding ? 'Taking back…' : 'Take back anyway'}
              </button>
            </div>
          </div>
        </div>
      )}

    </div>
  );
}
