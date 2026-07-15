import { memo, useState } from "react";
import ReactMarkdown from "react-markdown";
import { RouteBadge } from "./RouteBadge";
import { ChevronDown, ChevronRight, FileCode, ThumbsUp, ThumbsDown } from "lucide-react";
export const ChatMessage = memo(function ChatMessage({ message, sendFeedback }) {
  const { role, content, metadata } = message;
  const isBot = role === "assistant";
  const [sourcesExpanded, setSourcesExpanded] = useState(false);
  const [fullContextExpanded, setFullContextExpanded] = useState(false);
  const [feedbackGiven, setFeedbackGiven] = useState(null);
  // Em dash mojibake fix — replace smart quotes/dashes with ASCII equivalents
  const cleanContent = content
    ? content
        .replace(/\u00e2\u0080\u0094/g, "--")
        .replace(/\u00e2\u0080\u0099/g, "'")
        .replace(/\u00e2\u0080\u009c/g, "\"")
        .replace(/\u00e2\u0080\u009d/g, "\"")
    : content;
  const time = new Date(message.createdAt).toLocaleTimeString();
  const sources = metadata?.sources;

  return (
    <div
      className={`chat-message w-fit max-w-[88%] min-w-0 px-3.5 py-2.5 rounded-[14px] text-sm ${
        isBot
          ? "bg-[rgba(10,10,10,0.85)] border border-glass-border rounded-bl-[4px]"
          : "ml-auto rounded-br-[4px]"
      }`}
      style={
        !isBot
          ? { background: "linear-gradient(135deg, #8b5cf6, #d946ef)" }
          : undefined
      }
    >
      {/* Header */}
      <div className="flex items-center justify-between gap-2 mb-1.5">
        <div className="flex items-center gap-2">
          <span className="text-[11px] font-bold text-text-secondary opacity-80">
            {isBot ? "Intelligraph" : "You"}
          </span>
          {metadata?.route && <RouteBadge route={metadata.route} />}
          {metadata?.savings?.reduction_factor > 0 && (
            <span className="text-[10px] text-emerald-400/80 font-mono">
              {metadata.savings.reduction_factor}x savings
            </span>
          )}
        </div>
        <time className="text-[10px] text-muted">{time}</time>
      </div>

      {/* LLM answer */}
      {content && (
        <div className="message-body text-[13px] leading-relaxed">
          <ReactMarkdown>{cleanContent}</ReactMarkdown>
        </div>
      )}

      {/* Bento grid for graph results */}
      {metadata?.result && !content && <ResultGrid result={metadata.result} />}

      {/* Sources panel — expandable, shows retrieved code context */}
      {isBot && sources && (
        <div className="mt-2 border-t border-white/5 pt-2">
          <button
            onClick={() => setSourcesExpanded(!sourcesExpanded)}
            className="flex items-center gap-1.5 text-[11px] text-muted hover:text-text transition-colors"
          >
            {sourcesExpanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
            <FileCode size={12} />
            <span>View sources ({sources.files?.length || 0} files{sources.context_stats?.raw_chunks ? `, ${sources.context_stats.raw_chunks} chunks` : ""})</span>
          </button>
          {sourcesExpanded && (
            <div className="mt-2 space-y-2">
              {/* File list + stats */}
              <div className="rounded-lg bg-black/20 p-2 space-y-1.5">
                {sources.files?.length > 0 && (
                  <div>
                    <div className="text-[10px] font-bold text-muted uppercase tracking-wider mb-1">Files used</div>
                    <div className="flex flex-wrap gap-1">
                      {sources.files.slice(0, 20).map((f, i) => (
                        <code key={i} className="px-1.5 py-0.5 rounded bg-accent/10 text-accent-light text-[10px]">{f}</code>
                      ))}
                    </div>
                  </div>
                )}
                {sources.context_stats && (
                  <div className="text-[10px] text-muted-subtle">
                    Strategy: {sources.context_stats.strategy || "?"} | Chunks: {sources.context_stats.raw_chunks || 0} | Chars: {(sources.context_stats.final_chars || 0).toLocaleString()} | Budget: {(sources.context_stats.budget_chars || 0).toLocaleString()}
                    {sources.context_stats.degraded && <span className="text-orange ml-1">| DEGRADED</span>}
                  </div>
                )}
              </div>
              {/* Full context (expandable) */}
              <button
                onClick={() => setFullContextExpanded(!fullContextExpanded)}
                className="flex items-center gap-1.5 text-[10px] text-muted hover:text-text transition-colors"
              >
                {fullContextExpanded ? <ChevronDown size={10} /> : <ChevronRight size={10} />}
                <span>{fullContextExpanded ? "Hide" : "Show"} full context</span>
              </button>
              {fullContextExpanded && sources.context && (
                <div className="rounded-lg bg-black/30 p-2 max-h-[400px] overflow-y-auto">
                  <div className="message-body text-[11px] leading-relaxed">
                    <ReactMarkdown>{sources.context}</ReactMarkdown>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* Path warnings */}
      {metadata?.pathWarnings?.length > 0 && (
        <div className="mt-2 p-2 rounded-md text-[11px] text-orange border border-orange/20 bg-orange/5">
          Unverified references:{" "}
          {metadata.pathWarnings.map((w, i) => (
            <code key={i} className="px-1 py-0.5 rounded text-accent-light bg-accent/10 text-[10px] mx-0.5">
              {w}
            </code>
          ))}
        </div>
      )}

      {/* Feedback buttons */}
      {isBot && content && !content.startsWith("(LLM") && sendFeedback && (
        <div className="mt-2 flex items-center gap-1.5">
          <button
            onClick={() => {
              if (feedbackGiven !== "up") { setFeedbackGiven("up"); sendFeedback(1); }
            }}
            className={`p-1 rounded transition-colors ${feedbackGiven === "up" ? "text-emerald-400 bg-emerald-400/10" : "text-muted hover:text-text"}`}
          >
            <ThumbsUp size={12} />
          </button>
          <button
            onClick={() => {
              if (feedbackGiven !== "down") { setFeedbackGiven("down"); sendFeedback(-1); }
            }}
            className={`p-1 rounded transition-colors ${feedbackGiven === "down" ? "text-red-400 bg-red-400/10" : "text-muted hover:text-text"}`}
          >
            <ThumbsDown size={12} />
          </button>
        </div>
      )}
    </div>
  );
});

function ResultGrid({ result }) {
  const sections = [
    ["Matches", result.matches || result.matched || result.node],
    ["Callers", result.callers],
    ["Callees", result.callees],
    ["Dependents", result.dependents],
    ["Coverage", result.coverage],
    ["Flows", result.flows],
    ["Communities", result.communities],
  ].filter(([, items]) => items?.length);

  if (!sections.length) return null;

  return (
    <div className="mt-2 space-y-2">
      {sections.map(([title, items]) => (
        <div key={title}>
          <div className="text-[10px] font-bold text-muted uppercase tracking-wider mb-1">{title}</div>
          <div className="grid gap-1.5" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))" }}>
            {(items || []).slice(0, 24).map((item, i) => (
              <div
                key={i}
                className="glass-bubble rounded-lg p-2.5 text-xs hover:bg-surface-hover transition-colors cursor-pointer"
              >
                <div className="font-semibold text-text truncate">{item.name || item.source_qualified || item.target_qualified || "?"}</div>
                <div className="text-muted mt-0.5">
                  {item.kind && <span className="mr-1.5">{item.kind}</span>}
                  {item.file_path && <span className="mr-1.5">{item.file_path}{item.line ? `:${item.line}` : ""}</span>}
                  {item.criticality && <span className="text-orange">risk: {item.criticality}</span>}
                </div>
                {item.signature && <div className="text-muted-subtle mt-0.5 font-mono text-[10px] truncate">{item.signature}</div>}
                {item.purpose && <div className="text-muted text-[10px] mt-0.5">{item.purpose}</div>}
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}