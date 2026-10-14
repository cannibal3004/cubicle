'use client';

import React, { useState, useRef, useEffect } from 'react';
import MessageItem from './MessageItem';
import ApprovalCard from './ApprovalCard';
import CommandPicker from './CommandPicker';
import ModelPicker from './ModelPicker';
import ToolsPopover from './ToolsPopover';
import ContextMeter from './ContextMeter';
import TodoCard from './TodoCard';
import MascotAvatar from './MascotAvatar';
import { FiPlus, FiMic, FiMicOff, FiMonitor, FiX, FiImage, FiEdit2, FiArrowDown } from 'react-icons/fi';
import {
  sendMessage,
  subscribeToChatStream,
  startTurn,
  fetchTurnStatus,
  cancelTurn,
  uploadImage,
  respondApproval,
  fetchCommands,
  clearChatHistory,
  compactThread,
  fetchChatHistory,
} from '../lib/api';

const AUTO_APPROVAL_MODES = ['off', 'shadow', 'on', 'inherit'];

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

export default function ChatWindow({ bot, models, catalogError, onRefreshModels, messages, setMessagesFor, streamingBots, onStreamingChange, turnStates, onUpdateBotModel, onEditBot, onUpdateBot, onBotPatched, onToggleComputer, onOpenAudit, onOpenSettings, defaultModel, prefill }) {
  const [inputPrompt, setInputPrompt] = useState('');
  // Streaming is tracked per bot by the Dashboard so a reply keeps going
  // while another bot or tab is shown.
  const isStreaming = Boolean(bot?.id && streamingBots?.[bot.id]);
  const setStreamingFor = (botId, value) => {
    if (onStreamingChange) onStreamingChange(botId, value);
  };

  // One draft per bot: switching bots must not carry or lose typed text.
  const draftsRef = useRef({});
  const draftBotRef = useRef(bot?.id);
  useEffect(() => {
    const previous = draftBotRef.current;
    const next = bot?.id;
    if (previous === next) return;
    if (previous) draftsRef.current[previous] = inputPrompt;
    setInputPrompt(draftsRef.current[next] || '');
    draftBotRef.current = next;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bot?.id]);
  const [isListening, setIsListening] = useState(false);
  const [activeModel, setActiveModel] = useState(bot?.model || defaultModel || '');
  const [selectedImage, setSelectedImage] = useState(null);
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
    if (prefill?.text) setInputPrompt(prefill.text);
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

  const botApprovals = pendingApprovals.filter((approval) => approval.botId === bot?.id);
  const botTakeovers = takeoverRequests.filter((request) => request.botId === bot?.id);

  // Switching bots shows a different thread: start it at the bottom.
  useEffect(() => {
    followRef.current = true;
    setShowJumpPill(false);
    scrollToBottom('auto');
  }, [bot?.id]);

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

  const handleImageSelect = async (e) => {
    const file = e.target.files?.[0];
    if (!file) return;

    // Strict IMAGE ONLY validation
    if (!file.type.startsWith('image/')) {
      alert('Only image files (JPEG, PNG, WEBP, GIF, AVIF) are allowed.');
      return;
    }

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

  // One live subscription per bot. Attaching replays the turn's events from
  // `after`, so a reload or another machine rebuilds the reply in progress.
  const attachmentsRef = useRef({});
  const attachToTurn = (botId, turnId, after = 0) => {
    const existing = attachmentsRef.current[botId];
    if (existing && (!turnId || existing.turnId === turnId)) return;
    if (existing) existing.close();

    let streamingMsgId = null;
    const updateMessages = (updater) => setMessagesFor(botId, updater);

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
        prev.map((msg) =>
          msg.id === streamingMsgId
            ? { ...msg, text: msg.text + text, reasoning: (msg.reasoning || '') + reasoning }
            : msg
        )
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
      setStreamingFor(botId, false);
      const current = attachmentsRef.current[botId];
      if (current && current.turnId === turnId) {
        current.close();
        delete attachmentsRef.current[botId];
      }
    };

    setStreamingFor(botId, true);
    const close = subscribeToChatStream(
      botId,
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
        } else if (event.type === 'request.opened') {
          setPendingApprovals((prev) => [
            ...prev.filter((approval) => approval.requestId !== event.requestId),
            { ...event, botId },
          ]);
        } else if (event.type === 'gate.scored') {
          // The auto-approval gate's verdict on a pending card. If it approved
          // the action, the card is about to be replaced by tool.started.
          setPendingApprovals((prev) =>
            prev.map((approval) => (approval.requestId === event.requestId ? { ...approval, gate: event.verdict } : approval))
          );
        } else if (event.type === 'computer.takeover_requested') {
          setTakeoverRequests((prev) => [
            ...prev.slice(-4),
            { id: `${event.botMsgId}-${Date.now()}`, reason: event.reason || '', botId },
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
        } else if (['tool.started', 'tool.completed', 'tool.failed', 'tool.denied', 'tool.expired'].includes(event.type)) {
          if (event.type === 'tool.completed' && String(event.callName || '').startsWith('todo_') && Array.isArray(event.result?.items) && onBotPatched) {
            // A todo result carries the whole list: show it without a refetch.
            onBotPatched(botId, { todo: event.result.items });
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
              const others = (msg.toolCalls || []).filter((call) => call.id !== key);
              return {
                ...msg,
                toolCalls: [
                  ...others,
                  {
                    id: key,
                    name: event.callName || event.tool,
                    status: event.type.replace('tool.', ''),
                    error: event.error || null,
                    gate: event.gate || null,
                  },
                ],
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
    attachmentsRef.current[botId] = { turnId, close };
  };

  useEffect(() => () => {
    Object.values(attachmentsRef.current).forEach((attachment) => attachment.close());
  }, []);

  // Follow a turn that is already running for this bot: after a reload, a
  // bot switch, or when the server reports one started elsewhere.
  const remoteTurnId = bot?.id ? turnStates?.[bot.id]?.turn_id : undefined;
  useEffect(() => {
    const botId = bot?.id;
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
  }, [bot?.id, remoteTurnId]);

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

  // Auto-grow the composer up to ~8 lines; /shared write needs a second line.
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  }, [inputPrompt]);

  const addLocalMessage = (text, isError = false) => {
    if (!bot?.id) return;
    setMessagesFor(bot.id, (prev) => [
      ...prev,
      { id: `local-${Date.now()}-${Math.random().toString(16).slice(2, 6)}`, sender: 'system', text, isError, local: true, created_at: new Date().toISOString() },
    ]);
  };

  const runClientCommand = async (spec, args) => {
    const botId = bot?.id;
    if (!botId) return;
    switch (spec.name) {
      case 'help': {
        const lines = commands.map((c) => `${c.usage}\n    ${c.description}`);
        addLocalMessage(`Slash commands\n\n${lines.join('\n')}`);
        return;
      }
      case 'clear': {
        if (!window.confirm(`Delete the whole conversation with ${botTitle}? This cannot be undone.`)) return;
        try {
          await clearChatHistory(botId);
          setMessagesFor(botId, () => []);
          addLocalMessage('Conversation cleared.');
        } catch (err) {
          addLocalMessage(`Could not clear the conversation: ${err.message}`, true);
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
      case 'auto': {
        const mode = args.trim().toLowerCase();
        if (!AUTO_APPROVAL_MODES.includes(mode)) {
          addLocalMessage(`Auto-approval for this bot: ${bot?.auto_approval || 'inherit'}\nUsage: /auto off|shadow|on|inherit`, mode !== '');
          return;
        }
        if (!onUpdateBot) return;
        try {
          await onUpdateBot(botId, { auto_approval: mode });
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
    }
  };

  const handleSendMessage = async (e) => {
    e?.preventDefault();
    await submitText(inputPrompt);
  };

  const submitText = async (rawText) => {
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
    if ((!rawText.trim() && !selectedImage) || isStreaming) return;

    const botId = bot?.id;
    if (!botId) return;
    const updateMessages = (updater) => setMessagesFor(botId, updater);
    const userText = rawText;
    const currentSelected = selectedImage;
    
    setInputPrompt('');
    setSelectedImage(null);
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
      created_at: new Date().toISOString(),
    };
    updateMessages((prev) => [...prev, userMsgObj]);

    try {
      const sent = await sendMessage(botId, botId, userText, activeModel, finalImageUrl);
      // Take the server's id for the message, so a later history merge (a
      // turn seen through the poll, a reload) recognises it as the same one.
      if (sent?.message?.id) {
        updateMessages((prev) =>
          prev.map((msg) => (msg.id === userMsgObj.id ? { ...msg, id: sent.message.id, created_at: sent.message.created_at || msg.created_at } : msg))
        );
      }
      const started = await startTurn(botId, activeModel);
      attachToTurn(botId, started?.turn?.turn_id || null, 0);
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
    <div className="flex-1 flex flex-col h-screen overflow-hidden bg-[#09090b] relative select-none font-sans text-zinc-100">
      {/* Top Header Bar */}
      <header className="px-6 py-3.5 flex items-center justify-between z-20 bg-[#09090b]/80 backdrop-blur-md border-b border-[#18181c]">
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
          {bot && onEditBot && (
            <button
              type="button"
              onClick={onEditBot}
              className="p-1 rounded-md text-zinc-500 hover:text-white hover:bg-[#1f1f23] transition"
              title="Edit this bot"
            >
              <FiEdit2 className="text-xs" />
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

          {bot && onUpdateBot && (
            <ToolsPopover
              bot={bot}
              onSave={(toolSettings) => onUpdateBot(bot.id, { tool_settings: toolSettings })}
              onSaveAutoApproval={(mode) => onUpdateBot(bot.id, { auto_approval: mode })}
              onSaveGateThreshold={(value) => onUpdateBot(bot.id, { gate_threshold: value === null ? 'inherit' : value })}
              onOpenEditor={onEditBot}
            />
          )}

          <button
            suppressHydrationWarning={true}
            onClick={onToggleComputer}
            className="p-1.5 rounded-lg text-zinc-400 hover:text-white hover:bg-[#1f1f23] transition"
            title="Toggle Desktop Screen Preview"
          >
            <FiMonitor className="text-base" />
          </button>
        </div>
      </header>

      {bot && <TodoCard bot={bot} onSave={onUpdateBot ? (items) => onUpdateBot(bot.id, { todo: items }) : null} />}

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
            <MessageItem key={msg.id} message={msg} botId={bot?.id} />
          ))}

          {/* Approval and takeover cards sit where the conversation currently is, not at the top. */}
          {botApprovals.map((approval) => (
            <ApprovalCard
              key={approval.requestId}
              approval={approval}
              onRespond={handleApprovalResponse}
            />
          ))}

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
                    Open the computer, press Take control, do the step, then Hand back. The bot will continue when you tell it you are done.
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
                    if (onToggleComputer) onToggleComputer();
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
        {/* Hidden Image File Input */}
        <input
          ref={fileInputRef}
          type="file"
          accept="image/*"
          onChange={handleImageSelect}
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
                  {selectedImage.file.name}
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

        {pickerOpen && (
          <CommandPicker
            commands={filteredCommands}
            activeIndex={pickerIndex}
            onPick={pickCommand}
            onHover={setPickerIndex}
          />
        )}

        <form
          onSubmit={handleSendMessage}
          className="w-full max-w-2xl dark-pill-input px-4 py-2.5 flex items-end gap-3 bg-[#1c1c20] border border-[#2b2b32] shadow-2xl transition focus-within:border-zinc-500"
        >
          {/* Plus / Image Upload Action Button */}
          <button
            suppressHydrationWarning={true}
            type="button"
            onClick={() => fileInputRef.current?.click()}
            className="text-zinc-400 hover:text-white transition p-1 text-base flex-shrink-0"
            title="Upload Image (JPEG, PNG, WEBP, GIF, AVIF)"
          >
            <FiPlus />
          </button>

          {/* Composer. Enter sends, Shift+Enter adds a line, "/" opens the command picker. */}
          <textarea
            ref={textareaRef}
            suppressHydrationWarning={true}
            rows={1}
            value={inputPrompt}
            onChange={(e) => { setInputPrompt(e.target.value); setPickerDismissed(false); }}
            onKeyDown={handleComposerKeyDown}
            placeholder={`Message ${botTitle}, or / for commands`}
            aria-autocomplete="list"
            aria-expanded={pickerOpen}
            className="w-full bg-transparent text-xs text-zinc-100 placeholder-zinc-500 focus:outline-none resize-none leading-5 py-0.5 max-h-40 overflow-y-auto"
          />

          {/* Microphone Dictation Button */}
          <button
            suppressHydrationWarning={true}
            type="button"
            onClick={handleVoiceToggle}
            className={`p-1.5 rounded-full text-base transition flex-shrink-0 ${
              isListening
                ? 'bg-rose-500 text-white animate-pulse'
                : 'text-zinc-400 hover:text-white'
            }`}
            title="Dictate Voice Input"
          >
            {isListening ? <FiMicOff /> : <FiMic />}
          </button>

        </form>
      </div>

    </div>
  );
}
