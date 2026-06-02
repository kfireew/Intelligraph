import { useRef, useEffect } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { ChatMessage } from "./ChatMessage";
import { PromptComposer } from "./PromptComposer";
import { StatusPill } from "./StatusPill";
import { Settings, Zap } from "lucide-react";

const STATUS_PILL = {
  idle: null,
  classifying: { text: "Classifying", tone: "info" },
  thinking: { text: "Thinking", tone: "info" },
  answering: { text: "Streaming", tone: "success" },
  error: { text: "Error", tone: "error" },
};

export function ChatPanel({ messages, status, streamingContent, onSend, activeProject, graphData, llmConfigured, onGoToLLM }) {
  const bottomRef = useRef(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, streamingContent]);

  const pill = STATUS_PILL[status];

  return (
    <div className="flex flex-col min-w-0 flex-1 min-h-0">
      {/* Header */}
      <div className="glass flex items-center justify-between px-4 py-3 border-b border-border min-h-[52px]">
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
        {pill && <StatusPill tone={pill.tone}>{pill.text}</StatusPill>}
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
  );
}