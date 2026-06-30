import { useState, useCallback, useRef } from "react";

function normalizeModels(items) {
  if (!Array.isArray(items)) return [];
  return items
    .filter((m) => typeof m === "object" && m)
    .map((m) => ({
      id: m.id || m.name || m.slug || m.model || String(m),
      name: m.name || m.id || m.label || m.description || "",
    }));
}

export function useLLM() {
  const [llmUrl, setLlmUrl] = useState(
    () => localStorage.getItem("llm-url") || "https://models.ai-services.idf.cts/v1/chat/completions"
  );
  const [llmToken, setLlmToken] = useState(
    () => localStorage.getItem("llm-token") || ""
  );
  const [model, setModel] = useState(
    () => localStorage.getItem("llm-model") || ""
  );
  const [models, setModels] = useState(() => {
    try {
      const cached = JSON.parse(localStorage.getItem("llm-models-cache") || "null");
      if (cached && cached.ts > Date.now() - 3600000) return cached.data;
    } catch {}
    return [];
  });
  const [modelsLoading, setModelsLoading] = useState(false);
  const [testResult, setTestResult] = useState("");
  const savedUrlRef = useRef(null);
  const savedTokenRef = useRef(null);
  const modelRef = useRef(model);
  if (model) modelRef.current = model;

  const fetchModels = useCallback(async (url, token) => {
    if (!url || !token) return;
    setModelsLoading(true);
    try {
      const r = await fetch("/llm/models", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url, token }),
      });
      const j = await r.json();
      const list = normalizeModels(j.models || []);
      setModels(list);
      localStorage.setItem("llm-models-cache", JSON.stringify({ ts: Date.now(), data: list }));
      return list;
    } catch {
      return [];
    } finally {
      setModelsLoading(false);
    }
  }, []);

  const save = useCallback((url, token) => {
    setLlmUrl(url);
    setLlmToken(token);
    localStorage.setItem("llm-url", url);
    localStorage.setItem("llm-token", token);
    setTestResult("");
    savedUrlRef.current = url;
    savedTokenRef.current = token;
  }, []);

  const selectModel = useCallback((m) => {
    setModel(m);
    modelRef.current = m;
    localStorage.setItem("llm-model", m);
  }, []);

  const test = useCallback(async () => {
    const payload = {
      model: modelRef.current || "gpt-4o-mini",
      messages: [{ role: "user", content: "hi" }],
      max_tokens: 10,
      temperature: 0.1,
    };
    try {
      const r = await fetch("/llm/relay", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: savedUrlRef.current, token: savedTokenRef.current, payload }),
      });
      const j = await r.json();
      if (j.status === 405) {
        setTestResult("Error: 405 method not allowed — the LLM endpoint rejected the request. Check the URL.");
        return;
      }
      const body = JSON.parse(j.body || "{}");
      const content = body.choices?.[0]?.message?.content;
      if (content) {
        setTestResult("Connected: " + content);
      } else if (body.choices?.[0]?.message?.reasoning) {
        setTestResult("Connected: " + body.choices[0].message.reasoning.slice(0, 30));
      } else if (body.error) {
        setTestResult("Error: " + (body.error.message || JSON.stringify(body.error)));
      } else {
        setTestResult("Error: No response content");
      }
    } catch (e) {
      setTestResult("Failed: " + e.message);
    }
  }, []);

  return {
    llmUrl, setLlmUrl, llmToken, setLlmToken,
    model, selectModel, models, modelsLoading,
    testResult, save, test, fetchModels,
  };
}