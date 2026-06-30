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

  // LLM
  llmRelay: "/llm/relay",

  // Downloads
  downloadMCPServer: "/download/mcp-server",
  downloadGraphBuilder: "/download/graph-builder",
};