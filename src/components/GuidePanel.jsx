import { Download, Server } from "lucide-react";
import { endpoints } from "../config/endpoints";

export function GuidePanel({ activePid }) {
  return (
    <div className="flex flex-col flex-1 min-h-0 p-6 overflow-y-auto space-y-6">
      <div className="flex items-center gap-2"><Server size={18} className="text-accent-light" /><h2 className="text-lg font-bold gradient-text">MCP Setup</h2></div>
      <Section title="REST API Endpoints" icon={Server}>
        <p className="text-xs text-text-secondary m-0 mb-3 leading-relaxed">Use these endpoints for n8n, CI/CD, or external automation.</p>
        <div className="space-y-2">
          <div>
            <p className="text-[11px] font-bold text-muted uppercase tracking-wider mb-1">Clone Repository</p>
            <div className="relative group">
              <pre className="m-0 p-2.5 rounded-lg bg-black/30 text-[11px] font-mono text-text-secondary overflow-x-auto cursor-pointer select-all"
                onClick={(e) => { navigator.clipboard.writeText(e.target.textContent.trim()); }}>{`POST /projects/clone`}</pre>
              <span className="absolute top-1 right-1.5 text-[9px] text-muted-subtle opacity-0 group-hover:opacity-60 transition-opacity">click to copy</span>
            </div>
          </div>
          <div>
            <p className="text-[11px] font-bold text-muted uppercase tracking-wider mb-1">Completions (stateless)</p>
            <div className="relative group">
              <pre className="m-0 p-2.5 rounded-lg bg-black/30 text-[11px] font-mono text-text-secondary overflow-x-auto cursor-pointer select-all"
                onClick={(e) => { navigator.clipboard.writeText(e.target.textContent.trim()); }}>{`POST /api/v1/projects/{pid}/completions`}</pre>
              <span className="absolute top-1 right-1.5 text-[9px] text-muted-subtle opacity-0 group-hover:opacity-60 transition-opacity">click to copy</span>
            </div>
          </div>
        </div>
        <details className="mt-3">
          <summary className="text-[11px] font-bold text-muted cursor-pointer hover:text-text transition-colors">Example payloads</summary>
          <div className="mt-2 space-y-2">
            <div>
              <p className="text-[10px] text-muted-subtle mb-1">Clone a private Bitbucket repo:</p>
              <pre className="m-0 p-2 rounded-lg bg-black/30 text-[10px] font-mono text-text-secondary overflow-x-auto">{`{
  "git_url": "https://bitbucket.example.com/scm/PROJ/repo.git",
  "access_token": "BBDC-...",
  "auth_provider": "bitbucket_datacenter"
}`}</pre>
            </div>
            <div>
              <p className="text-[10px] text-muted-subtle mb-1">Ask a question about project 1:</p>
              <pre className="m-0 p-2 rounded-lg bg-black/30 text-[10px] font-mono text-text-secondary overflow-x-auto">{`{
  "prompt": "Explain the architecture",
  "include_context": true,
  "llm_url": "https://openrouter.ai/api/v1/chat/completions",
  "llm_token": "sk-..."
}`}</pre>
            </div>
          </div>
        </details>
      </Section>
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