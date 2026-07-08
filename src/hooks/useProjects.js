import { useState, useCallback, useRef, useEffect } from "react";
import { saveToIDB } from "../utils/idb";
import { projectsService } from "../services/projectsService";

export function useProjects() {
  const [projects, setProjects] = useState([]);
  const [activePid, setActivePid] = useState(null);
  const [loading, setLoading] = useState(false);
  const pollRef = useRef(null);
  const pollingPids = useRef(new Set());

  const fetchProjects = useCallback(async () => {
    try {
      setLoading(true);
      const data = await projectsService.list();
      setProjects(data || []);
    } catch (e) {
      console.error("fetchProjects:", e);
    } finally {
      setLoading(false);
    }
  }, []);

  const stopPolling = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    pollingPids.current.clear();
  }, []);

  const startPolling = useCallback((pid) => {
    if (pollingPids.current.has(pid)) return;
    pollingPids.current.add(pid);
    if (!pollRef.current) {
      pollRef.current = setInterval(async () => {
        const pids = [...pollingPids.current];
        for (const p of pids) {
          try {
            const st = await projectsService.getStatus(p);
            setProjects((prev) => {
              const updated = prev.map((proj) =>
                proj.id === p ? { ...proj, status: st.status, nodes: st.nodes || 0, edges: st.edges || 0 } : proj
              );
              return updated;
            });
            if (st.status === "ready" || st.status === "error") {
              pollingPids.current.delete(p);
            }
          } catch (e) {
            console.error("status poll error:", e);
            pollingPids.current.delete(p);
          }
        }
        if (pollingPids.current.size === 0) {
          stopPolling();
          fetchProjects();
        }
      }, 2000);
    }
  }, [fetchProjects, stopPolling]);

  useEffect(() => () => stopPolling(), [stopPolling]);

  const selectProject = useCallback((pid) => {
    setActivePid(pid);
  }, []);

  const cloneProject = useCallback(async ({ gitUrl, name, accessToken, useLinkedCredentials, authProvider }) => {
    try {
      const p = await projectsService.clone({ gitUrl, name, accessToken, useLinkedCredentials, authProvider });
      // Clone returns immediately with "queued" status — start polling
      setActivePid(p.id);
      await fetchProjects();
      if (p.status === "queued" || p.status === "building") {
        startPolling(p.id);
      }
      return p;
    } catch (e) {
      throw e;
    }
  }, [fetchProjects, startPolling]);

  const renameProject = useCallback(async (pid, name) => {
    await projectsService.rename(pid, name);
    await fetchProjects();
  }, [fetchProjects]);

  const deleteProject = useCallback(async (pid) => {
    await projectsService.delete(pid);
    if (activePid === pid) setActivePid(null);
    pollingPids.current.delete(pid);
    await fetchProjects();
  }, [activePid, fetchProjects]);

  const pullProject = useCallback(async (pid) => {
    setProjects((prev) => prev.map((p) => p.id === pid ? { ...p, status: "pulling" } : p));
    try {
      await projectsService.pull(pid);
      startPolling(pid);
    } finally {
      await fetchProjects();
    }
  }, [fetchProjects, startPolling]);

  const activeProject = projects.find((p) => p.id === activePid) || null;

  return {
    projects, activePid, activeProject, loading,
    fetchProjects, selectProject, cloneProject, renameProject, deleteProject, pullProject,
  };
}
