// Port of SQL.js CRG queries and graphify fallback from templates/index.html

export function searchNodes(db, graphifyData, query, limit = 15) {
  const results = [];
  if (db) {
    try {
      // Tokenize: split into words, filter short words, build prefix FTS5
      const tokens = query.toLowerCase().split(/[\s,.-]+/).filter(w => w.length > 1 && !/^(hi|hey|the|a|an|is|are|was|in|on|of|to|for|and|or|what|where|how|who|why|tell|me|about|explain|show|find|get|list|can|you|please)$/i.test(w));
      let sql;
      try {
        if (tokens.length > 0) {
          // FTS5 prefix search: "word1* OR word2*"
          const ftsQuery = tokens.map(t => `"${t}"*`).join(" OR ");
          sql = `SELECT n.kind,n.name,n.qualified_name,n.file_path,n.line_start,n.signature FROM nodes_fts f JOIN nodes n ON n.qualified_name=f.qualified_name WHERE nodes_fts MATCH ? ORDER BY rank LIMIT ?`;
          const stmt = db.prepare(sql);
          stmt.bind([ftsQuery, limit]);
          while (stmt.step()) results.push(stmt.getAsObject());
          stmt.free();
        }
        if (!results.length) {
          // Fallback: exact phrase
          sql = "SELECT kind,name,qualified_name,file_path,line_start,signature FROM nodes WHERE name LIKE ? OR qualified_name LIKE ? LIMIT ?";
          const stmt = db.prepare(sql);
          const q = query.toLowerCase();
          stmt.bind([`%${q}%`, `%${q}%`, limit]);
          while (stmt.step()) results.push(stmt.getAsObject());
          stmt.free();
        }
      } catch {
        // LIKE fallback
        sql = "SELECT kind,name,qualified_name,file_path,line_start,signature FROM nodes WHERE name LIKE ? OR qualified_name LIKE ? LIMIT ?";
        const stmt = db.prepare(sql);
        const q = query.toLowerCase();
        stmt.bind([`%${q}%`, `%${q}%`, limit]);
        while (stmt.step()) results.push(stmt.getAsObject());
        stmt.free();
      }
      if (results.length) return results;
    } catch {
      // fall through to graphify
    }
  }
  if (!graphifyData?.nodes) return results;
  const ql = query.toLowerCase();
  for (const n of graphifyData.nodes) {
    if ((n.label || "").toLowerCase().includes(ql) || (n.source_file || "").toLowerCase().includes(ql)) {
      results.push({
        name: n.label || n.id,
        kind: n.file_type || n.kind,
        qualified_name: n.id || n.label,
        file_path: n.source_file,
      });
      if (results.length >= limit) break;
    }
  }
  return results;
}

export function getCallers(db, graphifyData, target, limit = 30) {
  const t = `%${target}%`;
  const results = [];
  if (db) {
    try {
      const sql = "SELECT kind,source_qualified,target_qualified,file_path,line FROM edges WHERE target_qualified LIKE ? LIMIT ?";
      const stmt = db.prepare(sql);
      stmt.bind([t, limit]);
      while (stmt.step()) results.push(stmt.getAsObject());
      stmt.free();
      if (results.length) return results;
    } catch {}
  }
  if (!graphifyData?.links) return results;
  const tl = target.toLowerCase();
  for (const l of graphifyData.links) {
    if ((l.target || "").toLowerCase().includes(tl)) {
      const src = (graphifyData.nodes || []).find((n) => n.id === l.source);
      results.push({
        source_qualified: l.source, target_qualified: l.target,
        kind: l.type || "calls", file_path: src?.source_file, line: l.line,
        name: src?.label, source: "graphify",
      });
      if (results.length >= limit) break;
    }
  }
  return results;
}

export function getCallees(db, graphifyData, target, limit = 30) {
  const t = `%${target}%`;
  const results = [];
  if (db) {
    try {
      const sql = "SELECT kind,source_qualified,target_qualified,file_path,line FROM edges WHERE source_qualified LIKE ? LIMIT ?";
      const stmt = db.prepare(sql);
      stmt.bind([t, limit]);
      while (stmt.step()) results.push(stmt.getAsObject());
      stmt.free();
      if (results.length) return results;
    } catch {}
  }
  if (!graphifyData?.links) return results;
  const tl = target.toLowerCase();
  for (const l of graphifyData.links) {
    if ((l.source || "").toLowerCase().includes(tl)) {
      const tgt = (graphifyData.nodes || []).find((n) => n.id === l.target);
      results.push({
        source_qualified: l.source, target_qualified: l.target,
        kind: l.type || "calls", file_path: tgt?.source_file, line: l.line,
        name: tgt?.label, source: "graphify",
      });
      if (results.length >= limit) break;
    }
  }
  return results;
}

export function getImpact(db, graphifyData, target) {
  const matches = searchNodes(db, graphifyData, target, 5);
  const qnames = matches.map((m) => m.qualified_name || m.name).filter(Boolean);
  const dependents = [];
  const flows = [];
  if (db && qnames.length) {
    try {
      const placeholders = qnames.map(() => "?").join(",");
      const sql = `SELECT kind,source_qualified,file_path,line FROM edges WHERE target_qualified IN (${placeholders}) AND kind IN ('CALLS','IMPORTS') LIMIT 50`;
      const stmt = db.prepare(sql);
      stmt.bind(qnames);
      while (stmt.step()) dependents.push(stmt.getAsObject());
      stmt.free();
      for (const qn of qnames) {
        try {
          const fsql = "SELECT DISTINCT f.name,f.criticality FROM flows f JOIN flow_memberships fm ON fm.flow_id=f.id JOIN nodes n ON n.id=fm.node_id WHERE n.qualified_name LIKE ? LIMIT 10";
          const fstmt = db.prepare(fsql);
          fstmt.bind([`%${qn}%`]);
          while (fstmt.step()) flows.push(fstmt.getAsObject());
          fstmt.free();
        } catch {}
      }
    } catch {}
  }
  if (!dependents.length && graphifyData?.links) {
    const tl = target.toLowerCase();
    for (const l of graphifyData.links) {
      if ((l.target || "").toLowerCase().includes(tl)) {
        dependents.push({ source_qualified: l.source, kind: l.type || "calls", source: "graphify" });
      }
    }
  }
  return { matched: matches, dependents: dependents.slice(0, 50), flows };
}

export function getArchitecture(db, graphifyData) {
  const communities = [];
  const flowsList = [];
  const kinds = [];
  const hubs = [];
  if (db) {
    try {
      const csql = "SELECT community_id,name,purpose,risk,size,dominant_language FROM community_summaries ORDER BY size DESC";
      const cstmt = db.prepare(csql);
      while (cstmt.step()) communities.push(cstmt.getAsObject());
      cstmt.free();
      const flsql = "SELECT name,criticality,node_count,depth FROM flows ORDER BY criticality DESC LIMIT 15";
      const flstmt = db.prepare(flsql);
      while (flstmt.step()) flowsList.push(flstmt.getAsObject());
      flstmt.free();
      const ksql = "SELECT kind,COUNT(*) as count FROM nodes GROUP BY kind ORDER BY count DESC";
      const kstmt = db.prepare(ksql);
      while (kstmt.step()) kinds.push(kstmt.getAsObject());
      kstmt.free();
      const hsql = "SELECT n.name,n.qualified_name,COUNT(e.id) as degree FROM nodes n JOIN edges e ON e.source_qualified=n.qualified_name GROUP BY n.qualified_name ORDER BY degree DESC LIMIT 10";
      const hstmt = db.prepare(hsql);
      while (hstmt.step()) hubs.push(hstmt.getAsObject());
      hstmt.free();
    } catch {
      // fallback
    }
  }
  if (!communities.length && graphifyData) {
    const kindMap = {};
    (graphifyData.nodes || []).forEach((n) => {
      const k = n.file_type || "unknown";
      kindMap[k] = (kindMap[k] || 0) + 1;
    });
    for (const [kind, count] of Object.entries(kindMap).sort((a, b) => b[1] - a[1])) {
      kinds.push({ kind, count });
    }
    (graphifyData.communities || []).forEach((c) => {
      communities.push({
        name: c.name || c.id,
        size: c.size || (c.nodes || []).length,
        dominant_language: c.language || c.dominant_language,
        purpose: c.description || "",
      });
    });
  }
  return { communities, flows: flowsList, kinds, hubs };
}

export function getTests(db, graphifyData, target) {
  const results = [];
  const t = `%${target}%`;
  if (db) {
    try {
      const sql = "SELECT name,file_path,line_start FROM nodes WHERE is_test=1 AND (name LIKE ? OR file_path LIKE ?) LIMIT 20";
      const stmt = db.prepare(sql);
      stmt.bind([t, t]);
      while (stmt.step()) results.push(stmt.getAsObject());
      stmt.free();
      if (results.length) return results;
    } catch {}
  }
  if (!graphifyData?.nodes) return results;
  const tl = target.toLowerCase();
  for (const n of graphifyData.nodes) {
    if (n.file_type === "Test" || (n.label || "").toLowerCase().includes("test")) {
      if ((n.label || "").toLowerCase().includes(tl) || (n.source_file || "").toLowerCase().includes(tl)) {
        results.push({ name: n.label, file_path: n.source_file, line_start: n.line_start });
      }
    }
    if (results.length >= 20) break;
  }
  return results;
}

export function findFileDetails(db, filePath) {
  if (!db || !filePath) return [];
  try {
    const sql = "SELECT qualified_name, name, kind, file_path, line_start, signature FROM nodes WHERE file_path = ? OR file_path LIKE ? LIMIT 40";
    const stmt = db.prepare(sql);
    stmt.bind([filePath, `%${filePath}`]);
    const rows = [];
    while (stmt.step()) rows.push(stmt.getAsObject());
    stmt.free();
    return rows;
  } catch { return []; }
}