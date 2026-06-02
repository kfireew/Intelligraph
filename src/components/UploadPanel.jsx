import { useState, useRef, useCallback } from "react";
import { Upload, FileJson, Database, Trash2, CheckCircle2, FileCode } from "lucide-react";

export function UploadPanel({ graphifyStatus, crgStatus, htmlStatus, onUpload, onClear, onRefresh, onReloadGraph }) {
  const [dragOver, setDragOver] = useState(null);

  const handleUpload = useCallback(async (file, type) => {
    const returnedPid = await onUpload(file, type);
    if (onReloadGraph) onReloadGraph(returnedPid);
  }, [onUpload, onReloadGraph]);

  const handleDrop = useCallback((e, type) => {
    e.preventDefault();
    setDragOver(null);
    const file = e.dataTransfer.files[0];
    if (file) handleUpload(file, type);
  }, [handleUpload]);

  return (
    <div className="flex flex-col flex-1 min-h-0 p-6 overflow-y-auto">
      <h2 className="text-lg font-bold gradient-text mb-1">Upload Graph Files</h2>
      <p className="text-xs text-muted mb-6">Upload graphify JSON and CRG SQLite database files</p>

      {/* Graphify drop zone */}
      <DropZone
        title="graphify Graph"
        description="graph.json — knowledge graph with nodes, edges, communities"
        icon={FileJson}
        type="graphify"
        status={graphifyStatus}
        onUpload={handleUpload}
        dragOver={dragOver}
        setDragOver={setDragOver}
        onDrop={handleDrop}
      />

      {/* CRG drop zone */}
      <DropZone
        title="CRG Database"
        description="graph.db — SQLite with FTS5 full-text search"
        icon={Database}
        type="crg"
        status={crgStatus}
        onUpload={handleUpload}
        dragOver={dragOver}
        setDragOver={setDragOver}
        onDrop={handleDrop}
      />

      {/* HTML drop zone */}
      <DropZone
        title="Graph HTML"
        description="graph.html — pre-built vis-network visualization"
        icon={FileCode}
        type="html"
        status={htmlStatus}
        onUpload={handleUpload}
        dragOver={dragOver}
        setDragOver={setDragOver}
        onDrop={handleDrop}
      />

      {/* Clear button */}
      <button
        onClick={onClear}
        className="flex items-center gap-2 px-4 py-2 rounded-lg text-xs font-bold text-red/80 bg-red/5 border border-red/10 hover:bg-red/10 transition-colors mt-4 self-start"
      >
        <Trash2 size={14} />
        Clear Uploads
      </button>
    </div>
  );
}

function DropZone({ title, description, icon: Icon, type, status, onUpload, dragOver, setDragOver, onDrop }) {
  const fileRef = useRef(null);
  const isDragActive = dragOver === type;

  return (
    <div
      className={`mb-4 rounded-xl border-2 border-dashed p-6 transition-colors cursor-pointer ${
        isDragActive
          ? "border-accent bg-accent/5"
          : "border-glass-border hover:border-border bg-transparent"
      }`}
      onClick={() => fileRef.current?.click()}
      onDragOver={(e) => { e.preventDefault(); setDragOver(type); }}
      onDragLeave={() => setDragOver(null)}
      onDrop={(e) => onDrop(e, type)}
    >
      <input
        ref={fileRef}
        type="file"
        accept={type === "graphify" ? ".json" : type === "html" ? ".html" : ".db"}
        className="hidden"
        onChange={(e) => { const f = e.target.files[0]; if (f) onUpload(f, type); }}
      />
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-lg bg-accent/10 flex items-center justify-center flex-shrink-0">
            <Icon size={20} className="text-accent-light" />
          </div>
          <div>
            <h3 className="text-sm font-bold text-text m-0">{title}</h3>
            <p className="text-[11px] text-muted m-0 mt-0.5">{description}</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {status?.loaded ? (
            <div className="flex items-center gap-1.5 text-[11px] text-green">
              <CheckCircle2 size={14} />
              <span>{status.message}</span>
            </div>
          ) : (
            <span className="text-[11px] text-muted">{status?.message || "No file"}</span>
          )}
          <Upload size={14} className="text-muted" />
        </div>
      </div>
    </div>
  );
}