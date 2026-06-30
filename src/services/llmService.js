import { requestJson } from "./apiClient";
import { endpoints } from "../config/endpoints";

export const llmService = {
  relay: ({ url, token, payload, projectId }) =>
    requestJson(endpoints.llmRelay, {
      method: "POST",
      body: JSON.stringify({ url, token, payload, project_id: projectId }),
    }),
};
