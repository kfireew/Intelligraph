import { useState, useCallback } from "react";
import { getFromIDB, saveToIDB, deleteIDB } from "../utils/idb";
import { graphService } from "../services/graphService";

export function useUpload(pid) {
  const [graphifyStatus, setGraphifyStatus] = useState({ loaded: false, nodes: 0, message: "No file" });
  const [crgStatus, setCrgStatus] = useState({ loaded: false, size: "", message: "No file" });

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
    if (crg) setCrgStatus({ loaded: true, size: formatSize(crg.length), message: formatSize(crg.length) });
  }, [pid]);

  const clearUploads = useCallback(async () => {
    if (!pid) return;
    await deleteIDB(`graphify-${pid}`);
    await deleteIDB(`crg-${pid}`);
    setGraphifyStatus({ loaded: false, nodes: 0, message: "No file" });
    setCrgStatus({ loaded: false, size: "", message: "No file" });
  }, [pid]);

  return { graphifyStatus, crgStatus, uploadFile, refreshStatus, clearUploads };
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}