import { useState, useCallback } from "react";
import { motion } from "framer-motion";
import {
  MessageSquare, Settings, Server, GitBranch,
  Plus, X, LogIn, LogOut, ChevronLeft,
  Loader2, CheckCircle2, AlertCircle, Clock,
  RefreshCw, Share2, KeyRound, AlertTriangle,
} from "lucide-react";

const STATUS_ICONS = {
  ready: CheckCircle2,
  cloning: Loader2,
  building: Loader2,
  pulling: Loader2,
  queued: Clock,
  error: AlertCircle,
  pending_upload: Clock,
};

const STATUS_COLORS = {
  ready: "text-green",
  cloning: "text-cyan animate-spin",
  building: "text-cyan animate-spin",
  pulling: "text-green animate-spin",
  queued: "text-orange",
  error: "text-red",
  pending_upload: "text-orange",
};

const NAV_ITEMS = [
  { panel: "chat", label: "Chat", icon: MessageSquare },
    { panel: "llm", label: "LLM", icon: Settings },
  { panel: "guide", label: "Guide", icon: Server },
];

const CLONED_NAV_ITEMS = [
  { panel: "branch", label: "Branch", icon: GitBranch },
];

export function Sidebar({
  projects, activePid, activePanel,
  auth, onSelectProject, onNewProject,
  onSwitchPanel, onRename, onDelete, onPull,
  tokenExpired, onShare, onJoin, onUpdateToken,
}) {
  const [collapsed, setCollapsed] = useState(false);
  const [renaming, setRenaming] = useState(null);
  const [showJoin, setShowJoin] = useState(false);
  const [joinKey, setJoinKey] = useState("");
  const [joinToken, setJoinToken] = useState("");
  const [joinError, setJoinError] = useState("");
  const [joinLoading, setJoinLoading] = useState(false);
  const [shareModal, setShareModal] = useState(null); // { pid, key }
  const [renewingPid, setRenewingPid] = useState(null);
  const [renewToken, setRenewToken] = useState("");

  const activeProject = projects.find((p) => p.id === activePid);
  const activeProjectHasGitUrl = !!(activeProject?.git_url);

  const handleRenameSubmit = useCallback((pid) => {
    const input = document.getElementById(`rename-input-${pid}`);
    if (input && input.value.trim()) {
      onRename(pid, input.value.trim());
    }
    setRenaming(null);
  }, [onRename]);

  const handleShare = async (pid) => {
    if (!onShare) return;
    const key = await onShare(pid);
    if (key) {
      setShareModal({ pid, key });
    }
  };

  const handleJoin = async () => {
    if (!joinKey.trim()) return;
    setJoinLoading(true);
    setJoinError("");
    try {
      await onJoin(joinKey.trim(), joinToken.trim() || undefined);
      setShowJoin(false);
      setJoinKey("");
      setJoinToken("");
    } catch (e) {
      setJoinError(e.message || "Join failed");
    } finally {
      setJoinLoading(false);
    }
  };

  const handleRenew = async (pid) => {
    if (!renewToken.trim() || !onUpdateToken) return;
    const ok = await onUpdateToken(pid, renewToken.trim());
    if (ok) {
      setRenewingPid(null);
      setRenewToken("");
    }
  };

  return (
    <motion.aside
      animate={{ width: collapsed ? 48 : 280 }}
      transition={{ type: "spring", stiffness: 400, damping: 30 }}
      className="glass flex flex-col flex-shrink-0 h-full overflow-hidden border-r border-glass-border"
    >
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-3.5 min-h-[52px]">
        {!collapsed && (
          <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="min-w-0">
            <h2 className="text-base font-bold gradient-text m-0 leading-tight">Intelligraph</h2>
            <p className="text-[10px] text-muted m-0">by Kfir Ezer</p>
          </motion.div>
        )}
        <button
          onClick={() => setCollapsed(!collapsed)}
          className="p-1.5 rounded-lg text-muted hover:text-text hover:bg-white/5 transition-colors flex-shrink-0"
        >
          <motion.div animate={{ rotate: collapsed ? 180 : 0 }}>
            <ChevronLeft size={18} />
          </motion.div>
        </button>
      </div>

      {/* New project button */}
      <div className="px-3 pb-2">
        <button
          onClick={onNewProject}
          className="w-full flex items-center justify-center gap-1.5 py-2 rounded-lg text-xs font-bold text-accent-light bg-accent/10 hover:bg-accent/15 border border-accent/20 transition-colors"
        >
          <Plus size={14} />
          {!collapsed && "New Project"}
        </button>
      </div>

      {/* Project tabs */}
      {!collapsed && (
        <div className="px-2 pb-2 space-y-0.5 overflow-y-auto flex-shrink min-h-0">
          {projects.map((p) => {
            const StatusIcon = STATUS_ICONS[p.status] || Clock;
            const statusColor = STATUS_COLORS[p.status] || "text-muted";
            const isActive = p.id === activePid;

            return (
              <motion.div
                key={p.id}
                initial={{ opacity: 0, x: -10 }}
                animate={{ opacity: 1, x: 0 }}
                className={`group flex items-center gap-2 px-2.5 py-1.5 rounded-lg cursor-pointer transition-colors text-xs ${
                  isActive
                    ? "bg-accent/10 border border-accent/20"
                    : "hover:bg-white/3 border border-transparent"
                }`}
                style={isActive ? { boxShadow: "0 0 12px rgba(139,92,246,0.15)" } : undefined}
                onClick={() => onSelectProject(p.id)}
                onDoubleClick={(e) => { e.stopPropagation(); setRenaming(p.id); }}
              >
                <StatusIcon size={12} className={`flex-shrink-0 ${statusColor}`} />
                {renaming === p.id ? (
                  <input
                    id={`rename-input-${p.id}`}
                    defaultValue={p.name}
                    autoFocus
                    className="flex-1 bg-transparent text-text text-xs outline-none border-b border-accent min-w-0"
                    onBlur={() => handleRenameSubmit(p.id)}
                    onKeyDown={(e) => { if (e.key === "Enter") handleRenameSubmit(p.id); if (e.key === "Escape") setRenaming(null); }}
                    onClick={(e) => e.stopPropagation()}
                  />
                ) : (
                  <>
                    <span className="flex-1 truncate font-medium">{p.name}</span>
                    <span className="text-[9px] text-muted flex-shrink-0 opacity-50">#{p.id}</span>
                  </>
                )}
                {p.git_url && p.status === "ready" && onPull && (
                  <button
                    className="opacity-0 group-hover:opacity-100 p-0.5 rounded hover:text-green hover:bg-green/10 transition-all flex-shrink-0 cursor-pointer"
                    title="Pull latest"
                    onClick={(e) => { e.stopPropagation(); onPull(p.id); }}
                  >
                    <RefreshCw size={11} />
                  </button>
                )}
                {p.git_url && p.status === "ready" && onShare && (
                  <button
                    className="opacity-0 group-hover:opacity-100 p-0.5 rounded hover:text-accent hover:bg-accent/10 transition-all flex-shrink-0 cursor-pointer"
                    title="Share project"
                    onClick={(e) => { e.stopPropagation(); handleShare(p.id); }}
                  >
                    <Share2 size={11} />
                  </button>
                )}
                <button
                  className="opacity-0 group-hover:opacity-100 p-0.5 rounded hover:text-red hover:bg-red/10 transition-all flex-shrink-0 cursor-pointer"
                  onClick={(e) => { e.stopPropagation(); onDelete(p.id); }}
                >
                  <X size={11} />
                </button>
              </motion.div>
            );
          })}
        </div>
      )}

      {/* Token expiry badge + renewal */}
      {!collapsed && tokenExpired && tokenExpired.size > 0 && (
        <div className="px-3 pb-2 space-y-1">
          {Array.from(tokenExpired).map((expid) => {
            const proj = projects.find((p) => p.id === expid);
            if (!proj) return null;
            return (
              <div key={expid} className="rounded-lg border border-orange/30 bg-orange/5 p-2">
                <div className="flex items-center gap-1.5 text-[10px] text-orange mb-1">
                  <AlertTriangle size={11} />
                  <span className="font-bold">Token expired: {proj.name}</span>
                </div>
                {renewingPid === expid ? (
                  <div className="flex gap-1">
                    <input
                      type="password" value={renewToken}
                      onChange={(e) => setRenewToken(e.target.value)}
                      placeholder="New BBDC-... token"
                      className="flex-1 px-1.5 py-1 rounded bg-white/5 border border-glass-border text-[10px] text-text outline-none focus:border-accent/40 font-mono"
                      onKeyDown={(e) => { if (e.key === "Enter") handleRenew(expid); }}
                      autoFocus
                    />
                    <button onClick={() => handleRenew(expid)}
                      className="px-2 py-1 rounded text-[10px] font-bold text-white"
                      style={{ background: "linear-gradient(135deg, #8b5cf6, #d946ef)" }}>
                      OK
                    </button>
                  </div>
                ) : (
                  <button onClick={() => { setRenewingPid(expid); setRenewToken(""); }}
                    className="text-[10px] text-accent-light hover:text-accent transition-colors flex items-center gap-1">
                    <KeyRound size={10} /> Update token
                  </button>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* Join project section */}
      {!collapsed && (
        <div className="px-3 pb-2">
          {showJoin ? (
            <div className="rounded-lg border border-glass-border bg-white/3 p-2 space-y-1.5">
              <div className="flex items-center gap-1.5 text-[10px] text-muted">
                <KeyRound size={11} />
                <span className="font-bold">Join shared project</span>
              </div>
              <input
                type="text" value={joinKey}
                onChange={(e) => setJoinKey(e.target.value)}
                placeholder="Share key (e.g. 2-aB3xK9mZ)"
                className="w-full px-2 py-1 rounded bg-white/5 border border-glass-border text-[11px] text-text outline-none focus:border-accent/40"
                onKeyDown={(e) => { if (e.key === "Enter") handleJoin(); }}
                autoFocus
              />
              <input
                type="password" value={joinToken}
                onChange={(e) => setJoinToken(e.target.value)}
                placeholder="Bitbucket token (for private repos)"
                className="w-full px-2 py-1 rounded bg-white/5 border border-glass-border text-[11px] text-text outline-none focus:border-accent/40 font-mono"
                onKeyDown={(e) => { if (e.key === "Enter") handleJoin(); }}
              />
              {joinError && <div className="text-[10px] text-red">{joinError}</div>}
              <div className="flex gap-1">
                <button onClick={() => setShowJoin(false)}
                  className="flex-1 px-2 py-1 rounded text-[10px] text-muted hover:text-text transition-colors">Cancel</button>
                <button onClick={handleJoin} disabled={joinLoading || !joinKey.trim()}
                  className="flex-1 px-2 py-1 rounded text-[10px] font-bold text-white disabled:opacity-40"
                  style={{ background: "linear-gradient(135deg, #8b5cf6, #d946ef)" }}>
                  {joinLoading ? <Loader2 size={10} className="animate-spin mx-auto" /> : "Join"}
                </button>
              </div>
            </div>
          ) : (
            <button onClick={() => setShowJoin(true)}
              className="w-full flex items-center justify-center gap-1.5 py-1.5 rounded-lg text-[11px] text-muted hover:text-accent-light hover:bg-accent/5 transition-colors">
              <KeyRound size={12} /> Join shared project
            </button>
          )}
        </div>
      )}

      {/* Nav buttons */}
      <div className="px-2 py-2 space-y-0.5 border-t border-glass-border">
        {NAV_ITEMS.map(({ panel, label, icon: Icon }) => (
          <motion.button
            key={panel}
            whileHover={{ scale: 1.02, backgroundColor: "rgba(255,255,255,0.06)" }}
            onClick={() => onSwitchPanel(panel)}
            className={`w-full flex items-center gap-2 px-2.5 py-1.5 rounded-lg text-xs font-medium transition-colors ${
              activePanel === panel
                ? "text-accent-light bg-accent/10"
                : "text-muted hover:text-text"
            }`}
          >
            <Icon size={14} className="flex-shrink-0" />
            {!collapsed && label}
          </motion.button>
        ))}
        {activeProjectHasGitUrl && CLONED_NAV_ITEMS.map(({ panel, label, icon: Icon }) => (
          <motion.button
            key={panel}
            whileHover={{ scale: 1.02, backgroundColor: "rgba(255,255,255,0.06)" }}
            onClick={() => onSwitchPanel(panel)}
            className={`w-full flex items-center gap-2 px-2.5 py-1.5 rounded-lg text-xs font-medium transition-colors ${
              activePanel === panel
                ? "text-accent-light bg-accent/10"
                : "text-muted hover:text-text"
            }`}
          >
            <Icon size={14} className="flex-shrink-0" />
            {!collapsed && label}
          </motion.button>
        ))}
      </div>

      {/* User bar */}
      <div className="px-3 py-2.5 border-t border-glass-border mt-auto">
        {auth.authenticated ? (
          <button onClick={auth.logout} className="w-full flex items-center gap-2 px-2 py-1.5 rounded-lg text-xs text-muted hover:text-text hover:bg-white/3 transition-colors">
            <LogOut size={14} />
            {!collapsed && <span className="truncate">{auth.user?.name || "User"}</span>}
          </button>
        ) : (
          <button onClick={auth.login} className="w-full flex items-center gap-2 px-2 py-1.5 rounded-lg text-xs text-accent-light hover:bg-accent/10 transition-colors">
            <LogIn size={14} />
            {!collapsed && "Login"}
          </button>
        )}
      </div>

      {/* Share modal */}
      {shareModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-6"
          style={{ background: "rgba(0,0,0,0.6)", backdropFilter: "blur(6px)" }}
          onClick={() => setShareModal(null)}>
          <div className="rounded-2xl p-5 max-w-sm w-full"
            style={{ background: "rgba(13,17,23,1)", border: "1px solid #21262d" }}
            onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between mb-3">
              <h3 className="text-sm font-bold gradient-text m-0">Share Project</h3>
              <button onClick={() => setShareModal(null)} className="text-muted hover:text-red"><X size={16} /></button>
            </div>
            <p className="text-[11px] text-muted mb-2">
              Share this key with your teammate. They can join via "Join shared project" in the sidebar.
            </p>
            <div className="flex gap-2">
              <input
                type="text" readOnly value={shareModal.key}
                className="flex-1 px-3 py-2 rounded-lg bg-white/5 border border-glass-border text-sm text-text font-mono outline-none"
                onClick={(e) => e.target.select()}
                autoFocus
              />
              <button
                onClick={() => { navigator.clipboard.writeText(shareModal.key); }}
                className="px-3 py-2 rounded-lg text-xs font-bold text-white"
                style={{ background: "linear-gradient(135deg, #8b5cf6, #d946ef)" }}>
                Copy
              </button>
            </div>
          </div>
        </div>
      )}
    </motion.aside>
  );
}