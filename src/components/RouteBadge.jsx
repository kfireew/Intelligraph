import { Boxes, Search, ArrowUp, ArrowDown, Zap, Lightbulb, HelpCircle, FlaskConical } from "lucide-react";

const iconMap = {
  architecture: Boxes,
  search: Search,
  callers: ArrowUp,
  callees: ArrowDown,
  impact: Zap,
  how_works: Lightbulb,
  what_is: HelpCircle,
  coverage: FlaskConical,
};

const colorMap = {
  architecture: "text-emerald-400 bg-emerald-400/10",
  search: "text-accent-light bg-accent/10",
  callers: "text-cyan-400 bg-cyan-400/10",
  callees: "text-cyan-400 bg-cyan-400/10",
  impact: "text-orange bg-orange/10",
  how_works: "text-purple-400 bg-purple-400/10",
  what_is: "text-accent-light bg-accent/10",
  coverage: "text-green bg-green/10",
};

export function RouteBadge({ route }) {
  if (!route) return null;
  const category = route?.category || "search";
  const label = route?.label || category;
  const Icon = iconMap[category] || Search;
  const color = colorMap[category] || colorMap.search;

  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-[11px] font-bold whitespace-nowrap ${color}`}>
      <Icon size={10} />
      {label}
    </span>
  );
}