import { requestJson } from "./apiClient";
import { endpoints } from "../config/endpoints";

export const graphService = {
  fetchGraphData: (pid) => requestJson(endpoints.projectGraphData(pid)),

  fetchCrgDb: async (pid) => {
    const response = await fetch(endpoints.projectCrgDb(pid));
    if (!response.ok) throw new Error(`CRG DB fetch failed: ${response.status}`);
    return new Uint8Array(await response.arrayBuffer());
  },

  upload: async (pid, file, type) => {
    const formData = new FormData();
    formData.append("graph_file", file);
    formData.append("type", type);
    return requestJson(endpoints.projectUpload(pid), {
      method: "POST",
      body: formData,
    });
  },
};