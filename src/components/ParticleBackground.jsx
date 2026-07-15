import { motion, AnimatePresence } from "framer-motion";
import { useMemo, useState, useEffect, useRef } from "react";

const COLORS = [
  "rgba(139,92,246,X)",
  "rgba(217,70,239,X)",
  "rgba(34,211,238,X)",
  "rgba(139,92,246,X)",
  "rgba(167,139,250,X)",
];

const IDLE_OPACITIES = [0.10, 0.08, 0.06, 0.08, 0.05];

function generateParticles(count = 10) {
  return Array.from({ length: count }, (_, i) => ({
    id: i,
    size: 80 + Math.random() * 140,
    x: Math.random() * 100,
    y: Math.random() * 100,
    colorIdx: i % COLORS.length,
    duration: 30 + Math.random() * 40,
    driftDelay: Math.random() * 15,
    driftX: (Math.random() - 0.5) * 120,
    driftY: (Math.random() - 0.5) * 120,
  }));
}

export function ParticleBackground({ thinking = false }) {
  const particles = useMemo(() => generateParticles(10), []);
  const [dimmed, setDimmed] = useState(false);
  const holdTimer = useRef(null);

  useEffect(() => {
    if (thinking) {
      clearTimeout(holdTimer.current);
      setDimmed(true);
    } else {
      holdTimer.current = setTimeout(() => setDimmed(false), 1500);
    }
    return () => clearTimeout(holdTimer.current);
  }, [thinking]);

  return (
    <div className="fixed inset-0 pointer-events-none overflow-hidden" style={{ zIndex: 0 }}>
      <motion.div
        animate={{ opacity: dimmed ? 0.15 : 0.35 }}
        transition={{ duration: 0.8, ease: "easeInOut" }}
        className="absolute inset-0"
      >
        {particles.map((p) => (
          <motion.div
            key={p.id}
            className="absolute rounded-full"
            style={{
              width: p.size, height: p.size,
              left: `${p.x}%`, top: `${p.y}%`,
              background: COLORS[p.colorIdx].replace("X", String(IDLE_OPACITIES[p.colorIdx])),
              filter: "blur(60px)",
            }}
            animate={{
              x: [0, p.driftX, -p.driftX * 0.7, p.driftX * 0.4, 0],
              y: [0, -p.driftY * 0.6, p.driftY, -p.driftY * 0.4, 0],
            }}
            transition={{
              duration: p.duration,
              delay: p.driftDelay,
              repeat: Infinity,
              ease: "linear",
            }}
          />
        ))}
      </motion.div>
    </div>
  );
}
