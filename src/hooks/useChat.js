import { useState, useCallback, useRef, useEffect, useMemo } from "react";
import { llmService } from "../services/llmService";

// ── Prompt builders (graphify + CRG + code-chunks context) ──

const SYSTEM_PROMPT = `You are an expert software architect helping a developer understand a codebase.

Give a direct, concise answer. Do not output your thinking process or say "Let me analyze" -- just answer.
Use the provided context as your only source of truth. Mention specific file paths.
If context is insufficient, state what is missing.
Do not invent files, functions, imports, or APIs. Format file references as a markdown list with newlines (one per line).`;



// ── LocalStorage persistence for conversations ──

const STORAGE_PREFIX = "intelligraph-chat-";

function loadConversations(pid) {
  if (!pid) return [];
  try {
    const raw = localStorage.getItem(STORAGE_PREFIX + pid);
    return raw ? JSON.parse(raw) : [];
  } catch { return []; }
}

function saveConversations(pid, conversations) {
  if (!pid) return;
  try {
    localStorage.setItem(STORAGE_PREFIX + pid, JSON.stringify(conversations.slice(-20)));
  } catch { /* quota exceeded, ignore */ }
}

// ── Helpers ──

function generateId() {
  return Date.now().toString(36) + Math.random().toString(36).slice(2);
}

function autoTitle(text) {
  const t = text.trim().replace(/^["']|["']$/g, "");
  return t.length > 50 ? t.slice(0, 47) + "\u2026" : t;
}

export function useChat({ activePid, llmUrl, llmToken, model, onMatchedNodes, onAnswerComplete, onSendMessage }) {
  const [conversations, setConversations] = useState([]);
  const [activeConvId, setActiveConvId] = useState(null);
  const [status, setStatus] = useState("idle");
  const [streamingContent, setStreamingContent] = useState("");
  const [pathWarnings, setPathWarnings] = useState(null);
  const abortRef = useRef(null);
  const prevPidRef = useRef(activePid);

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
    setConversations((prev) => {
      const next = prev.filter((c) => c.id !== id);
      if (activeConvId === id) {
        setActiveConvId(next.length > 0 ? next[0].id : null);
      }
      return next;
    });
  }, [activeConvId]);

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

  useEffect(() => {
    if (activePid && conversations.length > 0) {
      saveConversations(prevPidRef.current, conversations);
    }
  }, [conversations]);

  useEffect(() => {
    const prev = prevPidRef.current;
    if (prev && prev !== activePid && conversations.length > 0) {
      saveConversations(prev, conversations);
    }
    const convs = loadConversations(activePid);
    setConversations(convs);
    setActiveConvId(convs.length > 0 ? convs[0].id : null);
    prevPidRef.current = activePid;
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

    setStatus("classifying");
    setStreamingContent("");
    setPathWarnings(null);
    // Intent handled server-side by retrieval.py planner
    let intent = "architecture"

    // Build rich context — backend-owned retrieval
    setStatus("thinking");
    // We keep the old single-value return for caller compatibility;
    // but also extract matchedNodes from the full response
    let matchedNodes = [];
    const richContextResp = await (async () => {
      if (!activePid) return { context: "", matchedNodes: [] };
      try {
        const resp = await fetch("/graph/retrieve-context", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ prompt: trimmed, project_id: activePid }),
        });
        if (!resp.ok) return { context: "", matchedNodes: [] };
        const data = await resp.json();
        matchedNodes = data.matched_nodes || [];
        return { context: data.context || "", matchedNodes };
      } catch {
        return { context: "", matchedNodes: [] };
      }
    })();
    const richContext = richContextResp.context;
    if (onMatchedNodes && matchedNodes?.length) onMatchedNodes(matchedNodes);

    if (!llmUrl) {
      addMessage({ role: "assistant", content: "No LLM endpoint configured. Click the settings icon to set your LLM URL and token.", metadata: { intent, route: { category: intent, label: intent } } }, targetConvId);
      setStatus("idle");
      return;
    }

    // LLM call — non-streaming relay (LLM provider doesn't support streaming)
    setStatus("answering");
    let fullText = "";
    let pw = null;
    try {
      const priorMessages = (activeConv?.messages || []).slice(-8);
      const historyMessages = priorMessages.map(m => ({
        role: m.role,
        content: m.content.length > 200 ? m.content.slice(0, 200) + "..." : m.content,
      }));

      const j = await llmService.relay({
        url: llmUrl,
        token: llmToken,
        payload: {
          model: model || undefined,
          messages: [
            { role: "system", content: SYSTEM_PROMPT },
            ...(richContext ? [{ role: "system", content: `Project context:\n${richContext}` }] : []),
            ...historyMessages,
            { role: "user", content: trimmed },
          ],
          max_tokens: 4096,
          temperature: 0.2,
        },
        projectId: activePid,
      });

      if (j.status !== 200) {
        const errBody = JSON.parse(j.body || "{}");
        fullText = `(LLM error ${j.status}: ${errBody.error?.message || errBody.detail || j.body?.slice(0, 200) || "unknown"})`;
      } else {
        const body = JSON.parse(j.body || "{}");
        fullText = body.choices?.[0]?.message?.content || "";
        if (!fullText) {
          fullText = "(No response -- the LLM returned empty output. Try rephrasing your question.)";
        }
      }
    } catch (e) {
      fullText = `(LLM request failed: ${e.message || "unknown error"})`;
    }

    setPathWarnings(pw);
    addMessage({
      role: "assistant",
      content: fullText,
      metadata: { intent, route: { category: intent, label: intent }, pathWarnings: pw },
    }, targetConvId);
    setStreamingContent("");
    setStatus("idle");
    onAnswerComplete?.();
  }, [addMessage, activeConvId, activePid, llmUrl, llmToken, model, onMatchedNodes, onAnswerComplete]);

  const clearChats = useCallback((pid) => {
    localStorage.removeItem(STORAGE_PREFIX + pid);
    setConversations([]);
    setActiveConvId(null);
  }, []);

  return {
    clearChats,
    messages,
    conversations,
    activeConvId,
    status,
    streamingContent,
    pathWarnings,
    sendMessage,
    newConversation,
    deleteConversation,
    switchConversation,
    renameConversation,
  };
}
