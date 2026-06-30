import { requestJson } from "./apiClient";
import { endpoints } from "../config/endpoints";

export const projectsService = {
  list: () => requestJson(endpoints.projects),
  getStatus: (pid) => requestJson(endpoints.projectStatus(pid)),
  clone: ({ gitUrl, name, type = "bitbucket", accessToken, authMode }) =>
    requestJson(endpoints.projectClone, {
      method: "POST",
      body: JSON.stringify({
        git_url: gitUrl,
        name,
        type,
        ...(accessToken ? {
          access_token: accessToken,
          auth_mode: authMode || "bitbucket_datacenter_bearer",
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
  pull: (pid) =>
    requestJson(endpoints.projectPull(pid), { method: "POST" }),
  getGraphData: (pid) => requestJson(endpoints.projectGraphData(pid)),
};