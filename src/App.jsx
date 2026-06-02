import { useState, useEffect, useCallback } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { AppShell } from "./components/AppShell";
import { ParticleBackground } from "./components/ParticleBackground";
import { Sidebar } from "./components/Sidebar";
import { ChatPanel } from "./components/ChatPanel";
import { GraphPanel } from "./components/GraphPanel";
import { UploadPanel } from "./components/UploadPanel";
import { LLMSettings } from "./components/LLMSettings";
import { GuidePanel } from "./components/GuidePanel";
import { CloneModal } from "./components/CloneModal";
import { LoadingOverlay } from "./components/LoadingOverlay";
import { useAuth } from "./hooks/useAuth";
import { useProjects } from "./hooks/useProjects";
import { useGraph } from "./hooks/useGraph";
import { useChat } from "./hooks/useChat";
import { useLLM } from "./hooks/useLLM";
import { useUpload } from "./hooks/useUpload";
import { mcpService } from "./services/mcpService";

export default function App() {
  const auth = useAuth();
  const projects = useProjects();
  const graph = useGraph(projects.activePid);
  const llm = useLLM();
  const upload = useUpload(projects.activePid);

  const chat = useChat({
    graphData: graph.graphData,
    crgDbRef: graph.crgDb,
    searchNodes: graph.searchNodes,
    callers: graph.callers,
    callees: graph.callees,
    impact: graph.impact,
    architecture: graph.architecture,
    tests: graph.tests,
    activePid: projects.activePid,
    llmUrl: llm.llmUrl,
    llmToken: llm.llmToken,
    model: llm.model,
  });

  const [activePanel, setActivePanel] = useState("chat");
  const [showCloneModal, setShowCloneModal] = useState(false);
  const [cloneLoading, setCloneLoading] = useState(false);
  const [mcpStatus, setMcpStatus] = useState("");

  // Load projects on mount
  useEffect(() => {
    projects.fetchProjects();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Refresh upload status when project changes
  useEffect(() => {
    if (projects.activePid) upload.refreshStatus(projects.activePid);
  }, [projects.activePid]); // eslint-disable-line react-hooks/exhaustive-deps

  // Refresh upload status when graph loads (after clone or upload completes)
  useEffect(() => {
    if (graph.status === "ready" && projects.activePid) {
      upload.refreshStatus(projects.activePid);
    }
  }, [graph.status, projects.activePid]); // eslint-disable-line react-hooks/exhaustive-deps

  // Select first project by default
  useEffect(() => {
    if (projects.projects.length && projects.activePid === null) {
      projects.selectProject(projects.projects[0].id);
    }
  }, [projects.projects, projects.activePid, projects.selectProject]);

  const handleClone = useCallback(async ({ gitUrl, name }) => {
    setCloneLoading(true);
    try {
      await projects.cloneProject({ gitUrl, name });
    } finally {
      setCloneLoading(false);
    }
  }, [projects]);

  const handleMCPUpload = useCallback(async (file, type) => {
    try {
      const result = await mcpService.upload(file, type);
      setMcpStatus(`Token: ${result.token}`);
    } catch (e) {
      setMcpStatus(`Error: ${e.message}`);
    }
  }, []);

  const handleLLMSave = useCallback((url, token) => {
    llm.save(url, token);
  }, [llm]);

  const handleLLMFetchModels = useCallback(() => {
    llm.fetchModels(llm.llmUrl, llm.llmToken);
  }, [llm]);

  const handleLLMTest = useCallback(() => {
    llm.test();
  }, [llm]);

  // Loading states
  const isLoading = graph.status === "loading" || projects.loading;

  return (
    <AppShell>
      <ParticleBackground thinking={chat.status === "classifying" || chat.status === "thinking" || chat.status === "answering"} />

      <Sidebar
        projects={projects.projects}
        activePid={projects.activePid}
        activePanel={activePanel}
        auth={auth}
        onSelectProject={projects.selectProject}
        onNewProject={() => setShowCloneModal(true)}
        onSwitchPanel={setActivePanel}
        onRename={projects.renameProject}
        onDelete={projects.deleteProject}
      />

      {/* Main content area */}
      <div className="flex flex-1 min-w-0 overflow-hidden">
        <AnimatePresence mode="wait">
          {activePanel === "chat" && (
            <motion.div
              key="chat"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.15 }}
              className="flex flex-1 min-w-0"
            >
              <ChatPanel
                messages={chat.messages}
                status={chat.status}
                streamingContent={chat.streamingContent}
                onSend={chat.sendMessage}
                activeProject={projects.activeProject}
                graphData={graph.graphData}
                llmConfigured={!!(llm.llmUrl && llm.llmToken)}
                onGoToLLM={() => setActivePanel("llm")}
              />
            </motion.div>
          )}

          {activePanel === "upload" && (
            <motion.div
              key="upload"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.15 }}
              className="flex flex-1 min-w-0"
            >
              <UploadPanel
                graphifyStatus={upload.graphifyStatus}
                crgStatus={upload.crgStatus}
                htmlStatus={upload.htmlStatus}
                onUpload={upload.uploadFile}
                onClear={upload.clearUploads}
                onRefresh={() => projects.activePid && upload.refreshStatus(projects.activePid)}
                onReloadGraph={(uploadedPid) => {
                  const targetPid = uploadedPid || projects.activePid;
                  if (!targetPid) return;
                  if (!projects.activePid) projects.selectProject(targetPid);
                  graph.loadGraph(targetPid);
                }}
              />
            </motion.div>
          )}

          {activePanel === "llm" && (
            <motion.div
              key="llm"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.15 }}
              className="flex flex-1 min-w-0"
            >
              <LLMSettings
                llmUrl={llm.llmUrl}
                llmToken={llm.llmToken}
                model={llm.model}
                models={llm.models}
                modelsLoading={llm.modelsLoading}
                testResult={llm.testResult}
                onSave={handleLLMSave}
                onFetchModels={handleLLMFetchModels}
                onSelectModel={llm.selectModel}
                onTest={handleLLMTest}
              />
            </motion.div>
          )}

          {activePanel === "guide" && (
            <motion.div
              key="guide"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.15 }}
              className="flex flex-1 min-w-0"
            >
              <GuidePanel
                onMCPUpload={handleMCPUpload}
                mcpStatus={mcpStatus}
                graphifyStatus={upload.graphifyStatus}
                crgStatus={upload.crgStatus}
              />
            </motion.div>
          )}
        </AnimatePresence>

        {/* Graph panel — always visible on the right */}
        <div className="w-[42%] flex-shrink-0 border-l border-glass-border hidden lg:flex">
          <GraphPanel
            activePid={projects.activePid}
            crgDb={graph.crgDb}
            selectedNode={graph.selectedNode}
            onSelectNode={graph.selectNode}
          />
        </div>
      </div>

      {/* Clone modal */}
      {showCloneModal && (
        <CloneModal
          onClone={handleClone}
          onClose={() => setShowCloneModal(false)}
          loading={cloneLoading}
          onUploadComplete={(pid) => {
            projects.fetchProjects();
            projects.selectProject(pid);
          }}
        />
      )}

      {/* Loading overlay */}
      {isLoading && <LoadingOverlay title="Loading project..." detail="Fetching graph data" />}
    </AppShell>
  );
}