import { useState, useRef, useEffect } from "react";
import { motion } from "framer-motion";
import { Settings, Save, Zap, Eye, EyeOff, Search, Loader2, Check } from "lucide-react";
import { StatusPill } from "./StatusPill";

export function LLMSettings({ llmUrl, llmToken, model, models, modelsLoading, testResult, onSave, onFetchModels, onSelectModel, onTest }) {
  const [url, setUrl] = useState(llmUrl);
  const [token, setToken] = useState(llmToken);
  const [showToken, setShowToken] = useState(false);
  const [search, setSearch] = useState("");
  const [dropdownOpen, setDropdownOpen] = useState(false);
  const dropdownRef = useRef(null);
  const fetchedRef = useRef(false);

  // Sync from hook when props change externally
  useEffect(() => { setUrl(llmUrl); }, [llmUrl]);
  useEffect(() => { setToken(llmToken); }, [llmToken]);

  // Auto-fetch models on mount only once
  useEffect(() => {
    if (llmUrl && llmToken && !models.length && !fetchedRef.current) {
      fetchedRef.current = true;
      onFetchModels();
    }
  }, []);

  // Close dropdown on outside click
  useEffect(() => {
    const handler = (e) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target)) {
        setDropdownOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  const triggerFetch = () => {
    if (!url || !token) return;
    onFetchModels();
  };

  const handleSave = () => { onSave(url, token); if (url && token) onFetchModels(); };

  const handleTest = () => {
    onSave(url, token);
    onTest(url.trim().replace(/\/+$/, ""), token);
  };

  const filtered = (models || []).filter((m) => {
    if (!search) return true;
    const q = search.toLowerCase();
    return m.id.toLowerCase().includes(q) || (m.name || "").toLowerCase().includes(q);
  });

  const selectedModel = (model && (models || []).find((m) => m.id === model)) || null;
  const showModelSearch = (models || []).length > 0;

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      className="flex-1 overflow-y-auto p-6 space-y-6"
    >
      <div className="flex items-center gap-2.5">
        <div className="p-2 rounded-lg bg-accent/10">
          <Settings size={18} className="text-accent-light" />
        </div>
        <div>
          <h2 className="text-base font-bold text-text m-0">LLM Configuration</h2>
          <p className="text-xs text-muted mt-0.5 m-0">
            All calls proxy through the pod relay. Settings saved to localStorage.
          </p>
        </div>
      </div>

      <div className="glass rounded-xl p-5 space-y-4 max-w-lg">
        {/* URL */}
        <div>
          <label className="block text-[11px] font-bold text-muted uppercase tracking-wider mb-1.5">
            LLM URL
          </label>
          <input
            type="text"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            onBlur={triggerFetch}
            placeholder="https://models.ai-services.idf.cts/v1/chat/completions"
            className="w-full px-3 py-2 rounded-lg bg-white/5 border border-border text-text text-xs outline-none focus:border-accent transition-colors"
          />
        </div>

        {/* Token */}
        <div>
          <label className="block text-[11px] font-bold text-muted uppercase tracking-wider mb-1.5">
            API Token
          </label>
          <div className="relative">
            <input
              type={showToken ? "text" : "password"}
              value={token}
              onChange={(e) => setToken(e.target.value)}
              onBlur={triggerFetch}
              placeholder="sk-..."
              className="w-full px-3 py-2 pr-9 rounded-lg bg-white/5 border border-border text-text text-xs outline-none focus:border-accent transition-colors"
            />
            <button
              onClick={() => setShowToken(!showToken)}
              className="absolute right-2 top-1/2 -translate-y-1/2 text-muted hover:text-text transition-colors"
            >
              {showToken ? <EyeOff size={14} /> : <Eye size={14} />}
            </button>
          </div>
        </div>

        {/* Model — auto-populated after save */}
        {showModelSearch ? (
          <div ref={dropdownRef} className="relative">
            <label className="block text-[11px] font-bold text-muted uppercase tracking-wider mb-1.5">
              Model
              {modelsLoading && <Loader2 size={10} className="inline ml-1.5 animate-spin text-accent-light" />}
              <span className="ml-1.5 text-[10px] font-normal normal-case text-muted-subtle">({models.length} available)</span>
            </label>
            <div className="relative">
              <Search size={12} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-subtle pointer-events-none" />
              <input
                type="text"
                value={search}
                onChange={(e) => { setSearch(e.target.value); setDropdownOpen(true); }}
                onFocus={() => setDropdownOpen(true)}
                placeholder={selectedModel ? selectedModel.name || selectedModel.id : "Search models..."}
                className="w-full pl-8 pr-3 py-2 rounded-lg bg-white/5 border border-border text-text text-xs outline-none focus:border-accent transition-colors"
              />
            </div>
            {dropdownOpen && filtered.length > 0 && (
              <div className="absolute z-20 left-0 right-0 mt-1 max-h-52 overflow-y-auto bg-[#0a0a0a] rounded-lg border border-glass-border shadow-xl">
                {filtered.slice(0, 50).map((m) => (
                  <button
                    key={m.id}
                    onClick={() => {
                      onSelectModel(m.id);
                      setSearch("");
                      setDropdownOpen(false);
                    }}
                    className={`w-full text-left px-3 py-2 text-xs flex items-center justify-between gap-2 hover:bg-surface-hover transition-colors ${
                      model === m.id ? "bg-accent/10" : ""
                    }`}
                  >
                    <div className="min-w-0">
                      <div className="text-text font-medium truncate">{m.name || m.id}</div>
                      <div className="text-muted-subtle text-[10px] truncate">{m.id}</div>
                    </div>
                    {model === m.id && <Check size={12} className="text-green flex-shrink-0" />}
                    {m.context_length > 0 && (
                      <span className="text-[10px] text-muted-subtle flex-shrink-0">{Math.round(m.context_length / 1000)}k</span>
                    )}
                  </button>
                ))}
              </div>
            )}
          </div>
        ) : (
          <div>
            <label className="block text-[11px] font-bold text-muted uppercase tracking-wider mb-1.5">
              Model
            </label>
            <input
              type="text"
              value={model}
              onChange={(e) => onSelectModel(e.target.value)}
              placeholder="gpt-4o-mini"
              className="w-full px-3 py-2 rounded-lg bg-white/5 border border-border text-text text-xs outline-none focus:border-accent transition-colors"
            />
          </div>
        )}

        {/* Buttons */}
        <div className="flex items-center gap-2 pt-1">
          <button
            onClick={handleSave}
            className="flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs font-semibold text-white bg-accent/20 hover:bg-accent/30 border border-accent/25 transition-colors"
          >
            <Save size={14} />
            Save
          </button>
          <button
            onClick={handleTest}
            disabled={!url}
            className="flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs font-semibold text-text bg-white/4 hover:bg-white/8 border border-border disabled:opacity-30 transition-colors"
          >
            <Zap size={14} />
            Test Connection
          </button>
          {testResult && (
            <StatusPill tone={testResult.startsWith("Connected") ? "success" : "error"}>
              {testResult.length > 30 ? testResult.slice(0, 30) + "..." : testResult}
            </StatusPill>
          )}
        </div>
      </div>
    </motion.div>
  );
}