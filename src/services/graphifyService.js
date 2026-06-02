import { requestJson } from "./apiClient";

export const graphifyService = {
  query: ({ prompt, pid }) =>
    requestJson(`/projects/${pid}/graphify-query`, {
      method: "POST",
      body: JSON.stringify({ prompt }),
    }),

  explain: ({ concept, pid }) =>
    requestJson(`/projects/${pid}/graphify-explain`, {
      method: "POST",
      body: JSON.stringify({ concept }),
    }),

  path: ({ a, b, pid }) =>
    requestJson(`/projects/${pid}/graphify-path`, {
      method: "POST",
      body: JSON.stringify({ a, b }),
    }),

  affected: ({ target, pid, depth = 2 }) =>
    requestJson(`/projects/${pid}/graphify-affected`, {
      method: "POST",
      body: JSON.stringify({ target, depth }),
    }),

  codeChunks: ({ filePaths, pid }) =>
    requestJson(`/projects/${pid}/code-chunks`, {
      method: "POST",
      body: JSON.stringify({ file_paths: filePaths }),
    }),

  fileContent: ({ path, start = 1, end = 50, pid }) =>
    requestJson(
      `/projects/${pid}/file-content?path=${encodeURIComponent(path)}&start=${start}&end=${end}`
    ),
};