// Intelligraph Enforcement Plugin for opencode
// Previously blocked Grep/Glob/find/Select-String. Now a NO-OP — grep and glob
// are allowed as complementary tools alongside MCP search/impact/node.
// MCP provides structured graph navigation; grep provides literal text matching
// for type-pattern and string-literal searches. See agent.md for guidance.
export const IntelligraphEnforcePlugin = async ({ directory }) => {
  return {
    "tool.execute.before": async (input, output) => {
      // No-op: allow all tools through. Guidance is in agent.md.
    },
  };
};
