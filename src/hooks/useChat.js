import { useState, useCallback, useRef } from "react";
import { detectIntent } from "../utils/intentDetector";
import { llmService } from "../services/llmService";
import { graphifyService } from "../services/graphifyService";

// ── Prompt builders (graphify + CRG + code-chunks context) ──

const SYSTEM_PROMPT = `You are an expert software architect helping a developer understand a codebase.

Give a direct, concise answer. Do not output your thinking process or say "Let me analyze" — just answer.
Use the provided context as your only source of truth. Mention specific file paths.
If context is insufficient, state what is missing.
Do not invent files, functions, imports, or APIs.`;

const MAX_CONTEXT_CHARS = 8000;
const MAX_SNIPPET_CHARS = 2000;
const MIN_SNIPPET_SCORE = 0.40;

async function buildRichContext(prompt, intent, graphData, crgQueries, activePid) {
  const sections = [];
  let totalChars = 0;
  const { searchNodes, callers, callees, impact, architecture, tests } = crgQueries;

  // 1. CRG structural searches FIRST (fast, precise, FTS5)
  let crgResult = {};
  try {
    const t = prompt;
    switch (intent) {
      case "architecture":
        crgResult = architecture();
        break;
      case "what_is":
        crgResult = { matches: searchNodes(t, 20) };
        break;
      case "how_works": {
        crgResult = {
          node: searchNodes(t, 5),
          callers: callers(t, 15),
          callees: callees(t, 15),
        };
        break;
      }
      case "callers":
        crgResult = { matched: searchNodes(t, 5), callers: callers(t, 40) };
        break;
      case "callees":
        crgResult = { callees: callees(t, 40) };
        break;
      case "impact":
        crgResult = impact(t);
        break;
      case "tests":
        crgResult = { tests: tests(t) };
        break;
      default:
        crgResult = architecture();
    }
  } catch (e) {
    console.warn("CRG query error:", e);
  }

  // 2. graphify query — full semantic search (article pattern: query + explain)
  if (activePid) {
    try {
      const gfQuery = await graphifyService.query({ prompt: prompt.slice(0, 200), pid: activePid });
      if (gfQuery?.result) {
        const text = `## Repo Context\n${gfQuery.result}`;
        if (totalChars + text.length <= MAX_CONTEXT_CHARS) {
          sections.push(text);
          totalChars += text.length;
        }
      }
    } catch (e) { console.warn("graphify query failed:", e); }
  }

  // 3. graphify explain — semantic analysis (plain language)
  if (activePid) {
    try {
      const gfExplain = await graphifyService.explain({ concept: prompt.slice(0, 120), pid: activePid });
      if (gfExplain?.result && gfExplain.result !== "No results") {
        const text = `## Analysis\n${gfExplain.result}`;
        if (totalChars + text.length <= MAX_CONTEXT_CHARS) {
          sections.push(text);
          totalChars += text.length;
        }
      }
    } catch (e) { console.warn("graphify explain failed:", e); }
  }

  // 4. CRG match results — formatted as structure tree (Hackathon pattern)
  if (crgResult.matches?.length) {
    let text = "\n## Matching Files & Functions\n";
    crgResult.matches.forEach((m) => {
      const name = m.name || m.qualified_name || "?";
      const loc = m.file_path || "";
      const line = `- \`${name}\` — ${loc}${m.kind ? ` (${m.kind})` : ""}${m.line_start ? ` L${m.line_start}` : ""}\n`;
      if (totalChars + text.length + line.length <= MAX_CONTEXT_CHARS) {
        text += line;
      }
    });
    sections.push(text);
    totalChars += text.length;
  }

  // 5. Code chunks — actual source code (Hackathon pattern: scored, truncated)
  const allMatches = [
    ...(crgResult.matches || []),
    ...(crgResult.node || []),
    ...(crgResult.matched || []),
    ...(crgResult.callers || []),
    ...(crgResult.callees || []),
  ];
  const allFiles = [...new Set(allMatches.map((m) => m.file_path).filter(Boolean))].slice(0, 10);

  if (allFiles.length && activePid) {
    try {
      const chunkResp = await graphifyService.codeChunks({ filePaths: allFiles, pid: activePid });
      const chunks = chunkResp?.chunks || [];
      if (chunks.length) {
        let text = "\n## Source Code\n";
        for (const c of chunks) {
          const lang = (c.file_path || "").split(".").pop() || "";
          const snippet = c.content?.length > MAX_SNIPPET_CHARS
            ? c.content.slice(0, MAX_SNIPPET_CHARS) + "\n// ... (truncated)"
            : c.content;
          const block = `### ${c.file_path} — \`${c.name}\` (L${c.start_line}-${c.end_line})\n\`\`\`${lang}\n${snippet}\n\`\`\`\n`;
          if (totalChars + text.length + block.length > MAX_CONTEXT_CHARS) break;
          text += block;
        }
        sections.push(text);
        totalChars += text.length;
      }
    } catch (e) {
      console.warn("code chunks failed:", e);
    }
  }

  return sections.join("\n") || "(no data available)";
}

export function useChat({ graphData, crgDbRef, searchNodes, callers, callees, impact, architecture, tests, activePid, llmUrl, llmToken, model }) {
  const [messages, setMessages] = useState([]);
  const [status, setStatus] = useState("idle");
  const [streamingContent, setStreamingContent] = useState("");
  const [pathWarnings, setPathWarnings] = useState(null);
  const abortRef = useRef(null);

  const addMessage = useCallback((msg) => {
    setMessages((prev) => [...prev, { ...msg, id: `${Date.now()}-${Math.random().toString(36).slice(2)}`, createdAt: new Date().toISOString() }]);
  }, []);

  const clearMessages = useCallback(() => {
    setMessages([]);
    setStatus("idle");
    setStreamingContent("");
    setPathWarnings(null);
  }, []);

  const sendMessage = useCallback(async (prompt) => {
    if (!prompt.trim()) return;
    const trimmed = prompt.trim();
    addMessage({ role: "user", content: trimmed });

    setStatus("classifying");
    setStreamingContent("");
    setPathWarnings(null);

    // Classify intent
    let intent, target;
    try {
      const c = await llmService.classify(trimmed);
      intent = c.intent || "architecture";
      target = c.target || trimmed;
    } catch {
      const detected = detectIntent(trimmed);
      intent = detected.intent;
      target = detected.target || trimmed;
    }

    // Build rich context: graphify + CRG + code chunks
    setStatus("thinking");
    const crgQueries = { searchNodes, callers, callees, impact, architecture, tests };
    let result = {};
    try {
      result = { intent };
    } catch (e) {
      console.error("Graph query error:", e);
    }

    const richContext = await buildRichContext(trimmed, intent, graphData, crgQueries, activePid);

    if (!llmUrl) {
      addMessage({ role: "assistant", content: "", metadata: { intent, result, route: { category: intent, label: intent } } });
      setStatus("idle");
      return;
    }

    // Stream LLM
    setStatus("answering");
    let fullText = "";
    let pw = null;
    try {
      const payload = {
        model: model || undefined,
        messages: [
          { role: "system", content: SYSTEM_PROMPT },
          { role: "user", content: `${richContext}\n\n# User Query\n\n${trimmed}` },
        ],
        max_tokens: 2048,
        temperature: 0.2,
        stream: true,
      };

      const stream = llmService.relayStream({ url: llmUrl, token: llmToken, payload, projectId: activePid });
      for await (const { event, data } of stream) {
        if (event === "token") {
          fullText += data.text || "";
          setStreamingContent(fullText);
        } else if (event === "done") {
          fullText = data.text || fullText;
          pw = data.path_warnings || null;
        } else if (event === "error") {
          console.error("SSE error:", data.message);
        }
      }
    } catch (e) {
      console.error("SSE stream failed:", e);
      // Fallback to sync
      try {
        const j = await llmService.relay({
          url: llmUrl,
          token: llmToken,
          payload: {
            model: model || undefined,
            messages: [
              { role: "system", content: SYSTEM_PROMPT },
              { role: "user", content: `${richContext}\n\n# User Query\n\n${trimmed}` },
            ],
            max_tokens: 2048,
            temperature: 0.2,
          },
          projectId: activePid,
        });
        const body = JSON.parse(j.body || "{}");
        fullText = body.choices?.[0]?.message?.content || "";
      } catch {}
    }

    setPathWarnings(pw);
    addMessage({
      role: "assistant",
      content: fullText,
      metadata: { intent, result, route: { category: intent, label: intent }, pathWarnings: pw },
    });
    setStreamingContent("");
    setStatus("idle");
  }, [addMessage, searchNodes, callers, callees, impact, architecture, tests, activePid, llmUrl, llmToken, model, graphData]);

  return { messages, status, streamingContent, pathWarnings, sendMessage, clearMessages };
}