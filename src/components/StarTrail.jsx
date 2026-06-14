import { useRef, useEffect, useMemo } from "react";

export function StarTrail({ matchedNodes, links, hovered, active }) {
  const canvasRef = useRef(null);
  const fadeRef = useRef(0);
  const hoveredRef = useRef(false);
  const lastNodesRef = useRef([]);
  const phaseRef = useRef(0);
  const wasActiveRef = useRef(false);
  const timerRef = useRef(null);

  hoveredRef.current = hovered;

  if (matchedNodes?.length > 0) lastNodesRef.current = matchedNodes;
  const nodes = active ? matchedNodes : lastNodesRef.current;
  const effectiveNodes = nodes?.length > 0 ? nodes : matchedNodes;
  const MAX_STARS = 8;
  const mn = useMemo(() => effectiveNodes.slice(0, MAX_STARS), [effectiveNodes]);

  useEffect(() => {
    if (active && !wasActiveRef.current) {
      phaseRef.current = 0;
      const total = mn.length;
      if (total === 0) return;
      const stepInterval = setInterval(() => {
        phaseRef.current = Math.min(phaseRef.current + 1, total);
        if (phaseRef.current >= total) clearInterval(stepInterval);
      }, 80);
      wasActiveRef.current = true;
      return () => clearInterval(stepInterval);
    }
    if (!active) wasActiveRef.current = false;
  }, [active, mn.length]);

  useEffect(() => {
    if (!canvasRef.current) return;
    const canvas = canvasRef.current;
    if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null; }

    const ctx = canvas.getContext("2d");
    const resize = () => { canvas.width = window.innerWidth; canvas.height = window.innerHeight; };
    resize();

    const cNodes = mn;
    const cLinks = links;
    const matchedIds = new Set(cNodes.map(n => n.id));
    let seed = 0;
    for (const n of cNodes) for (let c of (n.id || "")) seed += c.charCodeAt(0);
    const rng = (max) => ((seed = (seed * 9301 + 49297) % 233280) / 233280) * max;

    const vw = window.innerWidth, vh = window.innerHeight;
    const cx = vw * 0.7, cy = vh * 0.6;

    const pos = {};

    const edges = [];
    if (cLinks?.length) {
      for (const l of cLinks) {
        const src = (typeof l.source === "object" ? l.source?.id || l.source?.name : l.source) || l.from;
        const tgt = (typeof l.target === "object" ? l.target?.id || l.target?.name : l.target) || l.to;
        if (src && tgt && matchedIds.has(src) && matchedIds.has(tgt)) edges.push({ source: src, target: tgt });
      }
      if (!edges.length) {
        const lowerIds = new Set([...matchedIds].map(id => id.toLowerCase()));
        for (const l of cLinks) {
          const src = (typeof l.source === "object" ? l.source?.id || l.source?.name : l.source) || l.from;
          const tgt = (typeof l.target === "object" ? l.target?.id || l.target?.name : l.target) || l.to;
          if (src && tgt && lowerIds.has(src.toLowerCase()) && lowerIds.has(tgt.toLowerCase())) edges.push({ source: src, target: tgt });
        }
      }
    }

    const connCount = {};
    for (const n of cNodes) connCount[n.id] = 0;
    for (const e of edges) { connCount[e.source] = (connCount[e.source] || 0) + 1; connCount[e.target] = (connCount[e.target] || 0) + 1; }
    const maxConn = Math.max(1, ...Object.values(connCount));

    cNodes.forEach((n, i) => {
      const deg = connCount[n.id] || 0;
      const cluster = 1 - (deg / maxConn) * 0.5;
      pos[n.id] = {
        x: cx + (rng(vw * 0.14) - vw * 0.07) * cluster,
        y: cy + (rng(vh * 0.12) - vh * 0.06) * cluster,
      };
    });

    const draw = () => {
      ctx.clearRect(0, 0, canvas.width, canvas.height);

      const tgt = hoveredRef.current ? 1 : 0;
      fadeRef.current += (tgt - fadeRef.current) * 0.08;
      if (Math.abs(fadeRef.current - tgt) < 0.001) fadeRef.current = tgt;
      const bright = 0.45 + fadeRef.current * 0.25;
      const lOp = bright * 0.7;
      const sOp = bright * 0.8;

      if (cNodes.length > 0) {
        const vc = Math.min(phaseRef.current, cNodes.length);
        const vIds = new Set(cNodes.slice(0, vc).map(n => n.id));
        const arr = cNodes.slice(0, vc);

        for (const e of edges) {
          if (!vIds.has(e.source) || !vIds.has(e.target)) continue;
          const p1 = pos[e.source], p2 = pos[e.target];
          if (!p1 || !p2) continue;
          ctx.beginPath(); ctx.moveTo(p1.x, p1.y); ctx.lineTo(p2.x, p2.y);
          ctx.strokeStyle = `rgba(139, 92, 246, ${lOp})`; ctx.lineWidth = 4; ctx.stroke();
        }

        for (let i = 0; i < arr.length - 1; i++) {
          const p1 = pos[arr[i].id], p2 = pos[arr[i + 1].id];
          if (!p1 || !p2) continue;
          ctx.beginPath(); ctx.moveTo(p1.x, p1.y); ctx.lineTo(p2.x, p2.y);
          ctx.strokeStyle = `rgba(139, 92, 246, ${lOp * 0.25})`; ctx.lineWidth = 2; ctx.stroke();
        }

        ctx.filter = "blur(1.5px)";
        for (let i = 0; i < vc; i++) {
          const n = cNodes[i], p = pos[n.id];
          if (!p) continue;
          const r = fadeRef.current > 0.5 ? 3 : 2;
          const gr = fadeRef.current > 0.5 ? 16 : 12;
          const grad = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, gr);
          grad.addColorStop(0, `rgba(139, 92, 246, ${sOp * 0.35})`);
          grad.addColorStop(1, "rgba(139, 92, 246, 0)");
          ctx.fillStyle = grad; ctx.beginPath(); ctx.arc(p.x, p.y, gr, 0, Math.PI * 2); ctx.fill();
          ctx.fillStyle = `rgba(167, 139, 250, ${sOp})`;
          ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, Math.PI * 2); ctx.fill();
        }
        ctx.filter = "none";
      }
    };
    draw();

    timerRef.current = setInterval(() => {
      const tgt = hoveredRef.current ? 1 : 0;
      if (Math.abs(fadeRef.current - tgt) > 0.001 || phaseRef.current < cNodes.length) draw();
      else { clearInterval(timerRef.current); timerRef.current = null; }
    }, 66);

    window.addEventListener("resize", resize);
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
      timerRef.current = null;
      window.removeEventListener("resize", resize);
    };
  }, [mn, links]);

  if (!effectiveNodes?.length) return null;

  return (
    <canvas
      ref={canvasRef}
      style={{
        position: "fixed", top: 0, left: 0,
        width: "100%", height: "100%",
        pointerEvents: "none",
        opacity: 0.7,
        zIndex: active ? 1 : -1,
      }}
    />
  );
}