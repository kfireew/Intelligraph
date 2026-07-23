import { Download, Server, Copy, Check, FileCode, KeyRound, Loader2, AlertCircle } from "lucide-react";
import { useState, useEffect, useCallback } from "react";
import { endpoints } from "../config/endpoints";
import { requestJson } from "../services/apiClient";

function copy(text) {
  navigator.clipboard.writeText(text);
}

export function GuidePanel({ activePid, activeProject }) {
  const [copied, setCopied] = useState(null);
  const [scriptPath, setScriptPath] = useState("");
  const [mcpToken, setMcpToken] = useState("");
  const [tokenLoading, setTokenLoading] = useState(false);
  const [tokenError, setTokenError] = useState("");
  const [siteUrl, setSiteUrl] = useState("");
  const [scriptSaved, setScriptSaved] = useState(false);
  const isReady = activeProject && ["ready", "cloned", "indexed"].includes(activeProject.status);
  const pid = activeProject?.id;

  // Auto-fill site URL: prefer INTELLIGRAPH_SITE_URL from /status,
  // fall back to window.location.origin, then to localhost:5050.
  useEffect(() => {
    (async () => {
      try {
        const res = await fetch("/status");
        const data = await res.json();
        if (data.site_url) { setSiteUrl(data.site_url); return; }
      } catch {}
      setSiteUrl(typeof window !== "undefined" ? window.location.origin : "http://localhost:5050");
    })();
  }, []);

  // Load saved MCP token for this project on mount / project switch.
  // Try localStorage first (fast), then the backend GET endpoint (source of
  // truth, survives browser data clears / different machines).
  const loadToken = useCallback(async (pid) => {
    if (!pid) return;
    const cacheKey = `mcp-token-${pid}`;
    const cached = (typeof localStorage !== "undefined" && localStorage.getItem(cacheKey)) || "";
    if (cached) { setMcpToken(cached); return; }
    try {
      const res = await requestJson(`/projects/${pid}/mcp-token`, { method: "GET" });
      if (res.mcp_token) {
        setMcpToken(res.mcp_token);
        if (typeof localStorage !== "undefined") localStorage.setItem(cacheKey, res.mcp_token);
      }
    } catch { /* 401 / not found — user will click Generate */ }
  }, []);

  useEffect(() => { loadToken(pid); }, [pid, loadToken]);

  // Load saved script path from localStorage (fast) then backend (source of truth).
  const loadScriptPath = useCallback(async (pid) => {
    if (!pid) return;
    const cacheKey = `script-path-${pid}`;
    const cached = (typeof localStorage !== "undefined" && localStorage.getItem(cacheKey)) || "";
    if (cached) { setScriptPath(cached); }
    try {
      const res = await requestJson(`/projects/${pid}/script-path`, { method: "GET" });
      if (res.script_path) {
        setScriptPath(res.script_path);
        if (typeof localStorage !== "undefined") localStorage.setItem(cacheKey, res.script_path);
      }
    } catch { /* 401 / not found — user will type it */ }
  }, []);

  useEffect(() => { loadScriptPath(pid); }, [pid, loadScriptPath]);

  // Debounced save of script path to backend
  const saveScriptPath = useCallback((pid, path) => {
    if (!pid) return;
    const clean = path.trim().replace(/^["']|["']$/g, "");
    if (typeof localStorage !== "undefined") localStorage.setItem(`script-path-${pid}`, clean);
    setScriptSaved(false);
    clearTimeout(saveScriptPath._t);
    saveScriptPath._t = setTimeout(async () => {
      try {
        await requestJson(`/projects/${pid}/script-path`, {
          method: "POST",
          body: JSON.stringify({ script_path: clean }),
        });
        setScriptSaved(true);
        setTimeout(() => setScriptSaved(false), 2000);
      } catch { /* ignore — will retry on next change */ }
    }, 600);
  }, []);

  const persistToken = useCallback((pid, token) => {
    setMcpToken(token);
    if (pid && typeof localStorage !== "undefined") {
      localStorage.setItem(`mcp-token-${pid}`, token);
    }
  }, []);

  const clearToken = useCallback((pid) => {
    setMcpToken("");
    if (pid && typeof localStorage !== "undefined") {
      localStorage.removeItem(`mcp-token-${pid}`);
    }
  }, []);

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

  // MCP config — standard python launch (works on host workstation, os4, localhost).
  // mcpUrl auto-fills from INTELLIGRAPH_SITE_URL env var or window.location.origin.
  const mcpContainerUrl = siteUrl || "http://localhost:5050";
  const cleanScriptPath = scriptPath.trim().replace(/^["']|["']$/g, "");
  const tokenArg = mcpToken.trim() || "YOUR-MCP-TOKEN";
  // Extract the directory from the script path — that's the local repo root.
  // e.g. C:\dev\myproject\mcp_server_standalone.py → C:\dev\myproject
  const repoDir = cleanScriptPath ? cleanScriptPath.replace(/[/\\][^/\\]+$/, "") : "";
  const scriptArgs = cleanScriptPath
    ? [cleanScriptPath, "--intelligraph-url", mcpContainerUrl, "--project-id", String(pid), "--mcp-token", tokenArg, "--repo-dir", repoDir]
    : null;

  const mcpCommand = (pid && scriptArgs) ? `python ${scriptArgs.join(" ")}` : null;

  const claudeMcp = (pid && scriptArgs)
    ? JSON.stringify({
        mcpServers: {
          intelligraph: {
            command: "python",
            args: scriptArgs,
          },
        },
      }, null, 2)
    : null;

  const opencodeMcp = (pid && scriptArgs)
    ? JSON.stringify({
        $schema: "https://opencode.ai/config.json",
        mcp: {
          intelligraph: {
            type: "local",
            command: ["python", ...scriptArgs],
            timeout: 120000,
          },
        },
      }, null, 2)
    : null;

  const handleGenerateToken = async () => {
    if (!pid) return;
    setTokenLoading(true);
    setTokenError("");
    try {
      const res = await requestJson(`/projects/${pid}/mcp-token`, { method: "POST" });
      persistToken(pid, res.mcp_token || "");
    } catch (e) {
      setTokenError(e.message || "Failed to generate token");
    } finally {
      setTokenLoading(false);
    }
  };

  const handleRevokeToken = async () => {
    if (!pid) return;
    setTokenLoading(true);
    setTokenError("");
    try {
      await requestJson(`/projects/${pid}/mcp-token`, { method: "DELETE" });
      clearToken(pid);
    } catch (e) {
      setTokenError(e.message || "Failed to revoke token");
    } finally {
      setTokenLoading(false);
    }
  };

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

            {/* Step 2 — Generate MCP Token */}
            <div className="mb-4">
              <p className="text-xs font-bold text-text mb-1">Step 2 — Generate MCP token</p>
              <p className="text-xs text-muted-subtle m-0 mb-2">
                The MCP server authenticates with this token (not your SSO session). Click generate, then copy it into the config below.
              </p>
              <div className="flex gap-2 items-center">
                {mcpToken ? (
                  <>
                    <input
                      type="text" readOnly value={mcpToken}
                      onClick={(e) => e.target.select()}
                      className="flex-1 px-2.5 py-1.5 rounded-lg bg-white/5 border border-glass-border text-[11px] text-text font-mono outline-none"
                    />
                    <button onClick={() => copy(mcpToken)} className="px-2.5 py-1.5 rounded-lg bg-accent/10 hover:bg-accent/20 text-accent-light text-[11px] font-medium transition-colors">
                      {copied === "mcpToken" ? <Check size={12} /> : <Copy size={12} />}
                    </button>
                    <button onClick={handleRevokeToken} disabled={tokenLoading}
                      className="px-2.5 py-1.5 rounded-lg bg-red/10 hover:bg-red/20 text-red text-[11px] font-medium transition-colors disabled:opacity-40">
                      Revoke
                    </button>
                  </>
                ) : (
                  <button onClick={handleGenerateToken} disabled={tokenLoading}
                    className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-bold text-white disabled:opacity-40"
                    style={{ background: "linear-gradient(135deg, #8b5cf6, #d946ef)" }}>
                    {tokenLoading ? <Loader2 size={14} className="animate-spin" /> : <KeyRound size={14} />}
                    <span>Generate Token</span>
                  </button>
                )}
              </div>
              {tokenError && (
                <div className="flex items-center gap-1.5 mt-2 text-[10px] text-red">
                  <AlertCircle size={11} /> {tokenError}
                </div>
              )}
            </div>

            {/* Step 3 */}
            <div className="mb-4">
              <p className="text-xs font-bold text-text mb-1">Step 3 — Download the MCP server script</p>
              <p className="text-xs text-muted-subtle m-0 mb-2">Save this file into your project folder (the folder where you run your AI assistant):</p>
              <a href={endpoints.downloadMCPServer} download
                className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-accent/10 hover:bg-accent/20 text-accent-light text-xs font-medium transition-colors no-underline">
                <Download size={14} /> Download mcp_server_standalone.py
              </a>
              <div className="mt-3">
                <div className="flex items-center gap-1.5 mb-1">
                  <p className="text-[11px] font-bold text-muted uppercase tracking-wider m-0">Full path to the script</p>
                  {!cleanScriptPath && <span className="text-[10px] text-red font-bold">REQUIRED</span>}
                  {scriptSaved && <span className="text-[10px] text-green flex items-center gap-0.5"><Check size={10} /> Saved</span>}
                </div>
                <p className="text-[10px] text-muted-subtle m-0 mb-1.5">
                  Paste the full path where you saved the file (with or without quotes). <span className="text-red">This is required — MCP will not work without it.</span> The directory of this path becomes your local <code className="px-1 py-0.5 rounded bg-accent/10 text-accent-light text-[10px] font-mono">--repo-dir</code>, used to rewrite graph paths to your local file system.
                </p>
                <input
                  type="text"
                  value={scriptPath}
                  onChange={(e) => { setScriptPath(e.target.value); saveScriptPath(pid, e.target.value); }}
                  placeholder="C:\Users\me\projects\myapp\mcp_server_standalone.py"
                  className={`w-full px-2.5 py-1.5 rounded-lg bg-white/5 border text-[11px] text-text font-mono outline-none transition-colors ${
                    cleanScriptPath ? "border-glass-border focus:border-accent/40" : "border-red/40 focus:border-red/60"
                  }`}
                />
              </div>
            </div>

            {/* Step 4 */}
            <div className="mb-4">
              <p className="text-xs font-bold text-text mb-1">Step 4 — Add config file</p>
              {!cleanScriptPath ? (
                <div className="p-2.5 rounded-lg bg-red/5 border border-red/20 flex items-center gap-1.5">
                  <AlertCircle size={14} className="text-red flex-shrink-0" />
                  <p className="text-[11px] text-red m-0">
                    Fill in the full path to the script in Step 3 above to generate your config. Without it, the MCP server can't find your local files.
                  </p>
                </div>
              ) : (
                <>
                  <p className="text-xs text-muted-subtle m-0 mb-2">
                    Your project ID is <span className="text-accent-light font-mono font-bold">{pid}</span>. The MCP server authenticates with your token at <span className="text-accent-light font-mono">{mcpContainerUrl}</span>. Just copy and paste.
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
                </>
              )}
            </div>

            {/* Step 5 */}
            <div className="mb-2">
              <p className="text-xs font-bold text-text mb-1">Step 5 — Use it</p>
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

      {/* ── Agent Guide ── */}
      <Section title="Agent Guide" icon={FileCode}>
        <p className="text-xs text-text-secondary m-0 mb-3 leading-relaxed">
          Some models (like Qwen3.6) tend to ignore MCP tools and answer from memory. Download this agent guide and add it to your AI assistant's instructions. It tells the model when and how to use each Intelligraph tool — without being so aggressive that it wastes tokens on unnecessary calls.
        </p>
        <a href={endpoints.downloadAgent} download
          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-accent/10 hover:bg-accent/20 text-accent-light text-xs font-medium transition-colors no-underline">
          <Download size={14} /> Download intelligraph-agent.md
        </a>

        <details className="mt-3">
          <summary className="text-[11px] font-bold text-muted cursor-pointer hover:text-text transition-colors">Claude Code</summary>
          <div className="mt-2 space-y-2">
            <p className="text-xs text-text-secondary m-0">
              Save the file as <code className="px-1 py-0.5 rounded bg-accent/10 text-accent-light text-[11px] font-mono">CLAUDE.md</code> in your project root. Claude Code reads this automatically as project instructions.
            </p>
            <CodeBlock id="claudeAgent" code={`# Save as CLAUDE.md in your project root\n# Claude Code reads this automatically\n\n@intelligraph-agent.md`} />
          </div>
        </details>

        <details className="mt-2">
          <summary className="text-[11px] font-bold text-muted cursor-pointer hover:text-text transition-colors">opencode</summary>
          <div className="mt-2 space-y-2">
            <p className="text-xs text-text-secondary m-0">
              Save the file in your project as <code className="px-1 py-0.5 rounded bg-accent/10 text-accent-light text-[11px] font-mono">AGENTS.md</code>. opencode reads this automatically as project instructions.
            </p>
            <CodeBlock id="opencodeAgent" code={`# Save as AGENTS.md in your project root\n# opencode reads this automatically\n\n@intelligraph-agent.md`} />
          </div>
        </details>
      </Section>

      {/* ── API Endpoints ── */}
      <Section title="API Endpoints" icon={Server}>
        {activeProject && (
          <div className="mb-3 p-2 rounded-lg bg-accent/5 border border-accent/10">
            <p className="text-xs text-text-secondary m-0">
              Project: <span className="text-text font-semibold">{activeProject.name}</span> &nbsp;|&nbsp; ID: <span className="text-accent-light font-mono">{pid}</span> &nbsp;|&nbsp; Status: <span className={isReady ? "text-green" : "text-yellow-400"}>{activeProject.status}</span> &nbsp;|&nbsp; Nodes: <span className="text-accent-light font-mono">{activeProject.nodes || 0}</span> &nbsp;|&nbsp; Edges: <span className="text-accent-light font-mono">{activeProject.edges || 0}</span>
            </p>
          </div>
        )}

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
                  model: "Qwen/Qwen3.6-27B-FP8",
                  context_used: true,
                  context_stats: { chunks: 12, tokens: 3400 },
                  path_warnings: [],
                }, null, 2)} /></div>
              </details>
            </>
          )}
        </div>

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
