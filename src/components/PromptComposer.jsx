import { useState, useRef, useCallback } from "react";
import { motion } from "framer-motion";
import { Send, Boxes, ArrowUp, Zap, FlaskConical, Loader2 } from "lucide-react";
import { RouteBadge } from "./RouteBadge";

const quickActions = [
  { label: "Architecture", icon: Boxes, prompt: "Architecture overview" },
  { label: "Callers", icon: ArrowUp, prompt: "Who calls the auth module?" },
  { label: "Impact", icon: Zap, prompt: "Impact if I change the API layer?" },
  { label: "Tests", icon: FlaskConical, prompt: "Show test coverage" },
];

export function PromptComposer({ disabled, status, onSend }) {
  const [prompt, setPrompt] = useState("");
  const taRef = useRef(null);

  const isAnswering = status === "answering" || status === "classifying";
  const canSend = prompt.trim() && !disabled;

  const handleSend = useCallback(() => {
    if (!canSend) return;
    onSend(prompt.trim());
    setPrompt("");
    if (taRef.current) taRef.current.style.height = "44px";
  }, [canSend, prompt, onSend]);

  const handleKeyDown = useCallback((e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  }, [handleSend]);

  const handleInput = useCallback((e) => {
    e.target.style.height = "44px";
    e.target.style.height = Math.min(e.target.scrollHeight, 200) + "px";
  }, []);

  const route = prompt.trim() ? { category: "pending", label: "LLM route" } : null;

  return (
    <div className="glass rounded-t-xl px-3.5 py-3 border-t border-border">
      {/* Quick actions */}
      <div className="flex gap-1.5 mb-2 flex-wrap">
        {quickActions.map((qa) => (
          <button
            key={qa.label}
            disabled={disabled}
            onClick={() => onSend(qa.prompt)}
            className="flex items-center gap-1 px-2.5 py-1 rounded-full text-[11px] font-medium text-muted bg-white/4 hover:bg-white/8 disabled:opacity-40 transition-colors backdrop-blur-sm"
          >
            <qa.icon size={12} />
            {qa.label}
          </button>
        ))}
      </div>

      {/* Textarea */}
      <textarea
        ref={taRef}
        value={prompt}
        onChange={(e) => setPrompt(e.target.value)}
        onInput={handleInput}
        onKeyDown={handleKeyDown}
        disabled={disabled}
        rows={1}
        placeholder="Ask about this project..."
        className="w-full resize-none bg-transparent text-text text-sm leading-relaxed outline-none placeholder:text-muted-subtle disabled:opacity-40"
        style={{ minHeight: 44, maxHeight: 200 }}
      />

      {/* Footer */}
      <div className="flex items-center justify-between gap-2 mt-2 pr-3">
        <RouteBadge route={route} />
        <motion.button
          whileTap={{ scale: 0.92 }}
          transition={{ duration: 0.1 }}
          disabled={!canSend}
          onClick={handleSend}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-bold text-white disabled:opacity-40 transition-opacity"
          style={{ background: canSend ? "linear-gradient(135deg, #8b5cf6, #d946ef)" : "rgba(255,255,255,0.06)" }}
        >
          {isAnswering ? <Loader2 size={14} className="animate-spin" /> : <Send size={14} />}
          {isAnswering ? "Working" : "Send"}
        </motion.button>
      </div>
    </div>
  );
}