import { useState, useEffect, useCallback } from "react";
import { SlidersHorizontal, Info } from "lucide-react";

const DEFAULT_MAX_TOKENS = 4096;
const DEFAULT_BUDGET = 12000;
const MIN_BUDGET = 3000;
const MAX_BUDGET = 48000;
const DEFAULT_EMBEDDING_WEIGHT = 0.4;
const DEFAULT_TRAVERSAL_DEPTH = 2;
const DEFAULT_SNIPPET_CHARS = 1500;

const DEFAULT_CHUNK_CAPS = {
  architecture: 15,
  how_works: 15,
  explain: 10,
  debug: 12,
  refactor: 12,
  coverage: 12,
  impact: 12,
  callers: 12,
  callees: 12,
  what_is: 8,
  nx_architecture: 10,
};

const CHUNK_CAP_LABELS = {
  architecture: "Architecture",
  how_works: "How works",
  explain: "Explain",
  debug: "Debug",
  refactor: "Refactor",
  coverage: "Coverage",
  impact: "Impact",
  callers: "Callers",
  callees: "Callees",
  what_is: "What is",
  nx_architecture: "Nx architecture",
};

function load() {
  const maxTokens = parseInt(
    (typeof localStorage !== "undefined" && localStorage.getItem("tuning-max-tokens")) || "",
    10
  );
  const budget = parseInt(
    (typeof localStorage !== "undefined" && localStorage.getItem("tuning-budget-chars")) || "",
    10
  );
  const embeddingWeight = parseFloat(
    (typeof localStorage !== "undefined" && localStorage.getItem("tuning-embedding-weight")) || ""
  );
  const traversalDepth = parseInt(
    (typeof localStorage !== "undefined" && localStorage.getItem("tuning-traversal-depth")) || "",
    10
  );
  const snippetChars = parseInt(
    (typeof localStorage !== "undefined" && localStorage.getItem("tuning-snippet-chars")) || "",
    10
  );
  let chunkCaps = { ...DEFAULT_CHUNK_CAPS };
  try {
    const saved = JSON.parse(localStorage.getItem("tuning-chunk-caps") || "null");
    if (saved && typeof saved === "object") {
      for (const key of Object.keys(DEFAULT_CHUNK_CAPS)) {
        if (saved[key] !== undefined) {
          chunkCaps[key] = saved[key];
        }
      }
    }
  } catch {}
  return {
    maxTokens: (maxTokens && maxTokens >= 512 && maxTokens <= 32768) ? maxTokens : DEFAULT_MAX_TOKENS,
    budgetChars: (budget && budget >= MIN_BUDGET && budget <= MAX_BUDGET) ? budget : DEFAULT_BUDGET,
    chunkCaps,
    embeddingWeight: (!isNaN(embeddingWeight) && embeddingWeight >= 0 && embeddingWeight <= 1) ? embeddingWeight : DEFAULT_EMBEDDING_WEIGHT,
    traversalDepth: (traversalDepth && traversalDepth >= 1 && traversalDepth <= 3) ? traversalDepth : DEFAULT_TRAVERSAL_DEPTH,
    snippetChars: (!isNaN(snippetChars) && snippetChars >= 0 && snippetChars <= 3000) ? snippetChars : DEFAULT_SNIPPET_CHARS,
  };
}

export function TuningPanel() {
  const [{ maxTokens, budgetChars, chunkCaps, embeddingWeight, traversalDepth, snippetChars }, setState] = useState(load);

  useEffect(() => {
    if (typeof localStorage !== "undefined") {
      localStorage.setItem("tuning-max-tokens", String(maxTokens));
      localStorage.setItem("tuning-budget-chars", String(budgetChars));
      localStorage.setItem("tuning-chunk-caps", JSON.stringify(chunkCaps));
      localStorage.setItem("tuning-embedding-weight", String(embeddingWeight));
      localStorage.setItem("tuning-traversal-depth", String(traversalDepth));
      localStorage.setItem("tuning-snippet-chars", String(snippetChars));
    }
  }, [maxTokens, budgetChars, chunkCaps, embeddingWeight, traversalDepth, snippetChars]);

  const setMaxTokens = useCallback((v) => {
    setState((s) => ({ ...s, maxTokens: v }));
  }, []);
  const setBudgetChars = useCallback((v) => {
    setState((s) => ({ ...s, budgetChars: v }));
  }, []);
  const setChunkCap = useCallback((key, v) => {
    setState((s) => ({ ...s, chunkCaps: { ...s.chunkCaps, [key]: v } }));
  }, []);
  const setEmbeddingWeight = useCallback((v) => {
    setState((s) => ({ ...s, embeddingWeight: v }));
  }, []);
  const setTraversalDepth = useCallback((v) => {
    setState((s) => ({ ...s, traversalDepth: v }));
  }, []);
  const setSnippetChars = useCallback((v) => {
    setState((s) => ({ ...s, snippetChars: v }));
  }, []);

  return (
    <div className="flex flex-col flex-1 min-h-0 p-6 overflow-y-auto space-y-6">
      <div className="flex items-center gap-2">
        <SlidersHorizontal size={18} className="text-accent-light" />
        <h2 className="text-lg font-bold gradient-text">Tuning</h2>
      </div>

      {/* Token limit slider */}
      <div className="glass rounded-xl p-4 space-y-5">
        <div>
          <div className="flex items-center justify-between mb-1.5">
            <label className="text-sm font-bold text-text m-0">Token limit</label>
            <span className="text-xs font-mono text-accent-light">{maxTokens.toLocaleString()} tokens</span>
          </div>
          <p className="text-[11px] text-muted-subtle m-0 mb-3">
            Maximum tokens the LLM generates per answer. Higher = longer, more detailed responses.
          </p>
          <input
            type="range" min={512} max={32768} step={1024}
            value={maxTokens}
            onChange={(e) => setMaxTokens(parseInt(e.target.value, 10))}
            className="w-full accent-purple-500"
          />
          <div className="flex justify-between text-[10px] text-muted mt-1">
            <span>512</span><span>8K</span><span>16K</span><span>24K</span><span>32K</span>
          </div>
        </div>

        {/* Budget slider */}
        <div className="border-t border-white/5 pt-4">
          <div className="flex items-center justify-between mb-1.5">
            <label className="text-sm font-bold text-text m-0">Context budget</label>
            <span className="text-xs font-mono text-accent-light">{budgetChars.toLocaleString()} chars</span>
          </div>
          <p className="text-[11px] text-muted-subtle m-0 mb-3">
            Total characters of code context fed to the LLM. Higher = more code, slower. Lower = faster, less detail.
          </p>
          <input
            type="range" min={MIN_BUDGET} max={MAX_BUDGET} step={1000}
            value={budgetChars}
            onChange={(e) => setBudgetChars(parseInt(e.target.value, 10))}
            className="w-full accent-purple-500"
          />
          <div className="flex justify-between text-[10px] text-muted mt-1">
            <span>3K</span><span>12K</span><span>24K</span><span>36K</span><span>48K</span>
          </div>
        </div>
      </div>

      {/* Chunk count table */}
      <div className="glass rounded-xl p-4">
        <div className="flex items-center justify-between mb-3">
          <label className="text-sm font-bold text-text m-0">Chunk limits per task type</label>
          <button
            onClick={() => setState((s) => ({ ...s, chunkCaps: { ...DEFAULT_CHUNK_CAPS } }))}
            className="text-[10px] text-muted hover:text-text transition-colors"
          >
            Reset
          </button>
        </div>
        <p className="text-[11px] text-muted-subtle m-0 mb-3">
          Maximum number of code chunks retrieved per question type. Higher = more files, more context.
        </p>
        <div className="grid grid-cols-2 gap-x-4 gap-y-1.5">
          {Object.entries(chunkCaps).map(([key, val]) => (
            <div key={key} className="flex items-center justify-between gap-2 py-0.5">
              <span className="text-[11px] text-text-secondary">{CHUNK_CAP_LABELS[key] || key}</span>
              <input
                type="number" min={1} max={30}
                value={val}
                onChange={(e) => setChunkCap(key, Math.max(1, Math.min(30, parseInt(e.target.value, 10) || 1)))}
                className={`w-14 px-1.5 py-0.5 rounded bg-white/5 border text-[11px] text-text text-center outline-none transition-colors ${
                  val > 20 ? "border-orange-500/40" : "border-glass-border focus:border-accent/40"
                }`}
              />
            </div>
          ))}
        </div>
        {Object.values(chunkCaps).some(v => v > 20) && (
          <p className="text-[10px] text-orange-400 mt-2">
            High chunk counts may slow responses and increase memory usage.
          </p>
        )}
      </div>

      {/* Intelligence settings */}
      <div className="glass rounded-xl p-4 space-y-5">
        <div>
          <div className="flex items-center justify-between mb-1.5">
            <label className="text-sm font-bold text-text m-0">Semantic search weight</label>
            <span className="text-xs font-mono text-accent-light">{embeddingWeight.toFixed(1)}</span>
          </div>
          <p className="text-[11px] text-muted-subtle m-0 mb-3">
            Blend keyword + semantic search. 0.0 = keyword only, 0.4 = balanced (recommended), 1.0 = semantic only.
          </p>
          <input
            type="range" min={0} max={1} step={0.1}
            value={embeddingWeight}
            onChange={(e) => setEmbeddingWeight(parseFloat(e.target.value))}
            className="w-full accent-purple-500"
          />
          <div className="flex justify-between text-[10px] text-muted mt-1">
            <span>Keyword</span><span>Balanced</span><span>Semantic</span>
          </div>
        </div>

        <div className="border-t border-white/5 pt-4">
          <div className="flex items-center justify-between mb-1.5">
            <label className="text-sm font-bold text-text m-0">Graph traversal depth</label>
            <span className="text-xs font-mono text-accent-light">{traversalDepth} hop{traversalDepth > 1 ? "s" : ""}</span>
          </div>
          <p className="text-[11px] text-muted-subtle m-0 mb-3">
            How deep to explore the call graph. 1 = direct neighbors, 2 = 2-hop (recommended), 3 = deep context.
          </p>
          <input
            type="range" min={1} max={3} step={1}
            value={traversalDepth}
            onChange={(e) => setTraversalDepth(parseInt(e.target.value, 10))}
            className="w-full accent-purple-500"
          />
          <div className="flex justify-between text-[10px] text-muted mt-1">
            <span>1 hop</span><span>2 hops</span><span>3 hops</span>
          </div>
        </div>

        <div className="border-t border-white/5 pt-4">
          <div className="flex items-center justify-between mb-1.5">
            <label className="text-sm font-bold text-text m-0">Source code snippets</label>
            <span className="text-xs font-mono text-accent-light">{snippetChars.toLocaleString()} chars</span>
          </div>
          <p className="text-[11px] text-muted-subtle m-0 mb-3">
            Include source code snippets in context. 0 = off (lightweight only), 1500 = balanced (recommended), 3000 = max.
          </p>
          <input
            type="range" min={0} max={3000} step={500}
            value={snippetChars}
            onChange={(e) => setSnippetChars(parseInt(e.target.value, 10))}
            className="w-full accent-purple-500"
          />
          <div className="flex justify-between text-[10px] text-muted mt-1">
            <span>Off</span><span>1.5K</span><span>3K</span>
          </div>
        </div>
      </div>

      <div className="glass rounded-xl p-4">
        <div className="flex items-start gap-2">
          <Info size={14} className="text-muted shrink-0 mt-0.5" />
          <p className="text-[11px] text-muted-subtle m-0 leading-relaxed">
            Settings are saved automatically and persist across reloads (localStorage). They apply to the web chat and the completions API.
          </p>
        </div>
      </div>
    </div>
  );
}

export function useTuning() {
  const [{ maxTokens, budgetChars, chunkCaps, embeddingWeight, traversalDepth, snippetChars }] = useState(load);
  return { maxTokens, budgetChars, chunkCaps, embeddingWeight, traversalDepth, snippetChars };
}
