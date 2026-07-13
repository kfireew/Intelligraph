import { useState, useRef, useEffect } from "react";
import { motion } from "framer-motion";
import { GitBranch, Search, Check, Loader2, RefreshCw, AlertCircle } from "lucide-react";

export function BranchPanel({ activePid, activeProject, onPull, fetchBranches }) {
  const [branches, setBranches] = useState([]);
  const [currentBranch, setCurrentBranch] = useState("");
  const [loading, setLoading] = useState(true);
  const [switching, setSwitching] = useState(false);
  const [error, setError] = useState("");
  const [search, setSearch] = useState("");
  const [selectedBranch, setSelectedBranch] = useState("");
  const [dropdownOpen, setDropdownOpen] = useState(false);
  const dropdownRef = useRef(null);

  const isCloned = !!(activeProject?.git_url);

  useEffect(() => {
    if (!activePid || !isCloned) {
      setBranches([]);
      setCurrentBranch("");
      setLoading(false);
      return;
    }
    setLoading(true);
    setError("");
    fetchBranches(activePid).then((data) => {
      if (data) {
        setBranches(data.branches || []);
        setCurrentBranch(data.current || "");
        setSelectedBranch(data.current || "");
      } else {
        setError("Failed to load branches");
      }
      setLoading(false);
    });
  }, [activePid, isCloned, fetchBranches]);

  useEffect(() => {
    const handler = (e) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target)) {
        setDropdownOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  const filtered = branches.filter((b) => {
    if (!search) return true;
    return b.toLowerCase().includes(search.toLowerCase());
  });

  const handleSwitch = async () => {
    if (!selectedBranch || selectedBranch === currentBranch) return;
    setSwitching(true);
    setError("");
    try {
      await onPull(activePid, selectedBranch);
      setCurrentBranch(selectedBranch);
    } catch (e) {
      setError(e.message || "Failed to switch branch");
    } finally {
      setSwitching(false);
    }
  };

  if (!activePid) {
    return (
      <div className="flex items-center justify-center h-full p-8">
        <p className="text-sm text-muted">No project selected</p>
      </div>
    );
  }

  if (!isCloned) {
    return (
      <div className="flex items-center justify-center h-full p-8">
        <div className="text-center max-w-[300px]">
          <GitBranch size={32} className="mx-auto mb-3 text-muted opacity-50" />
          <p className="text-sm text-muted">Branch switching is only available for cloned projects.</p>
        </div>
      </div>
    );
  }

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      className="flex flex-col h-full p-6 overflow-y-auto"
    >
      <div className="max-w-md w-full mx-auto">
        <div className="flex items-center gap-2 mb-6">
          <GitBranch size={20} className="text-accent-light" />
          <h2 className="text-sm font-bold text-text m-0">Branch Management</h2>
        </div>

        <div className="glass-bubble rounded-xl p-4 mb-4">
          <div className="text-[11px] text-muted mb-1">Project</div>
          <div className="text-sm font-medium text-text">{activeProject?.name}</div>
          {currentBranch && (
            <div className="flex items-center gap-1.5 mt-2">
              <GitBranch size={12} className="text-muted" />
              <span className="text-xs text-muted">Current: </span>
              <span className="text-xs font-medium text-accent-light">{currentBranch}</span>
            </div>
          )}
        </div>

        {loading ? (
          <div className="flex items-center justify-center py-8">
            <Loader2 size={20} className="animate-spin text-muted" />
          </div>
        ) : error ? (
          <div className="flex items-center gap-2 py-4 px-3 rounded-lg bg-red-500/10">
            <AlertCircle size={16} className="text-red flex-shrink-0" />
            <span className="text-xs text-red">{error}</span>
          </div>
        ) : (
          <>
            <label className="text-[11px] text-muted mb-1.5 block">Select Branch</label>
            <div className="relative" ref={dropdownRef}>
              <div className="relative">
                <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-muted pointer-events-none" />
                <input
                  type="text"
                  value={search}
                  onChange={(e) => { setSearch(e.target.value); setDropdownOpen(true); }}
                  onFocus={() => setDropdownOpen(true)}
                  placeholder={selectedBranch || "Search branches..."}
                  className="w-full pl-9 pr-3 py-2.5 rounded-lg bg-white/5 border border-glass-border text-sm text-text outline-none focus:border-accent transition-colors"
                />
              </div>
              {dropdownOpen && (
                <div className="absolute top-full left-0 right-0 mt-1 max-h-60 overflow-y-auto rounded-lg bg-[#0d1117] border border-glass-border z-50 shadow-xl">
                  {filtered.length === 0 ? (
                    <div className="px-3 py-2 text-xs text-muted">No branches found</div>
                  ) : (
                    filtered.slice(0, 50).map((b) => (
                      <button
                        key={b}
                        onClick={() => { setSelectedBranch(b); setSearch(""); setDropdownOpen(false); }}
                        className={`w-full flex items-center justify-between px-3 py-2 text-xs transition-colors hover:bg-white/5 ${
                          b === selectedBranch ? "text-accent-light bg-accent/5" : "text-text"
                        }`}
                      >
                        <span className="truncate font-mono">{b}</span>
                        {b === currentBranch && <Check size={12} className="text-green flex-shrink-0" />}
                      </button>
                    ))
                  )}
                </div>
              )}
            </div>

            <button
              onClick={handleSwitch}
              disabled={switching || !selectedBranch || selectedBranch === currentBranch}
              className="w-full mt-4 flex items-center justify-center gap-2 px-4 py-2.5 rounded-lg text-xs font-bold text-white transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
              style={{ background: "linear-gradient(135deg, #8b5cf6, #d946ef)" }}
            >
              {switching ? (
                <>
                  <Loader2 size={14} className="animate-spin" />
                  Switching & Rebuilding...
                </>
              ) : (
                <>
                  <RefreshCw size={14} />
                  Switch Branch
                </>
              )}
            </button>

            {selectedBranch && selectedBranch !== currentBranch && (
              <p className="text-[11px] text-muted mt-2 text-center">
                This will pull the <span className="font-mono text-accent-light">{selectedBranch}</span> branch and rebuild the graph.
              </p>
            )}
          </>
        )}
      </div>
    </motion.div>
  );
}
