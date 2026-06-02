import { requestJson } from "./apiClient";
import { endpoints } from "../config/endpoints";

export const mcpService = {
  upload: async (file, type) => {
    const formData = new FormData();
    formData.append("graph_file", file);
    formData.append("type", type);
    return requestJson(endpoints.mcpUpload, {
      method: "POST",
      body: formData,
    });
  },

  clear: () => requestJson(endpoints.mcpClear, { method: "POST" }),
};