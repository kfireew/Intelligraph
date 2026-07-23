// Intelligraph Enforcement Plugin for opencode
// Blocks grep/glob/find tools and redirects to intelligraph MCP search.
// Requires the intelligraph MCP server to be configured in opencode.json.
//
// Install: place this file at .opencode/plugins/intelligraph-enforce.js
// Register in opencode.json: "plugin": [".opencode/plugins/intelligraph-enforce.js"]

export const IntelligraphEnforcePlugin = async ({ directory }) => {
  return {
    "tool.execute.before": async (input, output) => {
      const blockedMsg = "BLOCKED: Use the intelligraph MCP 'search' tool instead — it returns file paths with line ranges and confidence levels.";

      // Block the grep tool entirely
      if (input.tool === "grep") {
        throw new Error(blockedMsg);
      }

      // Block the glob tool entirely
      if (input.tool === "glob") {
        throw new Error(blockedMsg);
      }

      // Block grep/find/rg inside bash commands (but allow git commands)
      if (input.tool === "bash") {
        const cmd = (output.args.command || "").trim();
        // Allow git commands (git grep, git log, git status, etc.)
        if (cmd.startsWith("git ") || cmd === "git") return;

        // Check for standalone grep/find/rg usage
        if (/\b(grep|rg|find)\b/.test(cmd.toLowerCase())) {
          throw new Error("BLOCKED: grep/find/rg are disabled in bash. Use the intelligraph MCP 'search' tool instead.");
        }
      }
    },
  };
};
