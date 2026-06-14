import { useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { X, GitBranch, Loader2, ChevronDown, ChevronRight } from "lucide-react";

export function CloneModal({ onClone, onClose, loading }) {
  const [gitUrl, setUrl] = useState("");
  const [name, setName] = useState("");
  const [status, setStatus] = useState("");
  const [showAuth, setShowAuth] = useState(false);
  const [accessToken, setAccessToken] = useState("");
  const [bitbucketUsername, setBitbucketUsername] = useState("");

  const clearSensitiveState = () => {
    setAccessToken("");
    setBitbucketUsername("");
  };

  const handleClone = async () => {
    if (!gitUrl.trim()) return;
    setStatus("Cloning...");
    try {
      const payload = { gitUrl: gitUrl.trim(), name: name.trim() || undefined };
      if (accessToken.trim()) {
        payload.accessToken = accessToken.trim();
        payload.bitbucketUsername = bitbucketUsername.trim() || undefined;
        payload.useLinkedCredentials = true;
        payload.authProvider = "bitbucket_datacenter";
      }
      await onClone(payload);
      setStatus("");
    } catch (e) {
      setStatus(`Error: ${e.message}`);
    } finally {
      clearSensitiveState();
    }
  };

  return (
    <AnimatePresence>
      <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
        className="fixed inset-0 z-50 flex items-center justify-center p-6"
        style={{ background: "rgba(0,0,0,0.6)", backdropFilter: "blur(6px)" }}
        onClick={onClose}>
        <motion.div initial={{ scale: 0.85, opacity: 0 }} animate={{ scale: 1, opacity: 1 }}
          exit={{ scale: 0.85, opacity: 0 }}
          transition={{ type: "spring", stiffness: 400, damping: 25 }}
          className="rounded-2xl w-full max-w-md p-6 shadow-2xl"
          style={{ background: "rgba(13,17,23,1)", border: "1px solid #21262d" }}
          onClick={(e) => e.stopPropagation()}>
          <div className="flex items-center justify-between mb-5">
            <h2 className="text-base font-bold gradient-text m-0">Clone Repository</h2>
            <button onClick={onClose} className="text-muted hover:text-red transition-colors p-0.5"><X size={18} /></button>
          </div>
          <div className="mb-4">
            <label className="block text-[11px] font-bold text-muted uppercase tracking-wider mb-1.5">Git URL</label>
            <input type="text" value={gitUrl} onChange={(e) => setUrl(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleClone()}
              placeholder="https://github.com/user/repo or https://bitbucket.example.com/scm/PROJECT/repo.git" autoFocus
              className="w-full px-3 py-2 rounded-lg bg-white/3 border border-glass-border text-text text-sm outline-none focus:border-accent/40 transition-colors" />
          </div>
          <div className="mb-4">
            <label className="block text-[11px] font-bold text-muted uppercase tracking-wider mb-1.5">Name <span className="text-muted-subtle font-normal normal-case">(optional)</span></label>
            <input type="text" value={name} onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleClone()}
              placeholder="Auto-detected from URL"
              className="w-full px-3 py-2 rounded-lg bg-white/3 border border-glass-border text-text text-sm outline-none focus:border-accent/40 transition-colors" />
          </div>

          {/* Bitbucket authentication section */}
          <div className="mb-4">
            <button
              onClick={() => setShowAuth(!showAuth)}
              className="flex items-center gap-2 text-[11px] font-bold text-muted uppercase tracking-wider mb-1.5 hover:text-text transition-colors w-full text-left">
              {showAuth ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
              Bitbucket authentication
            </button>
            {showAuth && (
              <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }}
                exit={{ height: 0, opacity: 0 }} className="overflow-hidden">
                <div className="mb-3">
                  <label className="block text-[11px] font-bold text-muted uppercase tracking-wider mb-1.5">Bitbucket Data Center HTTP access token</label>
                  <input type="password" value={accessToken} onChange={(e) => setAccessToken(e.target.value)}
                    placeholder="BBDC-..."
                    className="w-full px-3 py-2 rounded-lg bg-white/3 border border-glass-border text-text text-sm outline-none focus:border-accent/40 transition-colors font-mono tracking-widest" />
                  <p className="text-[10px] text-muted-subtle mt-1 leading-relaxed">
                    Required for private Bitbucket repos unless Intelligraph already has a linked Bitbucket credential for your account. OpenID login alone does not grant Git clone access. Use a read-only Bitbucket HTTP access token when possible.
                  </p>
                </div>
                <div className="mb-3">
                  <label className="block text-[11px] font-bold text-muted uppercase tracking-wider mb-1.5">Bitbucket username <span className="text-muted-subtle font-normal normal-case">(optional)</span></label>
                  <input type="text" value={bitbucketUsername} onChange={(e) => setBitbucketUsername(e.target.value)}
                    placeholder="username"
                    className="w-full px-3 py-2 rounded-lg bg-white/3 border border-glass-border text-text text-sm outline-none focus:border-accent/40 transition-colors" />
                  <p className="text-[10px] text-muted-subtle mt-1 leading-relaxed">
                    Only needed if your Bitbucket server expects a username with the HTTP access token.
                  </p>
                </div>
              </motion.div>
            )}
          </div>

          {status && (
            <div className={`mb-4 p-2 rounded-lg text-[12px] flex items-center gap-2 ${
              status.startsWith("Error") ? "bg-red/10 text-red border border-red/20" : "bg-accent/10 text-accent-light"
            }`}>{status.startsWith("Cloning") && <Loader2 size={14} className="animate-spin" />}{status}</div>
          )}
          <div className="flex gap-2 justify-end">
            <button onClick={onClose} className="px-4 py-2 rounded-lg text-xs font-bold text-muted hover:text-text hover:bg-white/3 transition-colors">Cancel</button>
            <button onClick={handleClone} disabled={!gitUrl.trim() || loading}
              className="px-5 py-2 rounded-lg text-xs font-bold text-white disabled:opacity-40 transition-opacity"
              style={{ background: "linear-gradient(135deg, #8b5cf6, #d946ef)" }}>
              {loading ? <Loader2 size={14} className="animate-spin" /> : "Clone"}
            </button>
          </div>
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );
}