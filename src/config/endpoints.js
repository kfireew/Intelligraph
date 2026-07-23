export const endpoints = {
  // Auth
  authMe: "/auth/me",

  // Projects
  projects: "/projects",
  projectClone: "/projects/clone",
  projectStatus: (pid) => `/projects/${pid}/status`,
  projectGraphData: (pid) => `/projects/${pid}/graph-data`,
  projectDetail: (pid) => `/projects/${pid}`,
  projectPull: (pid) => `/projects/${pid}/pull`,
  projectBranches: (pid) => `/projects/${pid}/branches`,
  projectToken: (pid) => `/projects/${pid}/token`,
  projectShare: (pid) => `/projects/${pid}/share`,
  shareJoin: "/share/join",

  // LLM
  llmRelay: "/llm/ask",

  // Downloads
  downloadMCPServer: "/download/mcp-server",
  downloadGraphBuilder: "/download/graph-builder",
  downloadAgent: "/download/agent",
  downloadEnforcePlugin: "/download/enforce-plugin",
  downloadClaudeHooks: "/download/claude-hooks"
};