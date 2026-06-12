import { motion } from "framer-motion";

export function AppShell({ children }) {
  return (
    <motion.main
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      transition={{ duration: 0.4 }}
      className="h-screen flex overflow-hidden relative"
    >
      {children}
    </motion.main>
  );
}