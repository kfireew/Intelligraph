import { Download, Server } from "lucide-react";
import { endpoints } from "../config/endpoints";

export function GuidePanel({ activePid }) {
  return (
    <div className="flex flex-col flex-1 min-h-0 p-6 overflow-y-auto space-y-6">
      <div className="flex items-center gap-2"><Server size={18} className="text-accent-light" /><h2 className="text-lg font-bold gradient-text">MCP Setup</h2></div>
      <Section title="Local MCP Server" icon={Download}>
        <p className="text-xs text-muted m-0 mb-3">Download and run alongside your project for Claude Code integration.</p>
        <ol className="text-xs text-text-secondary space-y-2 m-0 pl-4">
          <li>Download <code className="px-1.5 py-0.5 rounded bg-accent/10 text-accent-light text-[11px] font-mono">mcp_server_standalone.py</code></li>
          <li>Place in your project folder alongside <code className="px-1.5 py-0.5 rounded bg-accent/10 text-accent-light text-[11px] font-mono">.code-review-graph/</code> and <code className="px-1.5 py-0.5 rounded bg-accent/10 text-accent-light text-[11px] font-mono">graphify-out/</code></li>
          <li>Add to <code className="px-1.5 py-0.5 rounded bg-accent/10 text-accent-light text-[11px] font-mono">.mcp.json</code>:<pre className="mt-2 p-3 rounded bg-black/30 text-[11px] font-mono text-text-secondary overflow-x-auto">{`{"mcpServers":{"intelligraph":{"command":"python","args":["mcp_server_standalone.py","--crg-db",".code-review-graph/graph.db","--graphify","graphify-out/graph.json"],"cwd":"/path/to/your/project"}}}`}</pre></li>
        </ol>
        <a href={endpoints.downloadMCPServer} download
          className="inline-flex items-center gap-1.5 mt-3 px-3 py-1.5 rounded-lg bg-accent/10 hover:bg-accent/20 text-accent-light text-xs font-medium transition-colors no-underline">
          <Download size={14} /> Download MCP Server
        </a>
      </Section>
      <Section title="How it works" icon={Server}>
        <p className="text-xs text-text-secondary m-0 leading-relaxed">Uses the same <code className="px-1 py-0.5 rounded bg-accent/10 text-accent-light text-[11px] font-mono">retrieval.py</code> runtime as the web UI. Pipeline: ExecutionPlanner → NodeResolver → TraversalPlanner → NeighborhoodRanker → ChunkRetriever → ContextMerger.</p>
      </Section>
    </div>
  );
}
function Section({ title, icon: Icon, children }) {
  return <div className="glass rounded-xl p-4"><div className="flex items-center gap-2 mb-3">{Icon && <Icon size={16} className="text-accent-light shrink-0" />}<h3 className="text-sm font-bold text-text m-0">{title}</h3></div>{children}</div>;
}