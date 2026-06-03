import { useState, useCallback } from "react";
import { motion } from "framer-motion";
import {
  MessageSquare, Upload, Settings, BookOpen,
  Plus, X, LogIn, LogOut, ChevronLeft,
  Loader2, CheckCircle2, AlertCircle, Clock,
} from "lucide-react";

const STATUS_ICONS = {
  ready: CheckCircle2,
  cloning: Loader2,
  building: Loader2,
  error: AlertCircle,
  pending_upload: Clock,
};

const STATUS_COLORS = {
  ready: "text-green",
  cloning: "text-cyan animate-spin",
  building: "text-cyan animate-spin",
  error: "text-red",
  pending_upload: "text-orange",
};

const NAV_ITEMS = [
  { panel: "chat", label: "Chat", icon: MessageSquare },
  { panel: "upload", label: "Upload", icon: Upload },
  { panel: "llm", label: "LLM", icon: Settings },
  { panel: "guide", label: "Guide", icon: BookOpen },
];

export function Sidebar({
  projects, activePid, activePanel,
  auth, onSelectProject, onNewProject,
  onSwitchPanel, onRename, onDelete,
}) {
  const [collapsed, setCollapsed] = useState(false);
  const [renaming, setRenaming] = useState(null);

  const handleRenameSubmit = useCallback((pid) => {
    const input = document.getElementById(`rename-input-${pid}`);
    if (input && input.value.trim()) {
      onRename(pid, input.value.trim());
    }
    setRenaming(null);
  }, [onRename]);

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
            <h2 className="text-base font-bold gradient-text m-0 leading-tight">Intelliscan</h2>
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
                  <span className="flex-1 truncate font-medium">{p.name}</span>
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
    </motion.aside>
  );
}