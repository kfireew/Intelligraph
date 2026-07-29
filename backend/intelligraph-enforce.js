// Intelligraph Enforcement Plugin for opencode
// Blocks Grep/Glob/find/Get-ChildItem/Select-String and redirects to intelligraph MCP search.
// Auto-loaded from .opencode/plugins/ at startup (no config needed).
// See https://opencode.ai/docs/plugins

export const IntelligraphEnforcePlugin = async ({ directory }) => {
  return {
    "tool.execute.before": async (input, output) => {
      const blockedMsg = "BLOCKED: Use the intelligraph MCP 'search' tool with near= instead - it returns file paths with line ranges and confidence levels. Pass near= on every search after the first to get 2-3 targeted results instead of 16 broad ones.";

      // Block Grep and Glob tools (case-insensitive - opencode may use "Grep" or "grep")
      const tool = (input.tool || "").toLowerCase();
      if (tool === "grep") {
        throw new Error(blockedMsg);
      }
      if (tool === "glob") {
        throw new Error(blockedMsg);
      }

      // Block file-listing and search commands inside bash
      if (tool === "bash") {
        const cmd = (output.args.command || "").trim();
        // Allow git commands (git grep, git log, git status, etc.)
        if (cmd.startsWith("git ") || cmd === "git") return;

        const lower = cmd.toLowerCase();

        // Block grep/rg/find/ag (silver searcher)/ack
        if (/\b(grep|rg|ag|ack)\b/.test(lower)) {
          throw new Error("BLOCKED: grep/rg/ag/ack are disabled. Use the intelligraph MCP 'search' tool with near= instead.");
        }

        // Block find with path patterns (find . -name, find /path, find C:\)
        if (/\bfind\s+[./]|[a-z]:/i.test(lower)) {
          throw new Error("BLOCKED: find is disabled. Use the intelligraph MCP 'search' tool instead.");
        }

        // Block PowerShell Get-ChildItem with -Recurse (file enumeration = glob equivalent)
        if (/get-childitem.*-recurse/i.test(cmd) || /\bgci\b.*-recurse/i.test(lower)) {
          throw new Error("BLOCKED: Get-ChildItem -Recurse is disabled. Use the intelligraph MCP 'search' tool instead.");
        }

        // Block PowerShell Get-ChildItem with wildcards (file enumeration)
        if (/(get-childitem|\bgci\b).*\*/i.test(cmd)) {
          throw new Error("BLOCKED: Get-ChildItem with wildcards is disabled. Use the intelligraph MCP 'search' tool instead.");
        }

        // Block PowerShell Select-String (grep equivalent)
        if (/select-string/i.test(cmd) || /\bslstr\b/i.test(cmd)) {
          throw new Error("BLOCKED: Select-String is disabled. Use the intelligraph MCP 'search' tool instead.");
        }

        // Block dir /s (cmd recursive directory listing)
        if (/\bdir\b/i.test(cmd) && /\s\/s\b/i.test(cmd)) {
          throw new Error("BLOCKED: dir /s is disabled. Use the intelligraph MCP 'search' tool instead.");
        }

        // Block ls -R / ls -r (recursive listing)
        if (/\bls\b.*-R\b/.test(lower) || (/\bls\b.*-r\b/.test(lower) && !lower.includes("--reverse"))) {
          throw new Error("BLOCKED: ls -R is disabled. Use the intelligraph MCP 'search' tool instead.");
        }

        // Block where/where.exe (Windows file search, not SQL WHERE)
        if (/^where\b/i.test(cmd.trim()) || /\bwhere\.exe\b/i.test(cmd)) {
          throw new Error("BLOCKED: where is disabled. Use the intelligraph MCP 'search' tool instead.");
        }
      }
    },
  };
};
