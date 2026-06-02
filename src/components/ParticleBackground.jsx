import { motion, AnimatePresence } from "framer-motion";
import { useMemo, useState, useEffect, useRef } from "react";

const COLORS = [
  "rgba(139,92,246,X)",
  "rgba(217,70,239,X)",
  "rgba(34,211,238,X)",
  "rgba(139,92,246,X)",
  "rgba(167,139,250,X)",
];

const IDLE_OPACITIES = [0.18, 0.13, 0.10, 0.13, 0.09];
const GALAXY_OPACITIES = [0.22, 0.16, 0.12, 0.16, 0.10];

function generateParticles(count = 15) {
  // Spread positions across full viewport with some clustering
  const gridCols = 4;
  const gridRows = 4;
  return Array.from({ length: count }, (_, i) => {
    const angle = i * 2.399963;
    const radius = Math.sqrt(i + 1) * 7;
    // Use grid-based spread so orbs don't all clump center
    const cellX = (i % gridCols) / gridCols;
    const cellY = Math.floor(i / gridCols) / gridRows;
    return {
      id: i,
      size: 60 + Math.random() * 120,
      x: (cellX * 100) + (Math.random() - 0.5) * 25,
      y: (cellY * 100) + (Math.random() - 0.5) * 25,
      colorIdx: i % COLORS.length,
      galaxyX: Math.cos(angle) * radius,
      galaxyY: Math.sin(angle) * radius,
      duration: 20 + Math.random() * 35,
      driftDelay: Math.random() * 15,
      driftX: (Math.random() - 0.5) * 150,
      driftY: (Math.random() - 0.5) * 150,
    };
  });
}

function generateStars(count = 80) {
  return Array.from({ length: count }, (_, i) => ({
    id: i,
    x: Math.random() * 100,
    y: Math.random() * 100,
    size: 1 + Math.random() * 3,
    opacity: 0.15 + Math.random() * 0.35,
    twinkleDelay: Math.random() * 2,
    twinkleDur: 1.5 + Math.random() * 3,
  }));
}

export function ParticleBackground({ thinking = false }) {
  const particles = useMemo(() => generateParticles(15), []);
  const stars = useMemo(() => generateStars(80), []);
  const [galaxyActive, setGalaxyActive] = useState(false);
  const holdTimer = useRef(null);

  useEffect(() => {
    if (thinking) {
      clearTimeout(holdTimer.current);
      setGalaxyActive(true);
    } else {
      // Hold galaxy for 1.5s after thinking stops so animation is visible
      holdTimer.current = setTimeout(() => setGalaxyActive(false), 1500);
    }
    return () => clearTimeout(holdTimer.current);
  }, [thinking]);

  return (
    <div className="fixed inset-0 pointer-events-none overflow-hidden" style={{ zIndex: galaxyActive ? 50 : 0 }}>
      {/* Galaxy overlay — fades in/out on top of idle orbs */}
      <AnimatePresence>
        {galaxyActive && (
          <motion.div
            key="galaxy"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.6 }}
            className="absolute inset-0"
          >
          {/* Dim twinkling stars */}
          {stars.map((s) => (
            <motion.div
              key={`s-${s.id}`}
              className="absolute rounded-full bg-white"
              style={{
                width: s.size, height: s.size,
                left: `${s.x}%`, top: `${s.y}%`,
              }}
              initial={{ opacity: 0 }}
              animate={{ opacity: [s.opacity * 0.2, s.opacity, s.opacity * 0.2] }}
              transition={{ duration: s.twinkleDur, delay: s.twinkleDelay, repeat: Infinity, ease: "easeInOut" }}
            />
          ))}

          {/* Rotating spiral galaxy */}
          <motion.div
            className="absolute"
            style={{ left: "50%", top: "50%" }}
            animate={{ rotate: 360 }}
            transition={{ duration: 40, repeat: Infinity, ease: "linear" }}
          >
            {particles.map((p) => (
              <motion.div
                key={p.id}
                className="absolute rounded-full"
                style={{
                  width: p.size * 0.8, height: p.size * 0.8,
                  background: COLORS[p.colorIdx].replace("X", String(GALAXY_OPACITIES[p.colorIdx])),
                  filter: "blur(50px)",
                }}
                initial={{ opacity: 0, scale: 0.3 }}
                animate={{ x: `${p.galaxyX}%`, y: `${p.galaxyY}%`, opacity: 1, scale: 1 }}
                transition={{ duration: 0.6, delay: p.driftDelay * 0.15, ease: "easeOut" }}
              />
            ))}
          </motion.div>
        </motion.div>
      )}
      </AnimatePresence>

      {/* Idle drifting orbs — always visible, fade under galaxy */}
      <motion.div
        animate={{ opacity: galaxyActive ? 0.2 : 1 }}
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
              filter: "blur(40px)",
            }}
            animate={{
              x: [0, p.driftX, -p.driftX * 0.8, p.driftX * 0.5, 0],
              y: [0, -p.driftY * 0.7, p.driftY, -p.driftY * 0.5, 0],
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