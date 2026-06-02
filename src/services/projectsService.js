import { requestJson } from "./apiClient";
import { endpoints } from "../config/endpoints";

export const projectsService = {
  list: () => requestJson(endpoints.projects),

  getStatus: (pid) => requestJson(endpoints.projectStatus(pid)),

  clone: ({ gitUrl, name, type = "bitbucket" }) =>
    requestJson(endpoints.projectClone, {
      method: "POST",
      body: JSON.stringify({ git_url: gitUrl, name, type }),
    }),

  rename: (pid, name) =>
    requestJson(endpoints.projectDetail(pid), {
      method: "PATCH",
      body: JSON.stringify({ name }),
    }),

  delete: (pid) =>
    requestJson(endpoints.projectDetail(pid), { method: "DELETE" }),

  getGraphData: (pid) => requestJson(endpoints.projectGraphData(pid)),

  getCrgDb: async (pid) => {
    const response = await fetch(endpoints.projectCrgDb(pid));
    if (!response.ok) throw new Error(`CRG DB fetch failed: ${response.status}`);
    return new Uint8Array(await response.arrayBuffer());
  },

  uploadData: async (pid, file, type) => {
    const formData = new FormData();
    formData.append("graph_file", file);
    formData.append("type", type);
    return requestJson(endpoints.projectUpload(pid), {
      method: "POST",
      body: formData,
    });
  },

  getMCPToken: (pid) => requestJson(endpoints.projectMCPToken(pid)),
};