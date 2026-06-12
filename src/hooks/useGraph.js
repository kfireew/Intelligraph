import { useState, useEffect, useCallback } from "react";
import { graphService } from "../services/graphService";

export function useGraph(pid) {
  const [graphData, setGraphData] = useState(null);
  const [status, setStatus] = useState("idle");
  const [selectedNode, setSelectedNode] = useState(null);

  const loadGraph = useCallback(async (projectId) => {
    if (!projectId) return;
    setStatus("loading");
    try {
      const gf = await graphService.fetchGraphData(projectId);
      if (gf) {
        setGraphData(gf);
        setStatus(gf.nodes !== undefined ? "ready" : "empty");
      }
    } catch (e) {
      console.warn("loadGraph failed:", e);
      setStatus("error");
    }
  }, []);

  useEffect(() => { if (pid) loadGraph(pid); }, [pid, loadGraph]);

  const selectNode = useCallback((node) => setSelectedNode(node), []);
  const clearGraph = useCallback(() => {
    setGraphData(null); setStatus("idle"); setSelectedNode(null);
  }, []);

  return { graphData, status, selectedNode, loadGraph, selectNode, clearGraph };
}