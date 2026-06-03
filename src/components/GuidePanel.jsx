import { useState, useRef } from "react";
import { BookOpen, Download, Upload, Database, FileJson, CheckCircle2 } from "lucide-react";
import { endpoints } from "../config/endpoints";

export function GuidePanel({ onMCPUpload, mcpStatus, graphifyStatus, crgStatus }) {
  const [uploading, setUploading] = useState(null);

  return (
    <div className="flex flex-col flex-1 min-h-0 p-6 overflow-y-auto space-y-6">
      <div className="flex items-center gap-2">
        <BookOpen size={18} className="text-accent-light" />
        <h2 className="text-lg font-bold gradient-text">How to + MCP</h2>
      </div>

      {/* Generate graphs section */}
      <Section title="Manually Generate Graph Files" icon={Download}>
        <ol className="text-xs text-text-secondary space-y-2 m-0 pl-4">
          <li>Clone a repo or download graph-builder</li>
          <li>Run <code className="px-1.5 py-0.5 rounded bg-accent/10 text-accent-light text-[11px]">python graph_builder.py /path/to/repo</code></li>
          <li>Upload the generated graph.json and graph.db here</li>
        </ol>
        <a
          href={endpoints.downloadGraphBuilder}
          className="inline-flex items-center gap-1.5 mt-3 px-3 py-1.5 rounded-lg text-[11px] font-bold text-accent-light bg-accent/10 border border-accent/20 hover:bg-accent/15 transition-colors"
        >
          <Download size={12} />
          Download graph-builder
        </a>
      </Section>

      {/* MCP Standalone */}
      <Section title="MCP Server — Standalone (Local)" icon={Download}>
        <p className="text-[12px] text-muted m-0 mb-3">
          Download the standalone MCP server and run it locally with your graph files.
          Claude Code connects via stdio.
        </p>
        <a
          href={endpoints.downloadMCPServer}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-[11px] font-bold text-accent-light bg-accent/10 border border-accent/20 hover:bg-accent/15 transition-colors"
        >
          <Download size={12} />
          Download mcp_server_standalone.py
        </a>
      </Section>

      {/* MCP Online */}
      <Section title="MCP Server — Online (Pod)" icon={Upload}>
        <p className="text-[12px] text-muted m-0 mb-3">
          Upload graph files to the pod to use as an HTTP MCP endpoint for Claude Code.
        </p>
        <div className="flex gap-3">
          <button
            onClick={() => { const el = document.getElementById("mcp-file-crg"); el?.click(); }}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-[11px] font-bold text-cyan bg-cyan/5 border border-cyan/15 hover:bg-cyan/10 transition-colors"
          >
            <Database size={12} />
            {crgStatus?.loaded ? (
              <span className="text-green">Uploaded <CheckCircle2 size={10} className="inline" /></span>
            ) : "Upload .db"}
          </button>
          <button
            onClick={() => { const el = document.getElementById("mcp-file-graphify"); el?.click(); }}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-[11px] font-bold text-cyan bg-cyan/5 border border-cyan/15 hover:bg-cyan/10 transition-colors"
          >
            <FileJson size={12} />
            {graphifyStatus?.loaded ? (
              <span className="text-green">Uploaded <CheckCircle2 size={10} className="inline" /></span>
            ) : "Upload .json"}
          </button>
        </div>
        <input id="mcp-file-crg" type="file" accept=".db" className="hidden" onChange={(e) => { const f = e.target.files[0]; if (f) onMCPUpload(f, "crg"); }} />
        <input id="mcp-file-graphify" type="file" accept=".json" className="hidden" onChange={(e) => { const f = e.target.files[0]; if (f) onMCPUpload(f, "graphify"); }} />
        {mcpStatus && (
          <div className="flex items-center gap-1.5 mt-3 text-[11px] text-green">
            <CheckCircle2 size={12} />
            {mcpStatus}
          </div>
        )}
        <a
          href={endpoints.downloadMCPConfig}
          className="inline-flex items-center gap-1.5 mt-3 px-3 py-1.5 rounded-lg text-[11px] font-bold text-accent-light bg-accent/10 border border-accent/20 hover:bg-accent/15 transition-colors"
        >
          <Download size={12} />
          Download .mcp.json
        </a>
      </Section>
    </div>
  );
}

function Section({ title, icon: Icon, children }) {
  return (
    <div className="glass rounded-xl p-4">
      <div className="flex items-center gap-2 mb-3">
        <Icon size={14} className="text-accent-light" />
        <h3 className="text-sm font-bold text-text m-0">{title}</h3>
      </div>
      {children}
    </div>
  );
}