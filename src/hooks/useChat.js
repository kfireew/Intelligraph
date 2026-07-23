import { useState, useCallback, useRef, useEffect, useMemo } from "react";

// ── Conversation persistence (SQLite backend + localStorage cache) ──

const STORAGE_PREFIX = "intelligraph-chat-";
const SAVE_DEBOUNCE_MS = 1000;

async function loadConversations(pid) {
  if (!pid) return [];
  try {
    const r = await fetch(`/api/v1/projects/${pid}/conversations`);
    if (r.ok) return await r.json();
  } catch { /* server unavailable */ }
  // Fallback to localStorage
  try {
    const raw = localStorage.getItem(STORAGE_PREFIX + pid);
    return raw ? JSON.parse(raw) : [];
  } catch { return []; }
}

function cacheConversations(pid, conversations) {
  if (!pid) return;
  try {
    localStorage.setItem(STORAGE_PREFIX + pid, JSON.stringify(conversations.slice(-50)));
  } catch { /* quota exceeded, ignore */ }
}

async function saveConversationsToServer(pid, conversations) {
  if (!pid) return;
  try {
    await fetch(`/api/v1/projects/${pid}/conversations`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(conversations.slice(-50)),
    });
  } catch { /* server unavailable, localStorage cache is fallback */ }
}

async function deleteConversationFromServer(pid, convId) {
  if (!pid || !convId) return;
  try {
    await fetch(`/api/v1/projects/${pid}/conversations/${convId}`, { method: "DELETE" });
  } catch { /* server unavailable */ }
}

// ── Helpers ──

function generateId() {
  return Date.now().toString(36) + Math.random().toString(36).slice(2);
}

function autoTitle(text) {
  const t = text.trim().replace(/^["']|["']$/g, "");
  return t.length > 50 ? t.slice(0, 47) + "\u2026" : t;
}

export function useChat({ activePid, llmUrl, llmToken, model, onMatchedNodes, onAnswerComplete, onSendMessage, onTokenExpired }) {
  const [conversations, setConversations] = useState([]);
  const [activeConvId, setActiveConvId] = useState(null);
  const [status, setStatus] = useState("idle");
  const [streamingContent, setStreamingContent] = useState("");
  const [progressSteps, setProgressSteps] = useState([]);
  const [pathWarnings, setPathWarnings] = useState(null);
  const abortRef = useRef(null);
  const prevPidRef = useRef(activePid);
  const hasLoadedRef = useRef(false);
  const saveTimerRef = useRef(null);
  const _lastTraceId = useRef("");

  // ── Derived: active conversation + messages ──

  const activeConv = useMemo(
    () => conversations.find((c) => c.id === activeConvId) || null,
    [conversations, activeConvId],
  );
  const messages = useMemo(
    () => (activeConv ? activeConv.messages : []),
    [activeConv],
  );

  // ── Conversation CRUD ──

  const newConversation = useCallback(() => {
    const id = generateId();
    setConversations((prev) => [
      { id, title: "New Chat", messages: [], createdAt: new Date().toISOString() },
      ...prev,
    ]);
    setActiveConvId(id);
  }, []);

  const deleteConversation = useCallback((id) => {
    // Delete from server immediately (not debounced) so it survives restarts
    if (activePid) deleteConversationFromServer(activePid, id);
    setConversations((prev) => {
      const next = prev.filter((c) => c.id !== id);
      if (activeConvId === id) {
        setActiveConvId(next.length > 0 ? next[0].id : null);
      }
      if (activePid) cacheConversations(activePid, next);
      return next;
    });
  }, [activeConvId, activePid]);

  const switchConversation = useCallback((id) => {
    setActiveConvId(id);
  }, []);

  const renameConversation = useCallback((id, title) => {
    setConversations((prev) =>
      prev.map((c) => (c.id === id ? { ...c, title } : c)),
    );
  }, []);

  // ── addMessage -- accepts optional convId to avoid stale-closure races ──

  const addMessage = useCallback((msg, convId) => {
    const targetId = convId !== undefined ? convId : activeConvId;
    if (!targetId) return;
    setConversations((prev) =>
      prev.map((c) => {
        if (c.id === targetId) {
          return {
            ...c,
            messages: [
              ...c.messages,
              { ...msg, id: generateId(), createdAt: new Date().toISOString() },
            ],
          };
        }
        return c;
      }),
    );
  }, [activeConvId]);

  // ── Persistence effects ──

  // Save effect — debounced, skipped until first load completes
  useEffect(() => {
    if (!activePid) return;
    // Skip save until we've loaded from server (fixes race condition:
    // on mount, conversations is [] and would overwrite saved chats)
    if (!hasLoadedRef.current) return;
    // Cache to localStorage immediately
    cacheConversations(activePid, conversations);
    // Debounce server save
    if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
    saveTimerRef.current = setTimeout(() => {
      saveConversationsToServer(activePid, conversations);
    }, SAVE_DEBOUNCE_MS);
    return () => {
      if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
    };
  }, [conversations, activePid]);

  // Load effect — runs when activePid changes (including on mount)
  useEffect(() => {
    if (!activePid) return;
    let cancelled = false;
    const prev = prevPidRef.current;
    // Save previous project's conversations before switching
    if (prev && prev !== activePid && hasLoadedRef.current) {
      cacheConversations(prev, conversations);
      saveConversationsToServer(prev, conversations);
    }
    hasLoadedRef.current = false;
    (async () => {
      const convs = await loadConversations(activePid);
      if (cancelled) return;
      setConversations(convs);
      setActiveConvId(convs.length > 0 ? convs[0].id : null);
      hasLoadedRef.current = true;
      prevPidRef.current = activePid;
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activePid]);

  // ── sendMessage ──

  const sendMessage = useCallback(async (prompt) => {
    if (!prompt.trim()) return;
    const trimmed = prompt.trim();

    // Resolve target convId -- auto-create if none active
    let targetConvId = activeConvId;
    if (!targetConvId) {
      targetConvId = generateId();
      setConversations((prev) => [
        { id: targetConvId, title: autoTitle(trimmed), messages: [], createdAt: new Date().toISOString() },
        ...prev,
      ]);
      setActiveConvId(targetConvId);
    } else {
      // Auto-title from first user message if still "New Chat" and empty
      setConversations((prev) =>
        prev.map((c) => {
          if (c.id === targetConvId && c.title === "New Chat" && c.messages.length === 0) {
            return { ...c, title: autoTitle(trimmed) };
          }
          return c;
        }),
      );
    }

    addMessage({ role: "user", content: trimmed }, targetConvId);

    setStatus("thinking");
    setStreamingContent("");
    setPathWarnings(null);
    let intent = "planner";

    if (!llmUrl) {
      addMessage({ role: "assistant", content: "No LLM endpoint configured. Click the settings icon to set your LLM URL and token.", metadata: { intent, route: { category: intent, label: intent } } }, targetConvId);
      setStatus("idle");
      return;
    }

    if (!model) {
      addMessage({ role: "assistant", content: "No model selected. Go to the LLM tab to select a model.", metadata: { intent, route: { category: intent, label: intent } } }, targetConvId);
      setStatus("idle");
      return;
    }

    // Read tuning settings directly from localStorage (not stale props)
    const maxTokens = parseInt(localStorage.getItem("tuning-max-tokens") || "4096", 10);
    const budgetChars = parseInt(localStorage.getItem("tuning-budget-chars") || "12000", 10);
    const embeddingWeight = parseFloat(localStorage.getItem("tuning-embedding-weight") || "0.4");
    const traversalDepth = parseInt(localStorage.getItem("tuning-traversal-depth") || "2", 10);
    const snippetChars = parseInt(localStorage.getItem("tuning-snippet-chars") || "1500", 10);
    let chunkCaps = null;
    try { chunkCaps = JSON.parse(localStorage.getItem("tuning-chunk-caps") || "null"); } catch {}

    // Single request to completions endpoint — backend does retrieval + LLM internally
    setStatus("answering");
    setProgressSteps([]);
    let fullText = "";
    let sources = null;
    let matchedNodes = [];
    let pw = null;
    let savings = null;
    let lastStep = "";
    try {
      // Chat compaction: most recent 2 messages full, older messages compressed into a summary
      const priorMessages = (activeConv?.messages || [])
        .filter(m => !m.content.startsWith("(LLM error") && !m.content.startsWith("(LLM request failed") && !m.content.startsWith("(No response"))
        .slice(-6);
      let conversationHistory;
      if (priorMessages.length <= 2) {
        conversationHistory = priorMessages.map(m => ({
          role: m.role,
          content: m.content.length > 2000 ? m.content.slice(0, 2000) + "..." : m.content,
        }));
      } else {
        const recent = priorMessages.slice(-2);
        const older = priorMessages.slice(0, -2);
        const summaryParts = older.map(m => {
          const c = m.content.length > 300 ? m.content.slice(0, 300) + "..." : m.content;
          return `${m.role}: ${c}`;
        });
        conversationHistory = [
          { role: "user", content: `Previous conversation context:\n${summaryParts.join("\n")}` },
          ...recent.map(m => ({
            role: m.role,
            content: m.content.length > 2000 ? m.content.slice(0, 2000) + "..." : m.content,
          })),
        ];
      }

      const resp = await fetch(`/api/v1/projects/${activePid}/completions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          prompt: trimmed,
          conversation_history: conversationHistory,
          llm_url: llmUrl,
          llm_token: llmToken,
          model: model,
          max_tokens: maxTokens,
          budget_chars: budgetChars,
          chunk_caps: chunkCaps,
          embedding_weight: embeddingWeight,
          traversal_depth: traversalDepth,
          snippet_chars: snippetChars,
        }),
      });

      if (!resp.ok) {
        const errBody = await resp.json().catch(() => ({}));
        fullText = `(LLM error ${resp.status}: ${errBody.error?.message || errBody.detail || errBody.error || "unknown"})`;
      } else {
        // Stream NDJSON: read progress events + final answer
        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop();
          for (const line of lines) {
            if (!line.trim()) continue;
            let msg;
            try { msg = JSON.parse(line); } catch { continue; }
            if (msg.type === "progress") {
              lastStep = msg.message;
              setProgressSteps(prev => [...prev, { step: msg.step, message: msg.message, time: Date.now() }]);
            } else if (msg.type === "answer") {
              fullText = msg.answer || "";
              if (!fullText) {
                fullText = "(No response -- the LLM returned empty output. Try rephrasing your question.)";
              }
              intent = msg.intent || "planner";
              sources = msg.sources || null;
              matchedNodes = msg.matched_nodes || [];
              pw = msg.path_warnings || [];
              savings = msg.context_savings || null;
              _lastTraceId.current = msg.trace_id || "";
            } else if (msg.type === "error") {
              const stepLabel = lastStep || msg.step || "unknown";
              fullText = `(Error at: ${stepLabel})\n${msg.error || "unknown error"}`;
            }
          }
        }
      }
    } catch (e) {
      if (lastStep) {
        fullText = `(Connection lost while: ${lastStep})`;
      } else {
        fullText = `(LLM request failed: ${e.message || "unknown error"})`;
      }
    }

    if (onMatchedNodes && matchedNodes?.length) onMatchedNodes(matchedNodes);
    setPathWarnings(pw);
    addMessage({
      role: "assistant",
      content: fullText,
      metadata: { intent, route: { category: intent, label: intent }, pathWarnings: pw, sources, savings },
    }, targetConvId);
    setStreamingContent("");
    setProgressSteps([]);
    setStatus("idle");
    onAnswerComplete?.();
  }, [addMessage, activeConvId, activePid, llmUrl, llmToken, model, onMatchedNodes, onAnswerComplete]);

  const clearChats = useCallback((pid) => {
    localStorage.removeItem(STORAGE_PREFIX + pid);
    if (pid) saveConversationsToServer(pid, []);
    setConversations([]);
    setActiveConvId(null);
  }, []);

  const sendFeedback = useCallback(async (rating, comment) => {
    const traceId = _lastTraceId.current;
    if (!activePid || !traceId) return;
    try {
      await fetch(`/api/v1/projects/${activePid}/feedback`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ trace_id: traceId, rating, comment: comment || "" }),
      });
    } catch { /* non-blocking */ }
  }, [activePid]);

  return {
    clearChats,
    messages,
    conversations,
    activeConvId,
    status,
    streamingContent,
    progressSteps,
    pathWarnings,
    sendMessage,
    newConversation,
    deleteConversation,
    switchConversation,
    renameConversation,
    sendFeedback,
  };
}
