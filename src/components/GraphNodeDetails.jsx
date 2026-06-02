import { X, FileCode, Layers, Hash } from "lucide-react";

const KIND_COLORS = {
  Function: "text-cyan",
  Class: "text-orange",
  Method: "text-green",
  Variable: "text-magenta-light",
  Module: "text-accent-light",
};

export function GraphNodeDetails({ node, onClear }) {
  if (!node) {
    return (
      <div className="glass rounded-xl p-4 animate-fade-in">
        <div className="flex items-center gap-2 mb-2">
          <FileCode size={13} className="text-muted-subtle" />
          <span className="text-xs text-muted font-semibold">Node Details</span>
        </div>
        <p className="text-xs text-muted-subtle leading-relaxed">
          Click a node in the graph to view details.
        </p>
      </div>
    );
  }

  const details = node.details || [];
  const byKind = {};
  details.forEach((d) => {
    const k = d.kind || "Other";
    if (!byKind[k]) byKind[k] = [];
    byKind[k].push(d);
  });

  return (
    <div className="glass rounded-xl p-4 space-y-2 animate-fade-in max-h-64 overflow-y-auto">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-1.5 min-w-0">
          <FileCode size={13} className="text-accent-light flex-shrink-0" />
          <span className="text-xs font-bold text-text truncate">{node.label || node.id}</span>
        </div>
        <button
          onClick={onClear}
          className="p-0.5 rounded text-muted-subtle hover:text-text hover:bg-surface-hover transition-colors flex-shrink-0"
        >
          <X size={12} />
        </button>
      </div>

      {/* Group + path */}
      <div className="flex items-center gap-2 text-[10px] text-muted-subtle">
        <span>{node.group || "root"}</span>
        <span>{node.id}</span>
      </div>

      {/* CRG details */}
      {details.length > 0 ? (
        <div className="space-y-2 pt-1 border-t border-glass-border">
          {Object.entries(byKind).map(([kind, items]) => (
            <div key={kind}>
              <div className="flex items-center gap-1.5 mb-1">
                <Layers size={10} className="text-muted-subtle" />
                <span className={`text-[10px] font-bold ${KIND_COLORS[kind] || "text-muted"}`}>{kind}s</span>
                <span className="text-[10px] text-muted-subtle">({items.length})</span>
              </div>
              {items.map((d, i) => (
                <div key={i} className="flex items-center gap-1 pl-3 text-[10px] leading-relaxed group">
                  <Hash size={8} className="text-muted-subtle flex-shrink-0" />
                  <span className="text-text font-medium">{d.name || d.qualified_name?.split(".").pop()}</span>
                  {d.line_start && (
                    <span className="text-muted-subtle">:{d.line_start}</span>
                  )}
                  {d.signature && (
                    <span className="text-muted-subtle truncate hidden group-hover:inline ml-1">({d.signature})</span>
                  )}
                </div>
              ))}
            </div>
          ))}
        </div>
      ) : (
        <p className="text-[10px] text-muted-subtle pt-1">
          Detailed node information not available.
        </p>
      )}
    </div>
  );
}