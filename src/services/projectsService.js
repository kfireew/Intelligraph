import { requestJson } from "./apiClient";
import { endpoints } from "../config/endpoints";

export const projectsService = {
  list: () => requestJson(endpoints.projects),
  getStatus: (pid) => requestJson(endpoints.projectStatus(pid)),
  clone: ({ gitUrl, name, type = "bitbucket", accessToken, useLinkedCredentials, authProvider }) =>
    requestJson(endpoints.projectClone, {
      method: "POST",
      body: JSON.stringify({
        git_url: gitUrl,
        name,
        type,
        ...(accessToken ? {
          access_token: accessToken,
          use_linked_credentials: useLinkedCredentials ?? true,
          auth_provider: authProvider || "bitbucket_datacenter",
        } : {}),
      }),
    }),
  rename: (pid, name) =>
    requestJson(endpoints.projectDetail(pid), {
      method: "PATCH",
      body: JSON.stringify({ name }),
    }),
  delete: (pid) =>
    requestJson(endpoints.projectDetail(pid), { method: "DELETE" }),
  getGraphData: (pid) => requestJson(endpoints.projectGraphData(pid)),
};