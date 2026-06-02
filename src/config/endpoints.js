export const endpoints = {
  // Auth
  authMe: "/auth/me",

  // Projects
  projects: "/projects",
  projectClone: "/projects/clone",
  projectStatus: (pid) => `/projects/${pid}/status`,
  projectGraphData: (pid) => `/projects/${pid}/graph-data`,
  projectCrgDb: (pid) => `/projects/${pid}/crg-db`,
  projectUpload: (pid) => `/projects/${pid}/upload-data`,
  projectMCPToken: (pid) => `/projects/${pid}/mcp-token`,
  projectDetail: (pid) => `/projects/${pid}`,

  // LLM
  llmRelay: "/llm/relay",
  llmRelayStream: "/llm/relay/stream",
  llmClassify: "/llm/classify",

  // graphify + code
  graphifyQuery: (pid) => `/projects/${pid}/graphify-query`,
  graphifyExplain: (pid) => `/projects/${pid}/graphify-explain`,
  graphifyPath: (pid) => `/projects/${pid}/graphify-path`,
  graphifyAffected: (pid) => `/projects/${pid}/graphify-affected`,
  codeChunks: (pid) => `/projects/${pid}/code-chunks`,
  fileContent: (pid) => `/projects/${pid}/file-content`,

  // MCP
  mcpUpload: "/mcp/upload",
  mcpClear: "/mcp/clear",

  // Downloads
  downloadMCPServer: "/download/mcp-server",
  downloadGraphBuilder: "/download/graph-builder",
  downloadMCPConfig: "/download/mcp-config",
};