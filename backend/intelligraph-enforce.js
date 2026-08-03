// Intelligraph Enforcement Plugin for opencode
// Blocks Grep/Glob/find/Get-ChildItem/Select-String and redirects to intelligraph MCP tools.
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

      // Positive-action guidance: tells the agent what TO use instead.
      const guidance = [
        `For codebase-wide search, use ${searchHint(input)}.`,
        "For searching within a specific file, use search_in_file(path, \"query\").",
        "For external package symbols, use package(\"@scope/name\").",
        "After search returns results, pass a returned symbol as near= to narrow further.",
      ].join(" ");

      // Block Grep and Glob tools (case-insensitive - opencode may use "Grep" or "grep")
      const tool = (input.tool || "").toLowerCase();
      if (tool === "grep") {
        throw new Error(`BLOCKED: grep is disabled. ${guidance}`);
      }
      if (tool === "glob") {
        throw new Error(`BLOCKED: glob is disabled. ${guidance}`);
      }

      // Block file-listing and search commands inside bash
      if (tool === "bash") {
        const cmd = (output.args.command || "").trim();
        // Allow git commands (git grep, git log, git status, etc.)
        if (cmd.startsWith("git ") || cmd === "git") return;

        const lower = cmd.toLowerCase();

        // Block grep/rg/find/ag (silver searcher)/ack
        if (/\b(grep|rg|ag|ack)\b/.test(lower)) {
          throw new Error(`BLOCKED: grep/rg/ag/ack are disabled. ${guidance}`);
        }

        // Block find with path patterns (find . -name, find /path, find C:\)
        if (/\bfind\s+[./]|[a-z]:/i.test(lower)) {
          throw new Error(`BLOCKED: find is disabled. ${guidance}`);
        }

        // Block PowerShell Get-ChildItem with -Recurse (file enumeration = glob equivalent)
        if (/get-childitem.*-recurse/i.test(cmd) || /\bgci\b.*-recurse/i.test(lower)) {
          throw new Error(`BLOCKED: Get-ChildItem -Recurse is disabled. ${guidance}`);
        }

        // Block PowerShell Get-ChildItem with wildcards (file enumeration)
        if (/(get-childitem|\bgci\b).*\*/i.test(cmd)) {
          throw new Error(`BLOCKED: Get-ChildItem with wildcards is disabled. ${guidance}`);
        }

        // Block PowerShell Select-String (grep equivalent)
        if (/select-string/i.test(cmd) || /\bslstr\b/i.test(cmd)) {
          throw new Error(`BLOCKED: Select-String is disabled. ${guidance}`);
        }

        // Block dir /s (cmd recursive directory listing)
        if (/\bdir\b/i.test(cmd) && /\s\/s\b/i.test(cmd)) {
          throw new Error(`BLOCKED: dir /s is disabled. ${guidance}`);
        }

        // Block ls -R / ls -r (recursive listing)
        if (/\bls\b.*-R\b/.test(lower) || (/\bls\b.*-r\b/.test(lower) && !lower.includes("--reverse"))) {
          throw new Error(`BLOCKED: ls -R is disabled. ${guidance}`);
        }

        // Block where/where.exe (Windows file search, not SQL WHERE)
        if (/^where\b/i.test(cmd.trim()) || /\bwhere\.exe\b/i.test(cmd)) {
          throw new Error(`BLOCKED: where is disabled. ${guidance}`);
        }
      }
    },
  };
};
