import { Download, Server, Copy, Check, FileCode } from "lucide-react";
import { useState } from "react";
import { endpoints } from "../config/endpoints";

function copy(text) {
  navigator.clipboard.writeText(text);
}

export function GuidePanel({ activePid, activeProject }) {
  const [copied, setCopied] = useState(null);
  const isReady = activeProject && ["ready", "cloned", "indexed"].includes(activeProject.status);
  const pid = activeProject?.id;

  // Auto-fill from what the user already has
  const containerUrl = typeof window !== "undefined" ? window.location.origin : "http://localhost:5050";
  const llmUrl = ((typeof localStorage !== "undefined" && localStorage.getItem("llm-url")) || "https://models.ai-services.idf.cts/v1/chat/completions").trim().replace(/\/+$/, "");
  const completionsUrl = pid ? `/api/v1/projects/${pid}/completions` : null;
  const fullCompletionsUrl = pid ? `${containerUrl}${completionsUrl}` : null;

  const curlExample = fullCompletionsUrl
    ? `curl -X POST ${fullCompletionsUrl} \\
  -H "Content-Type: application/json" \\
  -d '{
    "prompt": "Explain the architecture",
    "include_context": true,
    "llm_url": "${llmUrl}",
    "llm_token": "YOUR-TOKEN-HERE"
  }'`
    : null;

  // MCP config — auto-filled with real values
  const mcpCommand = pid
    ? `python mcp_server_standalone.py --intelligraph-url ${containerUrl} --project-id ${pid}`
    : null;

  const claudeMcp = pid
    ? JSON.stringify({
        mcpServers: {
          intelligraph: {
            command: "python",
            args: ["mcp_server_standalone.py", "--intelligraph-url", containerUrl, "--project-id", String(pid)],
          },
        },
      }, null, 2)
    : null;

  const opencodeMcp = pid
    ? JSON.stringify({
        $schema: "https://opencode.ai/config.json",
        mcp: {
          intelligraph: {
            type: "local",
            command: ["python", "mcp_server_standalone.py", "--intelligraph-url", containerUrl, "--project-id", String(pid)],
          },
        },
      }, null, 2)
    : null;

  const handleCopy = (key, text) => {
    copy(text);
    setCopied(key);
    setTimeout(() => setCopied(null), 2000);
  };

  const CodeBlock = ({ id, label, code }) => (
    <div>
      {label && <p className="text-[11px] font-bold text-muted uppercase tracking-wider mb-1.5">{label}</p>}
      <div className="relative group">
        <pre className="m-0 p-2.5 rounded-lg bg-black/30 text-[11px] font-mono text-text-secondary overflow-x-auto whitespace-pre-wrap break-all cursor-pointer"
          onClick={() => handleCopy(id, code)}>{code}</pre>
        <button onClick={() => handleCopy(id, code)}
          className="absolute top-1.5 right-1.5 p-1 rounded bg-black/40 hover:bg-black/60 text-muted-subtle hover:text-text transition-colors">
          {copied === id ? <Check size={12} className="text-green-400" /> : <Copy size={12} />}
        </button>
      </div>
    </div>
  );

  return (
    <div className="flex flex-col flex-1 min-h-0 p-6 overflow-y-auto space-y-6">
      <div className="flex items-center gap-2"><Server size={18} className="text-accent-light" /><h2 className="text-lg font-bold gradient-text">Guide</h2></div>

      {/* ── No project selected banner ── */}
      {!activeProject && (
        <div className="glass rounded-xl p-4 border border-yellow-500/20 bg-yellow-500/5">
          <p className="text-xs text-text-secondary m-0">Select a project on the left to see its API endpoints and MCP setup. Everything below auto-fills based on the active project.</p>
        </div>
      )}

      {/* ── API Endpoints ── */}
      <Section title="API Endpoints" icon={Server}>
        {activeProject && (
          <div className="mb-3 p-2 rounded-lg bg-accent/5 border border-accent/10">
            <p className="text-xs text-text-secondary m-0">
              Project: <span className="text-text font-semibold">{activeProject.name}</span> &nbsp;|&nbsp; ID: <span className="text-accent-light font-mono">{pid}</span> &nbsp;|&nbsp; Status: <span className={isReady ? "text-green" : "text-yellow-400"}>{activeProject.status}</span> &nbsp;|&nbsp; Nodes: <span className="text-accent-light font-mono">{activeProject.nodes || 0}</span> &nbsp;|&nbsp; Edges: <span className="text-accent-light font-mono">{activeProject.edges || 0}</span>
            </p>
          </div>
        )}

        {/* Project Completions */}
        <div className="space-y-3">
          {!activeProject ? (
            <p className="text-xs text-muted-subtle m-0">Select a project on the left. The endpoint, cURL, and n8n config below will auto-fill with that project's ID and your container URL.</p>
          ) : !isReady ? (
            <p className="text-xs text-muted-subtle m-0">Project is still <span className="text-yellow-400">{activeProject.status}</span>. Wait for it to finish before using the API.</p>
          ) : (
            <>
              <div>
                <p className="text-xs text-text-secondary m-0 mb-1 leading-relaxed">
                  Send a POST with a <code className="px-1 py-0.5 rounded bg-accent/10 text-accent-light text-[11px] font-mono">prompt</code> and your LLM credentials. Intelligraph retrieves relevant code context from the graph and sends it to the LLM for you.
                </p>
              </div>

              <CodeBlock id="endpoint" label={`Endpoint for "${activeProject.name}"`} code={`POST ${fullCompletionsUrl}`} />

              <div className="mt-3">
                <p className="text-[11px] font-bold text-muted uppercase tracking-wider mb-1.5">cURL — copy &amp; paste</p>
                <CodeBlock id="curl" code={curlExample} />
              </div>

              <div className="mt-3">
                <p className="text-[11px] font-bold text-muted uppercase tracking-wider mb-1.5">n8n HTTP Request node</p>
                <p className="text-[10px] text-muted-subtle m-0 mb-1.5">Set Method = POST, URL = the endpoint above, Auth = Bearer. Body parameters:</p>
                <CodeBlock id="n8n" code={JSON.stringify({
                  method: "POST",
                  url: fullCompletionsUrl,
                  authentication: "genericCredentialType",
                  genericAuthType: "httpBearerAuth",
                  sendBody: true,
                  bodyParameters: {
                    parameters: [
                      { name: "prompt", value: "Explain the architecture" },
                      { name: "include_context", value: true },
                      { name: "llm_url", value: llmUrl },
                      { name: "llm_token", value: "YOUR-TOKEN-HERE" },
                    ],
                  },
                }, null, 2)} />
              </div>

              <details className="mt-3">
                <summary className="text-[11px] font-bold text-muted cursor-pointer hover:text-text transition-colors">What the response looks like</summary>
                <div className="mt-2"><CodeBlock id="respExample" code={JSON.stringify({
                  answer: "The authentication module lives in src/auth/...",
                  model: "qwen/Qwen2.5-Coder-7B-Instruct",
                  context_used: true,
                  context_stats: { chunks: 12, tokens: 3400 },
                  path_warnings: [],
                }, null, 2)} /></div>
              </details>
            </>
          )}
        </div>

        {/* Clone Repository */}
        <div className="mt-4 pt-4 border-t border-white/5">
          <p className="text-[11px] font-bold text-muted uppercase tracking-wider mb-1.5">Clone Repository</p>
          <p className="text-xs text-text-secondary m-0 mb-2 leading-relaxed">Clone a new repo into Intelligraph via API.</p>
          <CodeBlock id="clone" code={`POST ${containerUrl}/projects/clone`} />
          <details className="mt-2">
            <summary className="text-[11px] font-bold text-muted cursor-pointer hover:text-text transition-colors">Example payload</summary>
            <div className="mt-2"><CodeBlock id="clonePayload" code={JSON.stringify({
              git_url: "https://bitbucket.example.com/scm/PROJ/repo.git",
              access_token: "BBDC-...",
              auth_mode: "bitbucket_datacenter_bearer",
            }, null, 2)} /></div>
          </details>
        </div>
      </Section>

      {/* ── MCP Server ── */}
      <Section title="MCP Server" icon={Download}>
        <p className="text-xs text-text-secondary m-0 mb-3 leading-relaxed">
          Connect your AI coding assistant (Claude Code or opencode) to Intelligraph. It can then search your codebase graph, find callers/callees, analyze impact, and more — right from your editor.
        </p>

        {!pid ? (
          <div className="p-3 rounded-lg bg-yellow-500/5 border border-yellow-500/20">
            <p className="text-xs text-text-secondary m-0">Select a project first. The config below auto-fills with your project ID ({pid || "none yet"}) and container URL.</p>
          </div>
        ) : (
          <>
            {/* Step 1 */}
            <div className="mb-4">
              <p className="text-xs font-bold text-text mb-1">Step 1 — Install dependencies</p>
              <p className="text-xs text-muted-subtle m-0 mb-2">Run this once on your machine:</p>
              <CodeBlock id="pip" code="pip install mcp requests" />
            </div>

            {/* Step 2 */}
            <div className="mb-4">
              <p className="text-xs font-bold text-text mb-1">Step 2 — Download the MCP server script</p>
              <p className="text-xs text-muted-subtle m-0 mb-2">Save this file into your project folder (the folder where you run your AI assistant):</p>
              <a href={endpoints.downloadMCPServer} download
                className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-accent/10 hover:bg-accent/20 text-accent-light text-xs font-medium transition-colors no-underline">
                <Download size={14} /> Download mcp_server_standalone.py
              </a>
            </div>

            {/* Step 3 */}
            <div className="mb-4">
              <p className="text-xs font-bold text-text mb-1">Step 3 — Add config file</p>
              <p className="text-xs text-muted-subtle m-0 mb-2">
                Your project ID is <span className="text-accent-light font-mono font-bold">{pid}</span> and your container is at <span className="text-accent-light font-mono">{containerUrl}</span>. These are already filled in below — just copy and paste.
              </p>

              <details open className="mt-2">
                <summary className="text-[11px] font-bold text-muted cursor-pointer hover:text-text transition-colors">Claude Code</summary>
                <div className="mt-2 space-y-2">
                  <p className="text-xs text-text-secondary m-0">
                    Create a file called <code className="px-1 py-0.5 rounded bg-accent/10 text-accent-light text-[11px] font-mono">.mcp.json</code> in your project folder. For global access, put it in <code className="px-1 py-0.5 rounded bg-accent/10 text-accent-light text-[11px] font-mono">~/.claude.json</code> instead.
                  </p>
                  <CodeBlock id="claudeMcp" code={claudeMcp} />
                </div>
              </details>

              <details className="mt-2">
                <summary className="text-[11px] font-bold text-muted cursor-pointer hover:text-text transition-colors">opencode</summary>
                <div className="mt-2 space-y-2">
                  <p className="text-xs text-text-secondary m-0">
                    Create a file called <code className="px-1 py-0.5 rounded bg-accent/10 text-accent-light text-[11px] font-mono">opencode.json</code> in your project folder. For global access, put it in <code className="px-1 py-0.5 rounded bg-accent/10 text-accent-light text-[11px] font-mono">~/.config/opencode/opencode.json</code>.
                  </p>
                  <CodeBlock id="opencodeMcp" code={opencodeMcp} />
                </div>
              </details>
            </div>

            {/* Step 4 */}
            <div className="mb-2">
              <p className="text-xs font-bold text-text mb-1">Step 4 — Use it</p>
              <p className="text-xs text-text-secondary m-0 leading-relaxed">
                Open your AI assistant in the project folder and ask questions like "search for authentication" or "who calls processPayment". The assistant will use Intelligraph's code graph to answer.
              </p>
            </div>

            <details className="mt-3">
              <summary className="text-[11px] font-bold text-muted cursor-pointer hover:text-text transition-colors">Or run manually (for testing)</summary>
              <div className="mt-2"><CodeBlock id="mcpCmd" code={mcpCommand} /></div>
            </details>
          </>
        )}
      </Section>

      {/* ── How it works ── */}
      <Section title="How it works" icon={FileCode}>
        <p className="text-xs text-text-secondary m-0 leading-relaxed">Uses the same <code className="px-1 py-0.5 rounded bg-accent/10 text-accent-light text-[11px] font-mono">retrieval.py</code> runtime as the web UI. Pipeline: ExecutionPlanner → NodeResolver → TraversalPlanner → NeighborhoodRanker → ChunkRetriever → ContextMerger.</p>
      </Section>
    </div>
  );
}
function Section({ title, icon: Icon, children }) {
  return <div className="glass rounded-xl p-4"><div className="flex items-center gap-2 mb-3">{Icon && <Icon size={16} className="text-accent-light shrink-0" />}<h3 className="text-sm font-bold text-text m-0">{title}</h3></div>{children}</div>;
}
