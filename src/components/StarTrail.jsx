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

  // Phase reveal when active starts
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

  // Canvas render — recalculates only when matchedNodes change (not on hover)
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null; }

    const ctx = canvas.getContext("2d");
    const resize = () => { canvas.width = window.innerWidth; canvas.height = window.innerHeight; };
    resize();

    const currentNodes = mn;
    const currentLinks = links;
    const matchedIds = new Set(currentNodes.map(n => n.id));
    let seed = 0;
    for (const n of currentNodes) for (let c of (n.id || "")) seed += c.charCodeAt(0);
    const rng = (max) => ((seed = (seed * 9301 + 49297) % 233280) / 233280) * max;

    const viewW = window.innerWidth;
    const viewH = window.innerHeight;
    const cx = viewW * 0.7;
    const cy = viewH * 0.6;

    const positions = {};

    // Edges between matched nodes
    const edges = [];
    if (currentLinks?.length) {
      for (const l of currentLinks) {
        const src = (typeof l.source === "object" ? l.source?.id || l.source?.name : l.source) || l.from;
        const tgt = (typeof l.target === "object" ? l.target?.id || l.target?.name : l.target) || l.to;
        if (src && tgt && matchedIds.has(src) && matchedIds.has(tgt)) edges.push({ source: src, target: tgt });
      }
      if (!edges.length) {
        const lowerIds = new Set([...matchedIds].map(id => id.toLowerCase()));
        for (const l of currentLinks) {
          const src = (typeof l.source === "object" ? l.source?.id || l.source?.name : l.source) || l.from;
          const tgt = (typeof l.target === "object" ? l.target?.id || l.target?.name : l.target) || l.to;
          if (src && tgt && lowerIds.has(src.toLowerCase()) && lowerIds.has(tgt.toLowerCase())) edges.push({ source: src, target: tgt });
        }
      }
    }

    // Connection count
    const connCount = {};
    for (const n of currentNodes) connCount[n.id] = 0;
    for (const e of edges) { connCount[e.source] = (connCount[e.source] || 0) + 1; connCount[e.target] = (connCount[e.target] || 0) + 1; }
    const maxConn = Math.max(1, ...Object.values(connCount));

    // Box layout — fully random positions, no radial pattern
    currentNodes.forEach((n, i) => {
      const deg = connCount[n.id] || 0;
      const cluster = 1 - (deg / maxConn) * 0.5; // connected nodes cluster nearer to center
      positions[n.id] = {
        x: cx + (rng(viewW * 0.14) - viewW * 0.07) * cluster,
        y: cy + (rng(viewH * 0.12) - viewH * 0.06) * cluster,
      };
    });

    // Draw once immediately
    const draw = () => {
      ctx.clearRect(0, 0, canvas.width, canvas.height);

      const target = hoveredRef.current ? 1 : 0;
      fadeRef.current += (target - fadeRef.current) * 0.08;
      if (Math.abs(fadeRef.current - target) < 0.001) fadeRef.current = target;
      const brightness = 0.35 + fadeRef.current * 0.3;
      const starOp = brightness * 0.65;
      const lineOp = brightness * 0.5;

      if (currentNodes.length > 0) {
        const visibleCount = Math.min(phaseRef.current, currentNodes.length);
        const visibleIds = new Set(currentNodes.slice(0, visibleCount).map(n => n.id));

        ctx.filter = "blur(1.5px)";
        for (const e of edges) {
          if (!visibleIds.has(e.source) || !visibleIds.has(e.target)) continue;
          const p1 = positions[e.source], p2 = positions[e.target];
          if (!p1 || !p2) continue;
          ctx.beginPath(); ctx.moveTo(p1.x, p1.y); ctx.lineTo(p2.x, p2.y);
          ctx.strokeStyle = `rgba(139, 92, 246, ${lineOp})`; ctx.lineWidth = 0.8; ctx.stroke();
        }
        for (let i = 0; i < visibleCount; i++) {
          const n = currentNodes[i], pos = positions[n.id];
          if (!pos) continue;
          const r = fadeRef.current > 0.5 ? 3 : 2, gr = fadeRef.current > 0.5 ? 14 : 10;
          const grad = ctx.createRadialGradient(pos.x, pos.y, 0, pos.x, pos.y, gr);
          grad.addColorStop(0, `rgba(139, 92, 246, ${starOp * 0.35})`);
          grad.addColorStop(1, "rgba(139, 92, 246, 0)");
          ctx.fillStyle = grad; ctx.beginPath(); ctx.arc(pos.x, pos.y, gr, 0, Math.PI * 2); ctx.fill();
          ctx.fillStyle = `rgba(167, 139, 250, ${starOp})`;
          ctx.beginPath(); ctx.arc(pos.x, pos.y, r, 0, Math.PI * 2); ctx.fill();
        }
        ctx.filter = "none";
      }
    };
    draw();

    // Throttled redraw at ~15fps while animating
    timerRef.current = setInterval(() => {
      const target = hoveredRef.current ? 1 : 0;
      const fadeDiff = Math.abs(fadeRef.current - target);
      const phaseDone = phaseRef.current >= currentNodes.length;
      if (fadeDiff > 0.001 || !phaseDone) {
        draw();
      } else {
        if (timerRef.current) clearInterval(timerRef.current);
        timerRef.current = null;
      }
    }, 66); // ~15fps

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
      className="star-trail-canvas"
      style={{
        position: "fixed", top: 0, left: 0,
        width: "100%", height: "100%",
        pointerEvents: "none",
        opacity: 0.7,
        zIndex: active ? 51 : 2,
      }}
    />
  );
}