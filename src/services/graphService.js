import { requestJson } from "./apiClient";
import { endpoints } from "../config/endpoints";

export const graphService = {
  fetchGraphData: (pid) => requestJson(endpoints.projectGraphData(pid)),
};