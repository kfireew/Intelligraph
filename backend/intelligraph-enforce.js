// Intelligraph Enforcement Plugin for opencode
// Blocks Grep/Glob/find/Get-ChildItem/Select-String and redirects to intelligraph MCP search.
// Auto-loaded from .opencode/plugins/ at startup (no config needed).
// See https://opencode.ai/docs/plugins

export const IntelligraphEnforcePlugin = async ({ directory }) => {
  return {
    "tool.execute.before": async (input, output) => {
      // Build a search hint from the tool's pattern/path arguments.
      // Converts glob patterns to space-joined terms for search().
      function searchHint(input) {
        let pattern = "";
        if (input.arguments) {
          pattern = input.arguments.pattern || input.arguments.path || input.arguments.query || "";
        }
        if (!pattern) return 'search("<your term>")';
        // Strip glob stars, backslashes, extensions -> space-separated terms
        const terms = pattern
          .replace(/[*?]/g, " ")
          .replace(/[/\\]/g, " ")
          .replace(/\.\w+$/g, "")
          .trim();
        const clean = terms.split(/\s+/).filter(t => t.length > 2).slice(0, 3).join(" ");
        return clean ? `search("${clean}")` : `search("<your term>")`;
      }

      // Suffix: tells the agent to search first, then anchor from results.
      const nearGuidance = "Use near= only with an exact symbol returned by that search.";

      // Block Grep and Glob tools (case-insensitive - opencode may use "Grep" or "grep")
      const tool = (input.tool || "").toLowerCase();
      if (tool === "grep") {
        throw new Error(`BLOCKED: grep is disabled. Try ${searchHint(input)} first. ${nearGuidance}`);
      }
      if (tool === "glob") {
        throw new Error(`BLOCKED: glob is disabled. Try ${searchHint(input)} first. ${nearGuidance}`);
      }

      // Block file-listing and search commands inside bash
      if (tool === "bash") {
        const cmd = (output.args.command || "").trim();
        // Allow git commands (git grep, git log, git status, etc.)
        if (cmd.startsWith("git ") || cmd === "git") return;

        const lower = cmd.toLowerCase();

        // Block grep/rg/find/ag (silver searcher)/ack
        if (/\b(grep|rg|ag|ack)\b/.test(lower)) {
          throw new Error(`BLOCKED: grep/rg/ag/ack are disabled. Try ${searchHint(input)} first. ${nearGuidance}`);
        }

        // Block find with path patterns (find . -name, find /path, find C:\)
        if (/\bfind\s+[./]|[a-z]:/i.test(lower)) {
          throw new Error(`BLOCKED: find is disabled. Try ${searchHint(input)} first. ${nearGuidance}`);
        }

        // Block PowerShell Get-ChildItem with -Recurse (file enumeration = glob equivalent)
        if (/get-childitem.*-recurse/i.test(cmd) || /\bgci\b.*-recurse/i.test(lower)) {
          throw new Error(`BLOCKED: Get-ChildItem -Recurse is disabled. Try ${searchHint(input)} first. ${nearGuidance}`);
        }

        // Block PowerShell Get-ChildItem with wildcards (file enumeration)
        if (/(get-childitem|\bgci\b).*\*/i.test(cmd)) {
          throw new Error(`BLOCKED: Get-ChildItem with wildcards is disabled. Try ${searchHint(input)} first. ${nearGuidance}`);
        }

        // Block PowerShell Select-String (grep equivalent)
        if (/select-string/i.test(cmd) || /\bslstr\b/i.test(cmd)) {
          throw new Error(`BLOCKED: Select-String is disabled. Try ${searchHint(input)} first. ${nearGuidance}`);
        }

        // Block dir /s (cmd recursive directory listing)
        if (/\bdir\b/i.test(cmd) && /\s\/s\b/i.test(cmd)) {
          throw new Error(`BLOCKED: dir /s is disabled. Try ${searchHint(input)} first. ${nearGuidance}`);
        }

        // Block ls -R / ls -r (recursive listing)
        if (/\bls\b.*-R\b/.test(lower) || (/\bls\b.*-r\b/.test(lower) && !lower.includes("--reverse"))) {
          throw new Error(`BLOCKED: ls -R is disabled. Try ${searchHint(input)} first. ${nearGuidance}`);
        }

        // Block where/where.exe (Windows file search, not SQL WHERE)
        if (/^where\b/i.test(cmd.trim()) || /\bwhere\.exe\b/i.test(cmd)) {
          throw new Error(`BLOCKED: where is disabled. Try ${searchHint(input)} first. ${nearGuidance}`);
        }
      }
    },
  };
};
