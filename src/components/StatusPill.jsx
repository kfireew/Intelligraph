export function StatusPill({ tone = "neutral", children }) {
  const tones = {
    neutral: "bg-white/6 text-muted",
    success: "bg-green/10 text-green",
    warning: "bg-orange/10 text-orange",
    error: "bg-red/10 text-red",
    info: "bg-accent/10 text-accent-light",
  };
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-bold uppercase tracking-wider ${tones[tone] || tones.neutral}`}>
      {children}
    </span>
  );
}