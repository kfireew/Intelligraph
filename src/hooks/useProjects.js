import { useState, useCallback } from "react";
import { saveToIDB } from "../utils/idb";
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

  const cloneProject = useCallback(async ({ gitUrl, name, accessToken, useLinkedCredentials, authProvider }) => {
    try {
      const p = await projectsService.clone({ gitUrl, name, accessToken, useLinkedCredentials, authProvider });
      if (p.graphify_data && p.crg_db_path) {
        p.graphify_data.has_crg_db = true;
      }
      if (p.graphify_data) await saveToIDB(`graphify-${p.id}`, p.graphify_data);
      if (p.crg_db_path) await saveToIDB(`crg-${p.id}`, { path: p.crg_db_path, has_crg_db: true, nodes: p.crg_nodes });
      if (p.graph_html_path) await saveToIDB(`html-${p.id}`, { path: p.graph_html_path, fileName: "graph.html" });
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
