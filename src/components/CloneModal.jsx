import { useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { X, GitBranch, Upload as UploadIcon, Loader2, FileJson, Database, CheckCircle2, ArrowRight, FileCode } from "lucide-react";

export function CloneModal({ onClone, onClose, loading, onUploadComplete }) {
  const [gitUrl, setBitbucketUrl] = useState("");
  const [name, setName] = useState("");
  const [mode, setMode] = useState("bitbucket");
  const [status, setStatus] = useState("");
  const [uploadFiles, setUploadFiles] = useState({ graphify: null, crg: null, html: null });

  const handleClone = async () => {
    if (!gitUrl.trim()) return;
    try {
      setStatus("Cloning...");
      const p = await onClone({ gitUrl: gitUrl.trim(), name: name.trim() || undefined, type: "git" });
      setStatus(`Created: ${p.name || name || "project"} — ${gitUrl.trim()}`);
      setTimeout(() => onClose(), 1200);
    } catch (e) {
      console.error("Clone failed:", e);
      setStatus(`Error: ${e.message || e.statusText || "Clone failed"}`);
    }
  };

  const handleFileSelect = (e, type) => {
    const file = e.target.files[0];
    if (file) setUploadFiles((prev) => ({ ...prev, [type]: file }));
    e.target.value = "";
  };

  const handleUploadCreate = async () => {
    if (!uploadFiles.graphify || !uploadFiles.crg || !uploadFiles.html) return;
    try {
      setStatus("Creating project & uploading...");
      const r = await fetch("/projects/clone", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ git_url: "", name: name.trim() || uploadFiles.graphify.name, type: "upload" }),
      });
      const p = await r.json();
      if (!p.id) throw new Error("Project creation failed");
      const formData = new FormData();
      formData.append("graph_file", uploadFiles.graphify);
      formData.append("type", "graphify");
      await fetch(`/projects/${p.id}/upload-data`, { method: "POST", body: formData });
      const crgFD = new FormData();
      crgFD.append("graph_file", uploadFiles.crg);
      crgFD.append("type", "crg");
      await fetch(`/projects/${p.id}/upload-data`, { method: "POST", body: crgFD });
      const htmlFD = new FormData();
      htmlFD.append("graph_file", uploadFiles.html);
      htmlFD.append("type", "html");
      await fetch(`/projects/${p.id}/upload-data`, { method: "POST", body: htmlFD });
      setStatus(`Created: ${p.name || uploadFiles.graphify.name || "project"} — Upload complete`);
      setTimeout(() => {
        onClose();
        if (onUploadComplete) onUploadComplete(p.id);
      }, 1200);
    } catch (e) {
      setStatus(`Error: ${e.message || "Upload failed"}`);
    }
  };

  const allUploaded = uploadFiles.graphify && uploadFiles.crg && uploadFiles.html;

  return (
    <AnimatePresence>
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        className="fixed inset-0 z-50 flex items-center justify-center p-6"
        style={{ background: "rgba(0,0,0,0.6)", backdropFilter: "blur(6px)" }}
        onClick={onClose}
      >
        <motion.div
          initial={{ scale: 0.85, opacity: 0 }}
          animate={{ scale: 1, opacity: 1 }}
          exit={{ scale: 0.85, opacity: 0 }}
          transition={{ type: "spring", stiffness: 400, damping: 25 }}
          className="rounded-2xl w-full max-w-md p-6 shadow-2xl" style={{ background: "rgba(13,17,23,1)", border: "1px solid #21262d" }}
          onClick={(e) => e.stopPropagation()}
        >
          {/* Header */}
          <div className="flex items-center justify-between mb-5">
            <h2 className="text-base font-bold gradient-text m-0">New Project</h2>
            <button onClick={onClose} className="text-muted hover:text-red transition-colors p-0.5">
              <X size={18} />
            </button>
          </div>

          {/* Mode toggle */}
          <div className="flex gap-1 mb-5 p-0.5 rounded-lg bg-white/3">
            <button
              onClick={() => { setMode("bitbucket"); setStatus(""); }}
              className={`flex-1 flex items-center justify-center gap-1.5 py-1.5 rounded-md text-[11px] font-bold transition-colors ${
                mode === "bitbucket" ? "bg-accent/20 text-accent-light" : "text-muted hover:text-text"
              }`}
            >
              <GitBranch size={12} />
              Bitbucket
            </button>
            <button
              onClick={() => { setMode("upload"); setStatus(""); }}
              className={`flex-1 flex items-center justify-center gap-1.5 py-1.5 rounded-md text-[11px] font-bold transition-colors ${
                mode === "upload" ? "bg-accent/20 text-accent-light" : "text-muted hover:text-text"
              }`}
            >
              <UploadIcon size={12} />
              Upload
            </button>
          </div>

          {mode === "bitbucket" ? (
            <>
              <div className="mb-4">
                <label className="block text-[11px] font-bold text-muted uppercase tracking-wider mb-1.5">Bitbucket URL</label>
                <input
                  type="text" value={gitUrl}
                  onChange={(e) => setBitbucketUrl(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && handleClone()}
                  placeholder="https://bitbucket.org/workspace/repo (or github.com/...)"
                  className="w-full px-3 py-2 rounded-lg bg-white/3 border border-glass-border text-text text-sm outline-none focus:border-accent/40 transition-colors" autoFocus
                />
              </div>
              <div className="mb-5">
                <label className="block text-[11px] font-bold text-muted uppercase tracking-wider mb-1.5">
                  Name <span className="text-muted-subtle font-normal normal-case">(optional)</span>
                </label>
                <input
                  type="text" value={name}
                  onChange={(e) => setName(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && handleClone()}
                  placeholder="Auto-detected from URL"
                  className="w-full px-3 py-2 rounded-lg bg-white/3 border border-glass-border text-text text-sm outline-none focus:border-accent/40 transition-colors"
                />
              </div>
              {status && (
                <div className={`mb-4 p-2 rounded-lg text-[12px] flex items-center gap-2 ${status.startsWith("Error") ? "bg-red/10 text-red border border-red/20" : "bg-accent/10 text-accent-light"}`}>
                  {status.startsWith("Cloning") && <Loader2 size={14} className="animate-spin" />}
                  {status}
                </div>
              )}
              <div className="flex gap-2 justify-end">
                <button onClick={onClose} className="px-4 py-2 rounded-lg text-xs font-bold text-muted hover:text-text hover:bg-white/3 transition-colors">Cancel</button>
                <button onClick={handleClone} disabled={!gitUrl.trim() || loading}
                  className="px-5 py-2 rounded-lg text-xs font-bold text-white disabled:opacity-40 transition-opacity"
                  style={{ background: "linear-gradient(135deg, #8b5cf6, #d946ef)" }}>
                  {loading ? <Loader2 size={14} className="animate-spin" /> : "Clone"}
                </button>
              </div>
            </>
          ) : (
            <>
              <div className="mb-4">
                <label className="block text-[11px] font-bold text-muted uppercase tracking-wider mb-1.5">
                  Project Name <span className="text-muted-subtle font-normal normal-case">(optional)</span>
                </label>
                <input
                  type="text" value={name} onChange={(e) => setName(e.target.value)}
                  placeholder="My Project"
                  className="w-full px-3 py-2 rounded-lg bg-white/3 border border-glass-border text-text text-sm outline-none focus:border-accent/40 transition-colors" autoFocus
                />
              </div>

              <div className="grid grid-cols-3 gap-3 mb-5">
                {/* Graphify */}
                <motion.label
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                transition={{ duration: 0.35, ease: "easeOut" }}
                className={`glass rounded-xl p-4 flex flex-col items-center gap-2 cursor-pointer transition-all text-center ${
                  uploadFiles.graphify ? "border-green/30 bg-green/3" : "hover:bg-surface-hover"
                }`}>
                <input type="file" accept=".json" className="hidden" onChange={(e) => handleFileSelect(e, "graphify")} />
                <div className={`p-2.5 rounded-xl ${uploadFiles.graphify ? "bg-green/10" : "bg-white/4"}`}>
                  {uploadFiles.graphify ? <CheckCircle2 size={20} className="text-green" /> : <FileJson size={20} className="text-accent-light" />}
                </div>
                <div>
                  <div className="text-xs font-bold text-text">graph.json</div>
                  <div className="text-[10px] text-muted mt-0.5">
                    {uploadFiles.graphify ? uploadFiles.graphify.name : "Graphify output"}
                  </div>
                </div>
              </motion.label>

              {/* CRG */}
              <motion.label
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                transition={{ duration: 0.35, delay: 0.12, ease: "easeOut" }}
                className={`glass rounded-xl p-4 flex flex-col items-center gap-2 cursor-pointer transition-all text-center ${
                  uploadFiles.crg ? "border-green/30 bg-green/3" : "hover:bg-surface-hover"
                }`}>
                <input type="file" accept=".db" className="hidden" onChange={(e) => handleFileSelect(e, "crg")} />
                <div className={`p-2.5 rounded-xl ${uploadFiles.crg ? "bg-green/10" : "bg-white/4"}`}>
                  {uploadFiles.crg ? <CheckCircle2 size={20} className="text-green" /> : <Database size={20} className="text-cyan-400" />}
                </div>
                <div>
                  <div className="text-xs font-bold text-text">graph.db</div>
                  <div className="text-[10px] text-muted mt-0.5">
                    {uploadFiles.crg ? uploadFiles.crg.name : "CRG database"}
                  </div>
                </div>
              </motion.label>

              {/* HTML */}
              <motion.label
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                transition={{ duration: 0.35, delay: 0.24, ease: "easeOut" }}
                className={`glass rounded-xl p-4 flex flex-col items-center gap-2 cursor-pointer transition-all text-center ${
                  uploadFiles.html ? "border-green/30 bg-green/3" : "hover:bg-surface-hover"
                }`}>
                <input type="file" accept=".html" className="hidden" onChange={(e) => handleFileSelect(e, "html")} />
                <div className={`p-2.5 rounded-xl ${uploadFiles.html ? "bg-green/10" : "bg-white/4"}`}>
                  {uploadFiles.html ? <CheckCircle2 size={20} className="text-green" /> : <FileCode size={20} className="text-purple-400" />}
                </div>
                <div>
                  <div className="text-xs font-bold text-text">graph.html</div>
                  <div className="text-[10px] text-muted mt-0.5">
                    {uploadFiles.html ? uploadFiles.html.name : "Vis-network graph"}
                  </div>
                </div>
              </motion.label>
              </div>

              {status && (
                <div className={`mb-4 p-2 rounded-lg text-[12px] flex items-center gap-2 ${status.startsWith("Error") ? "bg-red/10 text-red border border-red/20" : "bg-accent/10 text-accent-light"}`}>
                  {status.startsWith("Creating") && <Loader2 size={14} className="animate-spin" />}
                  {status}
                </div>
              )}

              <div className="flex gap-2 justify-end">
                <button onClick={onClose} className="px-4 py-2 rounded-lg text-xs font-bold text-muted hover:text-text hover:bg-white/3 transition-colors">Cancel</button>
                <button
                  onClick={handleUploadCreate}
                  disabled={!allUploaded}
                  className="flex items-center gap-1.5 px-5 py-2 rounded-lg text-xs font-bold text-white disabled:opacity-40 transition-all"
                  style={{ background: allUploaded ? "linear-gradient(135deg, #8b5cf6, #d946ef)" : "rgba(255,255,255,0.06)" }}>
                  Create <ArrowRight size={14} />
                </button>
              </div>
            </>
          )}
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );
}