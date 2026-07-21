import { useRef, useEffect, useMemo } from "react";

// Known constellation patterns — normalized coordinates (0-1 range, x, y)
// Stars are listed in order; edges connect consecutive stars
const CONSTELLATIONS = [
  { name: "Orion", stars: [
    [0.5, 0.15], [0.4, 0.3], [0.6, 0.3], [0.45, 0.45], [0.55, 0.45],
    [0.35, 0.6], [0.65, 0.6], [0.48, 0.75], [0.52, 0.75],
  ]},
  { name: "Big Dipper", stars: [
    [0.2, 0.3], [0.35, 0.25], [0.5, 0.3], [0.6, 0.4], [0.55, 0.55], [0.4, 0.6], [0.3, 0.5],
  ]},
  { name: "Cassiopeia", stars: [
    [0.15, 0.5], [0.3, 0.4], [0.45, 0.55], [0.6, 0.4], [0.75, 0.5],
  ]},
  { name: "Lyra", stars: [
    [0.5, 0.2], [0.45, 0.35], [0.55, 0.35], [0.48, 0.5], [0.52, 0.5],
  ]},
  { name: "Southern Cross", stars: [
    [0.5, 0.3], [0.5, 0.5], [0.5, 0.7], [0.35, 0.5], [0.65, 0.5],
  ]},
];

function randomConstellations(count, seedKey) {
  let seed = seedKey;
  const rng = () => { seed = (seed * 9301 + 49297) % 233280; return seed / 233280; };
  const indices = [...CONSTELLATIONS.keys()];
  // Shuffle
  for (let i = indices.length - 1; i > 0; i--) {
    const j = Math.floor(rng() * (i + 1));
    [indices[i], indices[j]] = [indices[j], indices[i]];
  }
  const picked = indices.slice(0, Math.min(count, CONSTELLATIONS.length));
  const placed = [];
  for (const idx of picked) {
    const base = CONSTELLATIONS[idx];
    // Semi-random position offset (avoid overlap with placed constellations)
    let ox, oy, ok = false;
    for (let attempt = 0; attempt < 20; attempt++) {
      ox = 0.1 + rng() * 0.5;
      oy = 0.1 + rng() * 0.6;
      ok = placed.every(p => {
        const dx = Math.abs(ox - p.ox), dy = Math.abs(oy - p.oy);
        return dx > 0.2 || dy > 0.2;
      });
      if (ok) break;
    }
    placed.push({ ox, oy });
    const stars = base.stars.map((s, i) => ({
      id: `${base.name}-${i}`,
      x: Math.min(0.95, Math.max(0.05, s[0] * 0.3 + ox)),
      y: Math.min(0.9, Math.max(0.05, s[1] * 0.3 + oy)),
      r: 1.5 + rng() * 1.5,
    }));
    const edges = [];
    for (let i = 0; i < stars.length - 1; i++) {
      edges.push({ s: stars[i].id, t: stars[i + 1].id });
    }
    return { stars, edges, name: base.name };
  }
  return null;
}

export function StarTrail({ matchedNodes, links, hovered, active, loading }) {
  const canvasRef = useRef(null);
  const rafRef = useRef(null);
  const stateRef = useRef({
    stars: [],
    fallingStars: [],
    constStars: [],
    constEdges: [],
    phase: 0,
    fade: 0,
    nextFall: 0,
  });

  // Build starfield once
  const starfield = useMemo(() => {
    const arr = [];
    for (let i = 0; i < 200; i++) {
      arr.push({
        x: Math.random(),
        y: Math.random(),
        r: 0.4 + Math.random() * 1.6,
        baseOp: 0.08 + Math.random() * 0.35,
        twinkleSpeed: 0.5 + Math.random() * 2,
        twinklePhase: Math.random() * Math.PI * 2,
      });
    }
    return arr;
  }, []);

  // Build loading constellations when loading starts — different each time
  const loadingConstellations = useMemo(() => {
    if (!loading) return null;
    return randomConstellations(4 + Math.floor(Math.random() * 2), Date.now() % 100000);
  }, [loading]);

  // Build matched-node constellation from response
  const matchedConstData = useMemo(() => {
    if (!matchedNodes?.length) return { stars: [], edges: [] };
    const MAX = 10;
    const nodes = matchedNodes.slice(0, MAX);
    const matchedIds = new Set(nodes.map(n => n.id));
    let seed = 0;
    for (const n of nodes) for (let c of (n.id || "")) seed += c.charCodeAt(0);
    const rng = () => { seed = (seed * 9301 + 49297) % 233280; return seed / 233280; };
    const stars = nodes.map((n, i) => ({
      id: n.id,
      label: n.label || n.id,
      x: 0.2 + rng() * 0.6,
      y: 0.15 + rng() * 0.7,
      r: 1.5 + rng() * 2,
    }));
    const edges = [];
    if (links?.length) {
      for (const l of links) {
        const src = (typeof l.source === "object" ? l.source?.id : l.source) || l.from;
        const tgt = (typeof l.target === "object" ? l.target?.id : l.target) || l.to;
        if (src && tgt && matchedIds.has(src) && matchedIds.has(tgt)) edges.push({ s: src, t: tgt });
      }
    }
    if (!edges.length) {
      for (let i = 0; i < stars.length - 1; i++) edges.push({ s: stars[i].id, t: stars[i + 1].id });
    }
    return { stars, edges };
  }, [matchedNodes, links]);

  useEffect(() => {
    const st = stateRef.current;
    if (loading && loadingConstellations) {
      st.constStars = loadingConstellations.stars;
      st.constEdges = loadingConstellations.edges;
      st.phase = 0;
    }
    // When loading ends, keep the loading constellations visible —
    // they fade out naturally via the fade logic below.
    // Don't switch to matched-node scatter (was a visual mess).
  }, [loadingConstellations, loading]);

  useEffect(() => {
    if (!canvasRef.current) return;
    const canvas = canvasRef.current;
    const ctx = canvas.getContext("2d");
    let w, h;

    const resize = () => {
      w = canvas.width = window.innerWidth;
      h = canvas.height = window.innerHeight;
    };
    resize();

    let t0 = performance.now();
    const loadingRef = { current: false };
    loadingRef.current = !!loading;

    const draw = (now) => {
      const dt = (now - t0) / 1000;
      t0 = now;
      const st = stateRef.current;
      const isLoad = loadingRef.current;

      ctx.clearRect(0, 0, w, h);

      // --- Starfield (density + brightness scales with loading) ---
      const starCount = isLoad ? starfield.length : Math.floor(starfield.length * 0.3);
      for (let i = 0; i < starCount; i++) {
        const s = starfield[i];
        const tw = Math.sin(now / 1000 * s.twinkleSpeed + s.twinklePhase);
        const op = s.baseOp * (0.5 + tw * 0.5) * (isLoad ? 1.4 : 0.6);
        ctx.fillStyle = `rgba(200, 210, 240, ${Math.min(op, 1)})`;
        ctx.beginPath();
        ctx.arc(s.x * w, s.y * h, s.r, 0, Math.PI * 2);
        ctx.fill();
      }

      // --- Falling stars — ONLY during answer loading ---
      if (isLoad) {
        st.nextFall -= dt;
        if (st.nextFall <= 0) {
          st.nextFall = 0.5 + Math.random() * 1.5;
          const startX = Math.random() * w;
          const angle = Math.PI * 0.15 + Math.random() * 0.2;
          st.fallingStars.push({
            x: startX, y: -20,
            vx: Math.cos(angle) * (300 + Math.random() * 200),
            vy: Math.sin(angle) * (300 + Math.random() * 200),
            life: 0, maxLife: 0.6 + Math.random() * 0.8,
          });
        }
      }
      st.fallingStars = st.fallingStars.filter(f => {
        f.x += f.vx * dt; f.y += f.vy * dt; f.life += dt;
        if (f.life > f.maxLife || f.y > h + 50 || f.x > w + 50) return false;
        if (!isLoad && f.life > 0.3) return false;
        const op = Math.sin((f.life / f.maxLife) * Math.PI);
        const grad = ctx.createLinearGradient(f.x, f.y, f.x - f.vx * 0.08, f.y - f.vy * 0.08);
        grad.addColorStop(0, `rgba(255, 255, 255, ${op})`);
        grad.addColorStop(0.3, `rgba(167, 139, 250, ${op * 0.6})`);
        grad.addColorStop(1, "rgba(139, 92, 246, 0)");
        ctx.strokeStyle = grad;
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.moveTo(f.x, f.y);
        ctx.lineTo(f.x - f.vx * 0.08, f.y - f.vy * 0.08);
        ctx.stroke();
        ctx.fillStyle = `rgba(255, 255, 255, ${op})`;
        ctx.beginPath();
        ctx.arc(f.x, f.y, 1.5, 0, Math.PI * 2);
        ctx.fill();
        return true;
      });

      // --- Constellations (from loading or matched nodes) ---
      if (st.constStars.length > 0) {
        const tgt = (hovered || active) ? 1 : 0.5;
        st.fade += (tgt - st.fade) * 0.06;

        if (st.phase < st.constStars.length) {
          st.phase += dt * (isLoad ? 10 : 6);
        }
        const vc = Math.min(Math.floor(st.phase), st.constStars.length);
        const visible = st.constStars.slice(0, vc);
        const vIds = new Set(visible.map(s => s.id));

        // Draw edges
        for (const e of st.constEdges) {
          if (!vIds.has(e.s) || !vIds.has(e.t)) continue;
          const p1 = st.constStars.find(s => s.id === e.s);
          const p2 = st.constStars.find(s => s.id === e.t);
          if (!p1 || !p2) continue;
          ctx.strokeStyle = `rgba(139, 92, 246, ${st.fade * 0.3})`;
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.moveTo(p1.x * w, p1.y * h);
          ctx.lineTo(p2.x * w, p2.y * h);
          ctx.stroke();
        }

        // Draw constellation stars
        for (const s of visible) {
          const sx = s.x * w, sy = s.y * h;
          const tw = Math.sin(now / 800 + s.x * 10);
          const op = (0.5 + st.fade * 0.4) * (0.7 + tw * 0.3);
          const r = s.r * (1 + tw * 0.2);

          // Glow
          const grad = ctx.createRadialGradient(sx, sy, 0, sx, sy, r * 8);
          grad.addColorStop(0, `rgba(167, 139, 250, ${st.fade * 0.3})`);
          grad.addColorStop(1, "rgba(139, 92, 246, 0)");
          ctx.fillStyle = grad;
          ctx.beginPath();
          ctx.arc(sx, sy, r * 8, 0, Math.PI * 2);
          ctx.fill();

          // Core
          ctx.fillStyle = `rgba(255, 255, 255, ${op})`;
          ctx.beginPath();
          ctx.arc(sx, sy, r, 0, Math.PI * 2);
          ctx.fill();
        }
      }

      rafRef.current = requestAnimationFrame(draw);
    };

    rafRef.current = requestAnimationFrame(draw);
    window.addEventListener("resize", resize);
    return () => {
      if (rafRef.current) cancelAnimationFrame(rafRef.current);
      window.removeEventListener("resize", resize);
    };
  }, [starfield, loading]);

  return (
    <canvas
      ref={canvasRef}
      style={{
        position: "fixed", top: 0, left: 0,
        width: "100%", height: "100%",
        pointerEvents: "none",
        zIndex: 1,
      }}
    />
  );
}
