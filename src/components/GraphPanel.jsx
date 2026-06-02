import { useMemo, useCallback, useState, useRef, useEffect } from "react";
import { GitBranch, Boxes, Maximize2, Minimize2 } from "lucide-react";
import { findFileDetails } from "../utils/graphQueries";
import { GraphNodeDetails } from "./GraphNodeDetails";

/**
 * GraphPanel — wraps graphify's graph.html in an iframe.
 * Theme-injected by backend at /projects/<pid>/graph-html.
 * Node click events bridged via postMessage → queries CRG for details.
 */
export function GraphPanel({ activePid, crgDb, selectedNode, onSelectNode }) {
  const [expanded, setExpanded] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const iframeRef = useRef(null);

  const graphUrl = activePid ? `/projects/${activePid}/graph-html` : null;

  // Listen for postMessage from graph.html (node clicks)
  useEffect(() => {
    const handler = (e) => {
      const msg = e.data;
      if (msg?.type === "node-click" && crgDb && msg.nodeId) {
        const details = findFileDetails(crgDb, String(msg.nodeId));
        onSelectNode?.({
          id: msg.nodeId,
          label: msg.label || String(msg.nodeId),
          source_file: msg.source_file || "",
          details,
        });
      }
    };
    window.addEventListener("message", handler);
    return () => window.removeEventListener("message", handler);
  }, [crgDb, onSelectNode]);

  const containerClass = expanded
    ? "fixed inset-0 z-40 bg-bg/90 backdrop-blur-sm p-6"
    : "flex-1 min-w-0 min-h-0";

  // Reset loaded when URL changes
  useEffect(() => { setLoaded(false); }, [graphUrl]);

  return (
    <div className={containerClass}>
      <div className="glass rounded-xl flex flex-col h-full overflow-hidden">
        <div className="flex items-center justify-between px-4 py-2.5 border-b border-glass-border min-h-[44px]">
          <div className="flex items-center gap-2">
            <GitBranch size={14} className="text-accent-light" />
            <h3 className="text-xs font-bold text-text m-0">Graph</h3>
          </div>
          <button
            onClick={() => setExpanded(!expanded)}
            className="p-1 rounded text-muted hover:text-text hover:bg-white/5 transition-colors"
          >
            {expanded ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
          </button>
        </div>

        {!graphUrl ? (
          <div className="flex-1 flex items-center justify-center">
            <div className="text-center max-w-[240px]">
              <div className="w-10 h-10 mx-auto mb-2.5 rounded-xl bg-accent/10 flex items-center justify-center">
                <Boxes size={20} className="text-accent-light" />
              </div>
              <span className="block mb-1 text-sm font-bold text-text">No project selected</span>
              <p className="text-xs text-muted leading-relaxed">
                Clone or select a project to visualize its graph.
              </p>
            </div>
          </div>
        ) : (
          <>
            <div className="flex-1 min-h-0 relative bg-[#0d1117]">
              {!loaded && (
                <div className="absolute inset-0 flex items-center justify-center z-10 bg-[#0d1117]">
                  <div className="text-center">
                    <div className="w-6 h-6 mx-auto mb-2 border-2 border-accent/30 border-t-accent rounded-full animate-spin" />
                    <span className="text-xs text-muted">Loading graph...</span>
                  </div>
                </div>
              )}
              <iframe
                ref={iframeRef}
                src={graphUrl}
                onLoad={() => setLoaded(true)}
                className="w-full h-full border-0"
                title="Codebase Graph"
                sandbox="allow-scripts allow-same-origin"
              />
            </div>
            <div className="px-3 pb-3">
              <GraphNodeDetails node={selectedNode} onClear={() => onSelectNode?.(null)} />
            </div>
          </>
        )}
      </div>
    </div>
  );
}