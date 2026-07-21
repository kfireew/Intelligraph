import { useState, useEffect, useCallback } from "react";
import { SlidersHorizontal, Info, ChevronDown, ChevronUp } from "lucide-react";

const DEFAULT_MAX_TOKENS = 4096;
const DEFAULT_BUDGET = 12000;
const MIN_BUDGET = 3000;
const MAX_BUDGET = 48000;
const DEFAULT_EMBEDDING_WEIGHT = 0.4;
const DEFAULT_TRAVERSAL_DEPTH = 2;
const DEFAULT_SNIPPET_CHARS = 800;

const DEFAULT_CHUNK_CAPS = {
  architecture: 15, how_works: 15, explain: 10, debug: 12,
  refactor: 12, coverage: 12, impact: 12, callers: 12, callees: 12,
  what_is: 8, nx_architecture: 10,
};

const CHUNK_CAP_LABELS = {
  architecture: "Architecture", how_works: "How works", explain: "Explain",
  debug: "Debug", refactor: "Refactor", coverage: "Coverage", impact: "Impact",
  callers: "Callers", callees: "Callees", what_is: "What is", nx_architecture: "Nx architecture",
};

const DETAIL_PRESETS = {
  1: { label: "Concise", budget: 4000, maxTokens: 2048, snippetChars: 0 },
  2: { label: "Light", budget: 8000, maxTokens: 3072, snippetChars: 400 },
  3: { label: "Balanced", budget: 12000, maxTokens: 4096, snippetChars: 800 },
  4: { label: "Thorough", budget: 24000, maxTokens: 8192, snippetChars: 1500 },
  5: { label: "Exhaustive", budget: 48000, maxTokens: 16384, snippetChars: 3000 },
};

const SEARCH_MODES = [
  { label: "Keyword", weight: 0.0, desc: "Exact symbol names. Best when you know what you're looking for." },
  { label: "Balanced", weight: 0.4, desc: "Keyword + semantic. Good default for most questions." },
  { label: "Semantic", weight: 1.0, desc: "Meaning-based. Best for fuzzy natural language questions." },
];

function load() {
  const maxTokens = parseInt(localStorage.getItem("tuning-max-tokens") || "", 10);
  const budget = parseInt(localStorage.getItem("tuning-budget-chars") || "", 10);
  const embeddingWeight = parseFloat(localStorage.getItem("tuning-embedding-weight") || "");
  const traversalDepth = parseInt(localStorage.getItem("tuning-traversal-depth") || "", 10);
  const snippetChars = parseInt(localStorage.getItem("tuning-snippet-chars") || "", 10);
  let chunkCaps = { ...DEFAULT_CHUNK_CAPS };
  try {
    const saved = JSON.parse(localStorage.getItem("tuning-chunk-caps") || "null");
    if (saved && typeof saved === "object") {
      for (const key of Object.keys(DEFAULT_CHUNK_CAPS)) {
        if (saved[key] !== undefined) chunkCaps[key] = saved[key];
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

function budgetToDetail(budget, snippetChars) {
  for (const [level, preset] of Object.entries(DETAIL_PRESETS)) {
    if (preset.budget >= budget && preset.snippetChars >= snippetChars) return parseInt(level, 10);
  }
  return 3;
}

export function TuningPanel() {
  const [state, setState] = useState(load);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const { maxTokens, budgetChars, chunkCaps, embeddingWeight, traversalDepth, snippetChars } = state;
  const detailLevel = budgetToDetail(budgetChars, snippetChars);

  useEffect(() => {
    localStorage.setItem("tuning-max-tokens", String(maxTokens));
    localStorage.setItem("tuning-budget-chars", String(budgetChars));
    localStorage.setItem("tuning-chunk-caps", JSON.stringify(chunkCaps));
    localStorage.setItem("tuning-embedding-weight", String(embeddingWeight));
    localStorage.setItem("tuning-traversal-depth", String(traversalDepth));
    localStorage.setItem("tuning-snippet-chars", String(snippetChars));
  }, [maxTokens, budgetChars, chunkCaps, embeddingWeight, traversalDepth, snippetChars]);

  const setDetailLevel = useCallback((level) => {
    const preset = DETAIL_PRESETS[level];
    if (preset) {
      setState((s) => ({ ...s, budgetChars: preset.budget, maxTokens: preset.maxTokens, snippetChars: preset.snippetChars }));
    }
  }, []);

  const setSearchMode = useCallback((weight) => {
    setState((s) => ({ ...s, embeddingWeight: weight }));
  }, []);

  const setChunkCap = useCallback((key, v) => {
    setState((s) => ({ ...s, chunkCaps: { ...s.chunkCaps, [key]: v } }));
  }, []);

  return (
    <div className="flex flex-col flex-1 min-h-0 p-6 overflow-y-auto space-y-6">
      <div className="flex items-center gap-2">
        <SlidersHorizontal size={18} className="text-accent-light" />
        <h2 className="text-lg font-bold gradient-text">Tuning</h2>
      </div>

      {/* Detail Level — the main control */}
      <div className="glass rounded-xl p-4 space-y-3">
        <div className="flex items-center justify-between">
          <label className="text-sm font-bold text-text m-0">Detail level</label>
          <span className="text-xs font-mono text-accent-light">{DETAIL_PRESETS[detailLevel]?.label}</span>
        </div>
        <p className="text-[11px] text-muted-subtle m-0">
          How much context and how long the answer should be. Higher = more code context + longer answers, but slower.
        </p>
        <div className="flex gap-1">
          {[1, 2, 3, 4, 5].map((level) => (
            <button
              key={level}
              onClick={() => setDetailLevel(level)}
              className={`flex-1 py-2 rounded-lg text-[11px] font-bold transition-all ${
                detailLevel === level
                  ? "bg-accent text-white shadow-lg shadow-accent/20"
                  : "bg-white/5 text-muted hover:text-text hover:bg-white/10"
              }`}
            >
              {level}
            </button>
          ))}
        </div>
        <div className="flex justify-between text-[10px] text-muted">
          <span>Concise</span><span>Balanced</span><span>Exhaustive</span>
        </div>
      </div>

      {/* Search Mode — the second main control */}
      <div className="glass rounded-xl p-4 space-y-3">
        <label className="text-sm font-bold text-text m-0">Search mode</label>
        <div className="flex gap-1">
          {SEARCH_MODES.map((mode) => (
            <button
              key={mode.label}
              onClick={() => setSearchMode(mode.weight)}
              className={`flex-1 py-2 px-2 rounded-lg text-[11px] font-bold transition-all ${
                Math.abs(embeddingWeight - mode.weight) < 0.01
                  ? "bg-accent text-white shadow-lg shadow-accent/20"
                  : "bg-white/5 text-muted hover:text-text hover:bg-white/10"
              }`}
            >
              {mode.label}
            </button>
          ))}
        </div>
        <p className="text-[10px] text-muted-subtle m-0">
          {SEARCH_MODES.find(m => Math.abs(embeddingWeight - m.weight) < 0.01)?.desc || "Custom weight"}
        </p>
      </div>

      {/* Advanced toggle */}
      <button
        onClick={() => setShowAdvanced(!showAdvanced)}
        className="flex items-center gap-1 text-[11px] text-muted hover:text-text transition-colors"
      >
        {showAdvanced ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
        Advanced settings
      </button>

      {showAdvanced && (
        <>
          {/* Chunk count table */}
          <div className="glass rounded-xl p-4">
            <div className="flex items-center justify-between mb-3">
              <label className="text-sm font-bold text-text m-0">Chunk limits per task type</label>
              <button
                onClick={() => setState((s) => ({ ...s, chunkCaps: { ...DEFAULT_CHUNK_CAPS } }))}
                className="text-[10px] text-muted hover:text-text transition-colors"
              >Reset</button>
            </div>
            <div className="grid grid-cols-2 gap-x-4 gap-y-1.5">
              {Object.entries(chunkCaps).map(([key, val]) => (
                <div key={key} className="flex items-center justify-between gap-2 py-0.5">
                  <span className="text-[11px] text-text-secondary">{CHUNK_CAP_LABELS[key] || key}</span>
                  <input
                    type="number" min={1} max={30}
                    value={val}
                    onChange={(e) => setChunkCap(key, Math.max(1, Math.min(30, parseInt(e.target.value, 10) || 1)))}
                    className="w-14 px-1.5 py-0.5 rounded bg-white/5 border border-glass-border focus:border-accent/40 text-[11px] text-text text-center outline-none transition-colors"
                  />
                </div>
              ))}
            </div>
          </div>

          {/* Graph traversal depth */}
          <div className="glass rounded-xl p-4 space-y-3">
            <div className="flex items-center justify-between">
              <label className="text-sm font-bold text-text m-0">Graph traversal depth</label>
              <span className="text-xs font-mono text-accent-light">{traversalDepth} hop{traversalDepth > 1 ? "s" : ""}</span>
            </div>
            <input
              type="range" min={1} max={3} step={1}
              value={traversalDepth}
              onChange={(e) => setState((s) => ({ ...s, traversalDepth: parseInt(e.target.value, 10) }))}
              className="w-full accent-purple-500"
            />
            <div className="flex justify-between text-[10px] text-muted">
              <span>1 hop</span><span>2 hops</span><span>3 hops</span>
            </div>
          </div>

          {/* Manual overrides */}
          <div className="glass rounded-xl p-4 space-y-3">
            <div>
              <div className="flex items-center justify-between mb-1">
                <label className="text-xs font-bold text-text m-0">Token limit</label>
                <span className="text-xs font-mono text-accent-light">{maxTokens.toLocaleString()}</span>
              </div>
              <input type="range" min={512} max={32768} step={1024} value={maxTokens}
                onChange={(e) => setState((s) => ({ ...s, maxTokens: parseInt(e.target.value, 10) }))}
                className="w-full accent-purple-500" />
            </div>
            <div className="border-t border-white/5 pt-3">
              <div className="flex items-center justify-between mb-1">
                <label className="text-xs font-bold text-text m-0">Context budget</label>
                <span className="text-xs font-mono text-accent-light">{budgetChars.toLocaleString()} chars</span>
              </div>
              <input type="range" min={MIN_BUDGET} max={MAX_BUDGET} step={1000} value={budgetChars}
                onChange={(e) => setState((s) => ({ ...s, budgetChars: parseInt(e.target.value, 10) }))}
                className="w-full accent-purple-500" />
            </div>
            <div className="border-t border-white/5 pt-3">
              <div className="flex items-center justify-between mb-1">
                <label className="text-xs font-bold text-text m-0">Snippet chars</label>
                <span className="text-xs font-mono text-accent-light">{snippetChars.toLocaleString()}</span>
              </div>
              <input type="range" min={0} max={3000} step={100} value={snippetChars}
                onChange={(e) => setState((s) => ({ ...s, snippetChars: parseInt(e.target.value, 10) }))}
                className="w-full accent-purple-500" />
            </div>
            <div className="border-t border-white/5 pt-3">
              <div className="flex items-center justify-between mb-1">
                <label className="text-xs font-bold text-text m-0">Semantic weight</label>
                <span className="text-xs font-mono text-accent-light">{embeddingWeight.toFixed(2)}</span>
              </div>
              <input type="range" min={0} max={1} step={0.05} value={embeddingWeight}
                onChange={(e) => setState((s) => ({ ...s, embeddingWeight: parseFloat(e.target.value) }))}
                className="w-full accent-purple-500" />
            </div>
          </div>
        </>
      )}

      <div className="glass rounded-xl p-4">
        <div className="flex items-start gap-2">
          <Info size={14} className="text-muted shrink-0 mt-0.5" />
          <p className="text-[11px] text-muted-subtle m-0 leading-relaxed">
            Detail level and search mode cover most needs. Use thumbs up/down on answers to auto-tune.
            Advanced settings are for fine-grained control.
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
