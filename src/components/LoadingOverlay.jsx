import { motion, AnimatePresence } from "framer-motion";
import { Loader2 } from "lucide-react";

export function LoadingOverlay({ title = "Loading", detail = "" }) {
  return (
    <AnimatePresence>
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        className="fixed inset-0 z-50 flex items-center justify-center"
        style={{ background: "rgba(0,0,0,0.72)", backdropFilter: "blur(4px)" }}
      >
        <motion.div
          initial={{ scale: 0.9, opacity: 0 }}
          animate={{ scale: 1, opacity: 1 }}
          exit={{ scale: 0.9, opacity: 0 }}
          className="glass flex items-center gap-4 px-6 py-4 rounded-xl shadow-lg"
        >
          <Loader2 size={22} className="animate-spin text-accent" />
          <div>
            <h2 className="text-base font-semibold text-text m-0">{title}</h2>
            {detail && <p className="text-xs text-muted mt-1 m-0">{detail}</p>}
          </div>
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );
}