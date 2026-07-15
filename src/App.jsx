import { useState, useEffect, useCallback } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { AppShell } from "./components/AppShell";
import { ParticleBackground } from "./components/ParticleBackground";
import { Sidebar } from "./components/Sidebar";
import { ChatPanel } from "./components/ChatPanel";
import { GraphPanel } from "./components/GraphPanel";
import { StarTrail } from "./components/StarTrail";
import { LLMSettings } from "./components/LLMSettings";
import { GuidePanel } from "./components/GuidePanel";
import { BranchPanel } from "./components/BranchPanel";
import { TuningPanel, useTuning } from "./components/TuningPanel";
import { CloneModal } from "./components/CloneModal";
import { LoadingOverlay } from "./components/LoadingOverlay";
import { useAuth } from "./hooks/useAuth";
import { useProjects } from "./hooks/useProjects";
import { useGraph } from "./hooks/useGraph";
import { useChat } from "./hooks/useChat";
import { useLLM } from "./hooks/useLLM";

export default function App() {
  const auth = useAuth();
  const projects = useProjects();
  const activeProject = projects.projects.find(p => p.id === projects.activePid);
  const graph = useGraph(projects.activePid, activeProject?.status);
  const llm = useLLM();
  const tuning = useTuning();
  const [matchedNodes, setMatchedNodes] = useState([]);
  const [orbHovered, setOrbHovered] = useState(false);
  const [answerComplete, setAnswerComplete] = useState(0);

  const chat = useChat({
    activePid: projects.activePid, llmUrl: llm.llmUrl,
    llmToken: llm.llmToken, model: llm.model,
    onMatchedNodes: setMatchedNodes,
    onAnswerComplete: () => setAnswerComplete((c) => c + 1),
    onTokenExpired: projects.markTokenExpired,
  });

  const [activePanel, setActivePanel] = useState("chat");
  const [showCloneModal, setShowCloneModal] = useState(false);
  const [cloneLoading, setCloneLoading] = useState(false);

  useEffect(() => { projects.fetchProjects(); }, []);
  useEffect(() => {
    if (projects.projects?.length && !projects.activePid)
      projects.selectProject(projects.projects[0].id);
  }, [projects.projects, projects.activePid, projects.selectProject]);

  const handleClone = useCallback(async (cloneArgs) => {
    const { gitUrl, name, ...extra } = cloneArgs;
    if (!gitUrl) return;
    setCloneLoading(true);
    try {
      const newProject = await projects.cloneProject({ gitUrl, name, ...extra });
      if (newProject?.id) {
        projects.selectProject(newProject.id);
        setShowCloneModal(false); // Auto-close on success
      }
    } catch (e) { console.error("Clone failed:", e); }
    setCloneLoading(false);
  }, [projects]);

  const handleLLMSave = useCallback((url, token) => { llm.save(url, token); }, [llm]);
  const handleLLMFetchModels = useCallback((fetchUrl, fetchToken) => {
    llm.fetchModels(fetchUrl || llm.llmUrl, fetchToken || llm.llmToken);
  }, [llm]);
  const handleLLMTest = useCallback((url, token) => { llm.test(url, token); }, [llm]);

  const isLoading = graph.status === "loading" || projects.loading;

  return (
    <AppShell>
      <ParticleBackground thinking={chat.status === "classifying" || chat.status === "answering"} />
      <StarTrail matchedNodes={matchedNodes} links={graph.graphData?.graphify?.links || []} hovered={orbHovered} graphData={graph.graphData} active={chat.status === "answering"} loading={chat.status === "classifying" || chat.status === "thinking" || chat.status === "answering"} />
      <Sidebar projects={projects.projects} activePid={projects.activePid}
        activePanel={activePanel} auth={auth}
        onSelectProject={projects.selectProject}
        onNewProject={() => setShowCloneModal(true)}
        onSwitchPanel={setActivePanel}
        onRename={projects.renameProject}
        onDelete={async (pid) => { await projects.deleteProject(pid); chat.clearChats(pid); }}
        onPull={projects.pullProject}
        tokenExpired={projects.tokenExpired}
        onShare={projects.shareProject}
        onJoin={projects.joinProject}
        onUpdateToken={projects.updateToken}
      />
      <div className="flex flex-1 min-w-0 overflow-hidden h-full">
        <AnimatePresence mode="wait">
          {activePanel === "chat" && (
            <motion.div key="chat" initial={{ opacity: 0 }} animate={{ opacity: 1 }}
              exit={{ opacity: 0 }} transition={{ duration: 0.15 }} className="flex flex-1 min-w-0">
              <ChatPanel messages={chat.messages} conversations={chat.conversations}
                activeConvId={chat.activeConvId} newConversation={chat.newConversation}
                deleteConversation={chat.deleteConversation}
                switchConversation={chat.switchConversation}
                status={chat.status} streamingContent={chat.streamingContent}
                onSend={chat.sendMessage} activeProject={projects.activeProject}
                graphData={graph.graphData}
                sendFeedback={chat.sendFeedback}
                llmConfigured={!!(llm.llmUrl && llm.llmToken)}
                onGoToLLM={() => setActivePanel("llm")}
              />
            </motion.div>
          )}
          {activePanel === "llm" && (
            <motion.div key="settings" initial={{ opacity: 0 }} animate={{ opacity: 1 }}
              exit={{ opacity: 0 }} transition={{ duration: 0.15 }} className="flex flex-1 min-w-0">
              <LLMSettings llmUrl={llm.llmUrl} llmToken={llm.llmToken} model={llm.model}
                models={llm.models} modelsLoading={llm.modelsLoading}
                testResult={llm.testResult}
                onSave={handleLLMSave} onFetchModels={handleLLMFetchModels}
                onSelectModel={llm.selectModel} onTest={handleLLMTest}
              />
            </motion.div>
          )}
          {activePanel === "guide" && (
            <motion.div key="guide" initial={{ opacity: 0 }} animate={{ opacity: 1 }}
              exit={{ opacity: 0 }} transition={{ duration: 0.15 }} className="flex flex-1 min-w-0">
              <GuidePanel activePid={projects.activePid} activeProject={projects.activeProject} />
            </motion.div>
          )}
          {activePanel === "branch" && (
            <motion.div key="branch" initial={{ opacity: 0 }} animate={{ opacity: 1 }}
              exit={{ opacity: 0 }} transition={{ duration: 0.15 }} className="flex flex-1 min-w-0">
              <BranchPanel activePid={projects.activePid} activeProject={projects.activeProject}
                onPull={projects.pullProject} fetchBranches={projects.fetchBranches} />
            </motion.div>
          )}
          {activePanel === "tuning" && (
            <motion.div key="tuning" initial={{ opacity: 0 }} animate={{ opacity: 1 }}
              exit={{ opacity: 0 }} transition={{ duration: 0.15 }} className="flex flex-1 min-w-0">
              <TuningPanel />
            </motion.div>
          )}
        </AnimatePresence>
      </div>
      <GraphPanel activePid={projects.activePid} answerComplete={answerComplete}
        projectStatus={activeProject?.status}
        onHoverChange={setOrbHovered} />
      {showCloneModal && (
        <CloneModal loading={cloneLoading} onClose={() => setShowCloneModal(false)}
          onClone={handleClone} auth={auth} />
      )}
      {isLoading && <LoadingOverlay title="Loading" />}
    </AppShell>
  );
}