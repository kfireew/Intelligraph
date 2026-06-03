import { useRef, useEffect, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { ChatMessage } from "./ChatMessage";
import { PromptComposer } from "./PromptComposer";
import { StatusPill } from "./StatusPill";
import { Settings, Zap, Plus, Trash2, MessageSquare, PanelLeftClose, PanelLeft, ChevronLeft } from "lucide-react";

const STATUS_PILL = {
  idle: null,
  classifying: { text: "Classifying", tone: "info" },
  thinking: { text: "Thinking", tone: "info" },
  answering: { text: "Streaming", tone: "success" },
  error: { text: "Error", tone: "error" },
};

function formatTime(iso) {
  try {
    const d = new Date(iso);
    const now = new Date();
    const diff = now - d;
    if (diff < 3600000) return `${Math.floor(diff / 60000)}m ago`;
    if (diff < 86400000) return `${Math.floor(diff / 3600000)}h ago`;
    return d.toLocaleDateString();
  } catch { return ""; }
}

export function ChatPanel({
  messages,
  conversations,
  activeConvId,
  status,
  streamingContent,
  onSend,
  activeProject,
  graphData,
  llmConfigured,
  onGoToLLM,
  newConversation,
  deleteConversation,
  switchConversation,
  graphCollapsed,
  onToggleGraphCollapse,
}) {
  const bottomRef = useRef(null);
  const [sidebarOpen, setSidebarOpen] = useState(true);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, streamingContent]);

  const pill = STATUS_PILL[status];

  return (
    <div className="flex flex-row min-w-0 flex-1 min-h-0">
      {/* Conversation List Sidebar */}
      <AnimatePresence>
        {sidebarOpen && (
          <motion.div
            initial={{ width: 0, opacity: 0 }}
            animate={{ width: 220, opacity: 1 }}
            exit={{ width: 0, opacity: 0 }}
            transition={{ duration: 0.15 }}
            className="border-r border-border flex flex-col min-h-0 overflow-hidden"
            style={{ minWidth: 0 }}
          >
            <div className="flex items-center justify-between px-3 py-2.5 border-b border-border">
              <span className="text-xs font-bold text-text">Conversations</span>
              <button
                onClick={() => setSidebarOpen(false)}
                className="p-1 rounded hover:bg-surface text-muted hover:text-text transition-colors"
                title="Close sidebar"
              >
                <PanelLeftClose size={14} />
              </button>
            </div>
            <button
              onClick={newConversation}
              className="mx-2 mt-2 flex items-center justify-center gap-1.5 px-3 py-2 rounded-lg text-xs font-bold text-white bg-accent hover:bg-accent-light transition-colors"
            >
              <Plus size={14} />
              New Chat
            </button>
            <div className="flex-1 min-h-0 overflow-y-auto px-2 py-2 space-y-1">
              {(conversations || []).map((conv) => (
                <div
                  key={conv.id}
                  onClick={() => switchConversation(conv.id)}
                  className={`group flex items-center gap-2 px-2.5 py-2 rounded-lg cursor-pointer transition-colors ${
                    conv.id === activeConvId
                      ? "bg-accent/15 text-accent-light"
                      : "text-text hover:bg-surface"
                  }`}
                >
                  <MessageSquare size={14} className="shrink-0 opacity-60" />
                  <div className="flex-1 min-w-0">
                    <div className="text-xs font-medium truncate">{conv.title || "New Chat"}</div>
                    <div className="text-[10px] text-muted">{formatTime(conv.createdAt)}</div>
                  </div>
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      deleteConversation(conv.id);
                    }}
                    className="p-0.5 rounded opacity-0 group-hover:opacity-100 hover:bg-red-500/10 text-muted hover:text-red-400 transition-all"
                    title="Delete conversation"
                  >
                    <Trash2 size={12} />
                  </button>
                </div>
              ))}
              {(!conversations || conversations.length === 0) && (
                <div className="text-center py-6">
                  <p className="text-xs text-muted">No conversations yet</p>
                </div>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Main Chat Area */}
      <div className="flex flex-col min-w-0 flex-1 min-h-0">
        {/* Header */}
        <div className="glass flex items-center justify-between px-4 py-3 border-b border-border min-h-[52px]">
          <div className="flex items-center gap-2">
            {!sidebarOpen && (
              <button
                onClick={() => setSidebarOpen(true)}
                className="p-1 rounded hover:bg-surface text-muted hover:text-text transition-colors"
                title="Show conversations"
              >
                <PanelLeft size={14} />
              </button>
            )}
            <div>
              <h2 className="text-sm font-bold text-text m-0">
                {activeProject ? activeProject.name : "Intelliscan Chat"}
              </h2>
              <p className="text-[11px] text-muted m-0 mt-0.5">
                {activeProject
                  ? `${activeProject.nodes || 0} nodes · ${activeProject.edges || 0} edges`
                  : "No project selected"}
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            {pill && <StatusPill tone={pill.tone}>{pill.text}</StatusPill>}
            {graphCollapsed && onToggleGraphCollapse && (
              <button
                onClick={onToggleGraphCollapse}
                className="p-1 rounded hover:bg-surface text-muted hover:text-text transition-colors"
                title="Show graph"
              >
                <ChevronLeft size={14} />
              </button>
            )}
          </div>
        </div>

        {/* Messages */}
        <div className="flex-1 min-h-0 overflow-y-auto px-4 py-3 space-y-3">
          {messages.length === 0 ? (
            <div className="flex items-center justify-center h-full">
              {llmConfigured ? (
                <div className="text-center max-w-[280px]">
                  <span className="block mb-1.5 text-sm font-bold text-text">Ask anything</span>
                  <p className="text-xs text-muted leading-relaxed">
                    Your codebase is indexed. Ask about architecture, callers, impact, or test coverage.
                  </p>
                </div>
              ) : (
                <div className="text-center max-w-[300px]">
                  <div className="w-12 h-12 mx-auto mb-3 rounded-xl bg-accent/10 flex items-center justify-center">
                    <Zap size={24} className="text-accent-light" />
                  </div>
                  <span className="block mb-1 text-sm font-bold text-text">Connect your LLM</span>
                  <p className="text-xs text-muted leading-relaxed mb-4">
                    Enter your API URL and token to start chatting with your codebase.
                  </p>
                  <button
                    onClick={onGoToLLM}
                    className="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg text-xs font-bold text-white"
                    style={{ background: "linear-gradient(135deg, #8b5cf6, #d946ef)" }}
                  >
                    <Settings size={14} />
                    Configure LLM
                  </button>
                </div>
              )}
            </div>
          ) : (
            <>
              <AnimatePresence>
                {messages.map((msg) => (
                  <ChatMessage key={msg.id} message={msg} />
                ))}
              </AnimatePresence>

              {/* Streaming indicator */}
              {streamingContent && status === "answering" && (
                <div className="self-start max-w-[88%] glass-bubble rounded-[14px] rounded-bl-[4px] px-3.5 py-2.5">
                  <div className="flex items-center gap-2 mb-1.5">
                    <span className="text-[11px] font-bold text-text-secondary opacity-80">Intelliscan</span>
                    <StatusPill tone="success">Streaming</StatusPill>
                  </div>
                  <div className="message-body text-[13px] leading-relaxed opacity-70">
                    {streamingContent.slice(-300)}
                  </div>
                </div>
              )}

              <div ref={bottomRef} />
            </>
          )}
        </div>

        {/* Composer */}
        <PromptComposer
          disabled={status === "answering" || status === "classifying" || !llmConfigured}
          status={status}
          onSend={onSend}
        />
      </div>
    </div>
  );
}