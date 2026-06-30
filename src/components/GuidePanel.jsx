import { Download, Server, Copy, Check } from "lucide-react";
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

  const llmUrl = (typeof localStorage !== "undefined" && localStorage.getItem("llm-url")) || "https://models.ai-services.idf.cts/v1/chat/completions";

  const curlExample = pid
    ? `curl -X POST ${window.location.origin}${completionsUrl} \\\n  -H "Content-Type: application/json" \\\n  -H "Authorization: Bearer <your-api-token>" \\\n  -d '{\n  "prompt": "Explain the architecture",\n  "include_context": true,\n  "llm_url": "${llmUrl}",\n  "llm_token": "sk-..."\n}'`
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
      { "name": "llm_url", "value": "${llmUrl}" },
      { "name": "llm_token", "value": "sk-..." }
    ]
  }
}` : null;

  const mcpArgs = `mcp_server_standalone.py --intelligraph-url http://localhost:5050 --project-id ${pid || "{pid}"}`;
  const claudeMcp = `{${JSON.stringify({
  mcpServers: {
    intelligraph: {
      command: "python",
      args: ["mcp_server_standalone.py", "--intelligraph-url", "http://localhost:5050", "--project-id", String(pid || "{pid}")],
      cwd: "/path/to/your/project"
    }
  }
}, null, 2)}}`;
  const opencodeMcp = JSON.stringify({
    $schema: "https://opencode.ai/config.json",
    mcp: {
      intelligraph: {
        type: "local",
        command: ["python", "mcp_server_standalone.py", "--intelligraph-url", "http://localhost:5050", "--project-id", String(pid || "{pid}")],
        cwd: "/path/to/your/project"
      }
    }
  }, null, 2);

  const handleCopy = (key, text) => {
    copy(text);
    setCopied(key);
    setTimeout(() => setCopied(null), 2000);
  };

  return (
    <div className="flex flex-col flex-1 min-h-0 p-6 overflow-y-auto space-y-6">
      <div className="flex items-center gap-2"><Server size={18} className="text-accent-light" /><h2 className="text-lg font-bold gradient-text">Guide</h2></div>

      {/* ── API Endpoints (consolidated) ── */}
      <Section title="API Endpoints" icon={Server}>
        {/* Project Completions */}
        <div className="space-y-3">
          <div>
            <p className="text-[11px] font-bold text-muted uppercase tracking-wider mb-1.5">Project Completions</p>
            <p className="text-xs text-text-secondary m-0 leading-relaxed mb-2">
              {activeProject ? (
                <>Use this endpoint for n8n, CI/CD, or external automation targeting <span className="text-text font-semibold">{activeProject.name}</span>.</>
              ) : (
                <>Select a project to see its API endpoint.</>
              )}
            </p>
          </div>

          {!activeProject ? null : !isReady ? (
            <div>
              <p className="text-xs text-muted-subtle m-0 mb-2">Project status: <span className="text-yellow-400 font-medium">{activeProject.status}</span>. Wait for cloning to finish.</p>
              <pre className="m-0 p-2.5 rounded-lg bg-black/30 text-[11px] font-mono text-text-secondary overflow-x-auto opacity-50">{completionsUrl}</pre>
            </div>
          ) : (
            <>
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

                  {/* n8n */}
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
            </>
          )}
        </div>

        {/* Clone Repository */}
        <div className="mt-4 pt-4 border-t border-white/5">
          <p className="text-[11px] font-bold text-muted uppercase tracking-wider mb-1.5">Clone Repository</p>
          <p className="text-xs text-text-secondary m-0 mb-2 leading-relaxed">General endpoint for scripting and automation.</p>
          <div className="space-y-2">
            <div className="relative group">
              <pre className="m-0 p-2.5 rounded-lg bg-black/30 text-[11px] font-mono text-text-secondary overflow-x-auto cursor-pointer select-all"
                onClick={() => handleCopy("clone", "POST /projects/clone")}>{`POST /projects/clone`}</pre>
              <button onClick={() => handleCopy("clone", "POST /projects/clone")}
                className="absolute top-1.5 right-1.5 p-1 rounded bg-black/40 hover:bg-black/60 text-muted-subtle hover:text-text transition-colors">
                {copied === "clone" ? <Check size={12} className="text-green-400" /> : <Copy size={12} />}
              </button>
            </div>
          </div>
          <details className="mt-2">
            <summary className="text-[11px] font-bold text-muted cursor-pointer hover:text-text transition-colors">Example payload</summary>
            <div className="mt-2">
              <p className="text-[10px] text-muted-subtle mb-1">Clone a private Bitbucket repo:</p>
              <pre className="m-0 p-2 rounded-lg bg-black/30 text-[10px] font-mono text-text-secondary overflow-x-auto">{`{
  "git_url": "https://bitbucket.example.com/scm/PROJ/repo.git",
  "access_token": "BBDC-...",
  "auth_provider": "bitbucket_datacenter"
}`}</pre>
            </div>
          </details>
        </div>
      </Section>

      {/* ── MCP Server ── */}
      <Section title="MCP Server" icon={Download}>
        <p className="text-xs text-text-secondary m-0 mb-3 leading-relaxed">
          Download the MCP server script and configure your AI coding tool to use Intelligraph's code-graph retrieval.
        </p>

        {/* Prerequisites */}
        <div className="mb-3 p-2.5 rounded-lg bg-accent/5 border border-accent/10">
          <p className="text-[11px] font-bold text-accent-light uppercase tracking-wider mb-1">Prerequisites</p>
          <ol className="text-xs text-text-secondary space-y-1 m-0 pl-4 list-decimal">
            <li>Install dependencies: <code className="px-1 py-0.5 rounded bg-accent/10 text-accent-light text-[11px] font-mono">pip install mcp requests</code></li>
            <li>Intelligraph container must be running and accessible</li>
            <li>At least one project must be cloned (note its project ID)</li>
          </ol>
        </div>

        {/* Claude Code */}
        <div className="mb-4">
          <p className="text-[11px] font-bold text-muted uppercase tracking-wider mb-1.5">Claude Code</p>
          <p className="text-xs text-text-secondary m-0 mb-2 leading-relaxed">
            For <b>project-specific</b> config, place <code className="px-1 py-0.5 rounded bg-accent/10 text-accent-light text-[11px] font-mono">.mcp.json</code> in your project root.
            For <b>global</b> config, edit <code className="px-1 py-0.5 rounded bg-accent/10 text-accent-light text-[11px] font-mono">~/.claude.json</code> instead.
          </p>
          <p className="text-[10px] text-muted-subtle mb-2">
            Tip: First <code className="text-accent-light">cd</code> into your project folder, then create <code className="text-accent-light">.mcp.json</code> there — Claude Code reads it from the current working directory.
          </p>
          <pre className="m-0 p-3 rounded-lg bg-black/30 text-[11px] font-mono text-text-secondary overflow-x-auto">{claudeMcp}</pre>
        </div>

        {/* opencode */}
        <div className="mb-4">
          <p className="text-[11px] font-bold text-muted uppercase tracking-wider mb-1.5">opencode</p>
          <p className="text-xs text-text-secondary m-0 mb-2 leading-relaxed">
            Place <code className="px-1 py-0.5 rounded bg-accent/10 text-accent-light text-[11px] font-mono">opencode.json</code> in your project root (or <code className="px-1 py-0.5 rounded bg-accent/10 text-accent-light text-[11px] font-mono">~/.config/opencode/opencode.json</code> for global).
          </p>
          <pre className="m-0 p-3 rounded-lg bg-black/30 text-[11px] font-mono text-text-secondary overflow-x-auto">{opencodeMcp}</pre>
        </div>

        {/* Concrete path example */}
        <div className="mb-3">
          <p className="text-[11px] font-bold text-muted uppercase tracking-wider mb-1.5">Path Example</p>
          <pre className="m-0 p-2.5 rounded-lg bg-black/30 text-[10px] font-mono text-text-secondary overflow-x-auto">{`# Example: cwd path
"cwd": "C:/Users/kfir/my-project"
# On Linux/macOS:
"cwd": "/home/user/my-project"`}</pre>
        </div>

        <a href={endpoints.downloadMCPServer} download
          className="inline-flex items-center gap-1.5 mt-1 px-3 py-1.5 rounded-lg bg-accent/10 hover:bg-accent/20 text-accent-light text-xs font-medium transition-colors no-underline">
          <Download size={14} /> Download MCP Server
        </a>
      </Section>

      {/* ── How it works ── */}
      <Section title="How it works" icon={Server}>
        <p className="text-xs text-text-secondary m-0 leading-relaxed">Uses the same <code className="px-1 py-0.5 rounded bg-accent/10 text-accent-light text-[11px] font-mono">retrieval.py</code> runtime as the web UI. Pipeline: ExecutionPlanner → NodeResolver → TraversalPlanner → NeighborhoodRanker → ChunkRetriever → ContextMerger.</p>
      </Section>
    </div>
  );
}
function Section({ title, icon: Icon, children }) {
  return <div className="glass rounded-xl p-4"><div className="flex items-center gap-2 mb-3">{Icon && <Icon size={16} className="text-accent-light shrink-0" />}<h3 className="text-sm font-bold text-text m-0">{title}</h3></div>{children}</div>;
}
