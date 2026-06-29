import { Download, Server, Copy, Check, ExternalLink } from "lucide-react";
import { useState } from "react";
import { endpoints } from "../config/endpoints";

function copy(text) {
  navigator.clipboard.writeText(text);
}

export function GuidePanel({ activePid, activeProject }) {
  const [copied, setCopied] = useState(null);
  const isReady = activeProject && ["ready", "cloned", "indexed"].includes(activeProject.status);
  const pid = activeProject?.id;
  const completionsUrl = pid ? `/api/v1/projects/${pid}/completions` : "/api/v1/projects/{pid}/completions";
  const curlExample = pid
    ? `curl -X POST ${window.location.origin}${completionsUrl} \\\n  -H "Content-Type: application/json" \\\n  -H "Authorization: Bearer <your-api-token>" \\\n  -d '{\n  "prompt": "Explain the architecture",\n  "include_context": true,\n  "llm_url": "https://openrouter.ai/api/v1/chat/completions",\n  "llm_token": "sk-..."\n}'`
    : null;
  const n8nExample = pid ? `{
  "method": "POST",
  "url": "${window.location.origin}${completionsUrl}",
  "authentication": "genericCredentialType",
  "genericAuthType": "httpBearerAuth",
  "sendBody": true,
  "bodyParameters": {
    "parameters": [
      { "name": "prompt", "value": "Explain the architecture" },
      { "name": "include_context", "value": true },
      { "name": "llm_url", "value": "https://openrouter.ai/api/v1/chat/completions" },
      { "name": "llm_token", "value": "sk-..." }
    ]
  }
}` : null;

  const handleCopy = (key, text) => {
    copy(text);
    setCopied(key);
    setTimeout(() => setCopied(null), 2000);
  };

  return (
    <div className="flex flex-col flex-1 min-h-0 p-6 overflow-y-auto space-y-6">
      <div className="flex items-center gap-2"><Server size={18} className="text-accent-light" /><h2 className="text-lg font-bold gradient-text">MCP Setup</h2></div>
      <Section title="Integrations / API" icon={Server}>
        {!activeProject ? (
          <p className="text-xs text-muted-subtle m-0">Select a project to see its API endpoint.</p>
        ) : !isReady ? (
          <div>
            <p className="text-xs text-muted-subtle m-0 mb-2">Project status: <span className="text-yellow-400 font-medium">{activeProject.status}</span>. Wait for cloning to finish.</p>
            <div className="relative group opacity-50 pointer-events-none">
              <pre className="m-0 p-2.5 rounded-lg bg-black/30 text-[11px] font-mono text-text-secondary overflow-x-auto">{completionsUrl}</pre>
            </div>
          </div>
        ) : (
          <div className="space-y-3">
            <p className="text-xs text-text-secondary m-0 leading-relaxed">Use this endpoint for n8n, CI/CD, or external automation targeting <span className="text-text font-semibold">{activeProject.name}</span>.</p>

            {/* Endpoint URL */}
            <div>
              <p className="text-[11px] font-bold text-muted uppercase tracking-wider mb-1.5">Endpoint</p>
              <div className="relative group">
                <pre className="m-0 p-2.5 rounded-lg bg-black/30 text-[11px] font-mono text-text-secondary overflow-x-auto cursor-pointer select-all whitespace-pre-wrap break-all"
                  onClick={() => handleCopy("endpoint", `POST ${completionsUrl}`)}>{completionsUrl}</pre>
                <button onClick={() => handleCopy("endpoint", `POST ${completionsUrl}`)}
                  className="absolute top-1.5 right-1.5 p-1 rounded bg-black/40 hover:bg-black/60 text-muted-subtle hover:text-text transition-colors">
                  {copied === "endpoint" ? <Check size={12} className="text-green-400" /> : <Copy size={12} />}
                </button>
              </div>
            </div>

            <details className="mt-2">
              <summary className="text-[11px] font-bold text-muted cursor-pointer hover:text-text transition-colors">cURL &amp; n8n examples</summary>
              <div className="mt-2 space-y-3">
                {/* cURL */}
                <div>
                  <p className="text-[11px] font-bold text-muted uppercase tracking-wider mb-1.5">cURL</p>
                  <div className="relative group">
                    <pre className="m-0 p-2.5 rounded-lg bg-black/30 text-[11px] font-mono text-text-secondary overflow-x-auto text-[10px] leading-relaxed whitespace-pre-wrap break-all"
                      onClick={() => handleCopy("curl", curlExample)}>{curlExample}</pre>
                    <button onClick={() => handleCopy("curl", curlExample)}
                      className="absolute top-1.5 right-1.5 p-1 rounded bg-black/40 hover:bg-black/60 text-muted-subtle hover:text-text transition-colors">
                      {copied === "curl" ? <Check size={12} className="text-green-400" /> : <Copy size={12} />}
                    </button>
                  </div>
                </div>

                {/* n8n HTTP Request */}
                <div>
                  <p className="text-[11px] font-bold text-muted uppercase tracking-wider mb-1.5">n8n HTTP Request (JSON)</p>
                  <div className="relative group">
                    <pre className="m-0 p-2.5 rounded-lg bg-black/30 text-[11px] font-mono text-text-secondary overflow-x-auto text-[10px] leading-relaxed whitespace-pre-wrap break-all"
                      onClick={() => handleCopy("n8n", n8nExample)}>{n8nExample}</pre>
                    <button onClick={() => handleCopy("n8n", n8nExample)}
                      className="absolute top-1.5 right-1.5 p-1 rounded bg-black/40 hover:bg-black/60 text-muted-subtle hover:text-text transition-colors">
                      {copied === "n8n" ? <Check size={12} className="text-green-400" /> : <Copy size={12} />}
                    </button>
                  </div>
                </div>
              </div>
            </details>
          </div>
        )}
      </Section>
      <Section title="REST API Endpoints" icon={Server}>
        <p className="text-xs text-text-secondary m-0 mb-3 leading-relaxed">General endpoints for scripting and automation.</p>
        <div className="space-y-2">
          <div>
            <p className="text-[11px] font-bold text-muted uppercase tracking-wider mb-1">Clone Repository</p>
            <div className="relative group">
              <pre className="m-0 p-2.5 rounded-lg bg-black/30 text-[11px] font-mono text-text-secondary overflow-x-auto cursor-pointer select-all"
                onClick={() => handleCopy("clone", "POST /projects/clone")}>{`POST /projects/clone`}</pre>
              <button onClick={() => handleCopy("clone", "POST /projects/clone")}
                className="absolute top-1.5 right-1.5 p-1 rounded bg-black/40 hover:bg-black/60 text-muted-subtle hover:text-text transition-colors">
                {copied === "clone" ? <Check size={12} className="text-green-400" /> : <Copy size={12} />}
              </button>
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