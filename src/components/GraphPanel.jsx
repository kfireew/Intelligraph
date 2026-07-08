import { useState, useEffect, useRef } from "react";
import { motion } from "framer-motion";
import { X, GitBranch } from "lucide-react";

export function GraphPanel({ activePid, answerComplete, projectStatus, onHoverChange }) {
  const [expanded, setExpanded] = useState(false);
  const [pulse, setPulse] = useState(false);
  const prevRef = useRef(0);
  const graphUrl = activePid ? `/projects/${activePid}/graph-html` : null;

  // Pulse only when answerComplete increments (answer finished)
  useEffect(() => {
    if (answerComplete <= prevRef.current) return;
    prevRef.current = answerComplete;
    setPulse(true);
    const t = setTimeout(() => setPulse(false), 1100);
    return () => clearTimeout(t);
  }, [answerComplete]);

  return (
    <>
      {!expanded && (
        <motion.button initial={{ scale: 0, opacity: 0 }} animate={{ scale: 1, opacity: 1 }}
          className={`graph-orb${pulse ? " graph-orb-pulse" : ""}`}
          onClick={() => setExpanded(true)}
          onMouseEnter={() => onHoverChange?.(true)}
          onMouseLeave={() => onHoverChange?.(false)} title="Open graph">
          <div className="graph-orb-inner" />
        </motion.button>
      )}
      {expanded && (
        <div className="fixed inset-0 flex items-center justify-center"
          style={{ background: "rgba(0,0,0,0.6)", backdropFilter: "blur(6px)", zIndex: 9997 }}
          onClick={() => setExpanded(false)} />
      )}
      {/* Graph card — opens/closes centered, no movement */}
      <motion.div
        initial={false}
        animate={expanded ? { opacity: 1, scale: 1 } : { opacity: 0, scale: 0.4 }}
        transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1] }}
        className="graph-expanded"
        style={{ pointerEvents: expanded ? "auto" : "none" }}
      >
        <button onClick={() => setExpanded(false)} className="graph-close-btn" style={{ display: expanded ? "flex" : "none" }}><X size={18} /></button>
        <div className="graph-glow" />
        <div className="graph-iframe-container">
          {graphUrl ? (
            <iframe key={activePid + '-' + (projectStatus || 'unknown')} src={graphUrl} className="graph-iframe" title="Codebase Graph" sandbox="allow-scripts allow-same-origin" />
          ) : (
            <div className="graph-empty"><GitBranch size={24} className="text-accent-light" />
              <p className="text-sm text-muted mt-3">Select a project</p></div>
          )}
        </div>
      </motion.div>
    </>
  );
}