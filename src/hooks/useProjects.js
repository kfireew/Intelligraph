import { useState, useCallback } from "react";
import { projectsService } from "../services/projectsService";

export function useProjects() {
  const [projects, setProjects] = useState([]);
  const [activePid, setActivePid] = useState(null);
  const [loading, setLoading] = useState(false);

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

  const selectProject = useCallback((pid) => {
    setActivePid(pid);
  }, []);

  const cloneProject = useCallback(async ({ gitUrl, name }) => {
    try {
      const p = await projectsService.clone({ gitUrl, name });
      setActivePid(p.id);
      await fetchProjects();
      return p;
    } catch (e) {
      throw e;
    }
  }, [fetchProjects]);

  const renameProject = useCallback(async (pid, name) => {
    await projectsService.rename(pid, name);
    await fetchProjects();
  }, [fetchProjects]);

  const deleteProject = useCallback(async (pid) => {
    await projectsService.delete(pid);
    if (activePid === pid) setActivePid(null);
    await fetchProjects();
  }, [activePid, fetchProjects]);

  const activeProject = projects.find((p) => p.id === activePid) || null;

  return {
    projects, activePid, activeProject, loading,
    fetchProjects, selectProject, cloneProject, renameProject, deleteProject,
  };
}