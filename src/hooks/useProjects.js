import { useState, useCallback, useRef, useEffect } from "react";
import { saveToIDB } from "../utils/idb";
import { projectsService } from "../services/projectsService";

export function useProjects() {
  const [projects, setProjects] = useState([]);
  const [activePid, setActivePid] = useState(null);
  const [loading, setLoading] = useState(false);
  const [tokenExpired, setTokenExpired] = useState(new Set());
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
      setActivePid(p.id);
      await fetchProjects();
      // Always start polling — clone may return "ready" but build might still be finishing
      startPolling(p.id);
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

  const pullProject = useCallback(async (pid, branch) => {
    setProjects((prev) => prev.map((p) => p.id === pid ? { ...p, status: "pulling" } : p));
    try {
      await projectsService.pull(pid, branch);
      startPolling(pid);
    } catch (e) {
      if (e.message && e.message.includes("token_expired_or_invalid")) {
        setTokenExpired((prev) => new Set([...prev, pid]));
      }
    } finally {
      await fetchProjects();
    }
  }, [fetchProjects, startPolling]);

  const fetchBranches = useCallback(async (pid) => {
    try {
      return await projectsService.branches(pid);
    } catch (e) {
      return null;
    }
  }, []);

  const updateToken = useCallback(async (pid, token) => {
    try {
      await projectsService.updateToken(pid, token);
      setTokenExpired((prev) => { const n = new Set(prev); n.delete(pid); return n; });
      return true;
    } catch (e) {
      return false;
    }
  }, []);

  const shareProject = useCallback(async (pid) => {
    try {
      const result = await projectsService.share(pid);
      return result.share_key || null;
    } catch (e) {
      return null;
    }
  }, []);

  const joinProject = useCallback(async (shareKey, bitbucketToken) => {
    try {
      const result = await projectsService.join(shareKey, bitbucketToken);
      if (result.project_id) {
        setActivePid(result.project_id);
        await fetchProjects();
      }
      return result;
    } catch (e) {
      throw e;
    }
  }, [fetchProjects]);

  const markTokenExpired = useCallback((pid) => {
    setTokenExpired((prev) => new Set([...prev, pid]));
  }, []);

  const activeProject = projects.find((p) => p.id === activePid) || null;

  return {
    projects, activePid, activeProject, loading, tokenExpired,
    fetchProjects, selectProject, cloneProject, renameProject, deleteProject, pullProject,
    fetchBranches, updateToken, shareProject, joinProject, markTokenExpired,
  };
}
