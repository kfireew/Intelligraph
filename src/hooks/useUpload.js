import { useState, useEffect, useCallback } from "react";
import { getFromIDB, saveToIDB, deleteIDB } from "../utils/idb";
import { graphService } from "../services/graphService";

export function useUpload(pid) {
  const [graphifyStatus, setGraphifyStatus] = useState({ loaded: false, nodes: 0, message: "No file" });
  const [crgStatus, setCrgStatus] = useState({ loaded: false, size: "", message: "No file" });
  const [htmlStatus, setHtmlStatus] = useState({ loaded: false, message: "No file" });

  useEffect(() => {
    (async () => {
      if (!pid) return;
      const gf = await getFromIDB(`graphify-${pid}`);
      if (gf) setGraphifyStatus({ loaded: true, nodes: gf.nodes?.length || 0, message: `${gf.nodes?.length || 0} nodes` });
      const crg = await getFromIDB(`crg-${pid}`);
      if (crg) setCrgStatus({ loaded: true, size: crg.path ? "Available" : "", message: crg.nodes ? crg.nodes + " entities" : "graph.db" });
      const html = await getFromIDB(`html-${pid}`);
      if (html) setHtmlStatus({ loaded: true, message: html.fileName || "graph.html" });
    })();
  }, [pid]);

  const uploadFile = useCallback(async (file, type) => {
    let projectId = pid;
    if (!projectId) {
      const r = await fetch("/projects/clone", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ git_url: "", name: file.name, type: "upload" }),
      });
      const p = await r.json();
      projectId = p.id;
    }
    if (type === "graphify") {
      const text = await file.text();
      const json = JSON.parse(text);
      await saveToIDB(`graphify-${projectId}`, json);
      setGraphifyStatus({ loaded: true, nodes: json.nodes?.length || 0, message: `${json.nodes?.length || 0} nodes` });
    } else if (type === "html") {
      await saveToIDB(`html-${projectId}`, { fileName: file.name });
      setHtmlStatus({ loaded: true, message: file.name || "graph.html" });
    } else {
      const buf = await file.arrayBuffer();
      await saveToIDB(`crg-${projectId}`, new Uint8Array(buf));
      setCrgStatus({ loaded: true, size: formatSize(file.size), message: formatSize(file.size) });
    }
    try {
      await graphService.upload(projectId, file, type);
    } catch (e) {
      console.error("Server upload failed:", e);
    }
    return projectId;
  }, [pid]);

  const refreshStatus = useCallback(async (projectId) => {
    const pid2 = projectId || pid;
    if (!pid2) return;
    const gf = await getFromIDB(`graphify-${pid2}`);
    if (gf) setGraphifyStatus({ loaded: true, nodes: gf.nodes?.length || 0, message: `${gf.nodes?.length || 0} nodes` });
    const crg = await getFromIDB(`crg-${pid2}`);
    if (crg2) setCrgStatus({ loaded: true, size: crg2.path ? "Available" : "", message: crg2.nodes ? crg2.nodes + " entities" : "graph.db" });
    const html = await getFromIDB(`html-${pid2}`);
    if (html) setHtmlStatus({ loaded: true, message: html.fileName || "graph.html" });
  }, [pid]);

  const clearUploads = useCallback(async () => {
    setGraphifyStatus({ loaded: false, nodes: 0, message: "No file" });
    setCrgStatus({ loaded: false, size: "", message: "No file" });
    setHtmlStatus({ loaded: false, message: "No file" });
    if (pid) {
      deleteIDB(`graphify-${pid}`).catch(() => {});
      deleteIDB(`crg-${pid}`).catch(() => {});
      deleteIDB(`html-${pid}`).catch(() => {});
    }
  }, [pid]);

  return { graphifyStatus, crgStatus, htmlStatus, uploadFile, refreshStatus, clearUploads };
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
