import { useState, useEffect, useCallback, useRef } from "react";
import initSqlJs from "sql.js";
import { graphService } from "../services/graphService";
import { saveToIDB, getFromIDB, deleteIDB } from "../utils/idb";
import * as gq from "../utils/graphQueries";

let SQL = null;

async function getSql() {
  if (!SQL) {
    SQL = await initSqlJs({ locateFile: (f) => `https://sql.js.org/dist/${f}` });
  }
  return SQL;
}

export function useGraph(pid) {
  const [graphData, setGraphData] = useState(null);
  const [crgDb, setCrgDb] = useState(null);
  const [status, setStatus] = useState("idle");
  const [selectedNode, setSelectedNode] = useState(null);
  const dbRef = useRef(null);

  const loadGraph = useCallback(async (projectId) => {
    if (!projectId) return;
    setStatus("loading");
    try {
      // Try IndexedDB first
      let gf = await getFromIDB(`graphify-${projectId}`);
      let crgBytes = await getFromIDB(`crg-${projectId}`);
      // Fall back to server
      if (!gf) {
        const data = await graphService.fetchGraphData(projectId);
        gf = data.graphify || data;
        await saveToIDB(`graphify-${projectId}`, gf);
      }
      if (!crgBytes && gf?.has_crg_db) {
        crgBytes = await graphService.fetchCrgDb(projectId);
        if (crgBytes) await saveToIDB(`crg-${projectId}`, crgBytes);
      }
      setGraphData(gf);
      if (crgBytes) {
        const S = await getSql();
        const oldDb = dbRef.current;
        if (oldDb) {
          try { oldDb.close(); } catch {}
        }
        const db = new S.Database(crgBytes);
        dbRef.current = db;
        setCrgDb(db);
      }
      setStatus("ready");
    } catch (e) {
      console.error("loadGraph:", e);
      setStatus("error");
    }
  }, []);

  useEffect(() => {
    if (pid) loadGraph(pid);
  }, [pid, loadGraph]);

  const searchNodes = useCallback((q, limit) => {
    return gq.searchNodes(dbRef.current, graphData, q, limit);
  }, [graphData]);

  const callers = useCallback((target, limit) => {
    return gq.getCallers(dbRef.current, graphData, target, limit);
  }, [graphData]);

  const callees = useCallback((target, limit) => {
    return gq.getCallees(dbRef.current, graphData, target, limit);
  }, [graphData]);

  const impact = useCallback((target) => {
    return gq.getImpact(dbRef.current, graphData, target);
  }, [graphData]);

  const architecture = useCallback(() => {
    return gq.getArchitecture(dbRef.current, graphData);
  }, [graphData]);

  const tests = useCallback((target) => {
    return gq.getTests(dbRef.current, graphData, target);
  }, [graphData]);

  const selectNode = useCallback((node) => {
    setSelectedNode(node);
  }, []);

  const clearGraph = useCallback(() => {
    if (dbRef.current) {
      try { dbRef.current.close(); } catch {}
      dbRef.current = null;
    }
    setGraphData(null);
    setCrgDb(null);
    setSelectedNode(null);
    setStatus("idle");
  }, []);

  return {
    graphData, crgDb, status, selectedNode,
    loadGraph, clearGraph, selectNode,
    searchNodes, callers, callees, impact, architecture, tests,
  };
}