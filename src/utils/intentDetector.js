// Single intent detector — client side only. No server-side intent.
const INTENTS = [
  [/test|coverage/i, "tests"],
  [/impact|blast radius|what breaks|what is affected|what would break|affect.*chang/i, "impact"],
  [/who calls|what calls|who uses|what uses|who imports|depends on|callers of/i, "callers"],
  [/what does.*(?:call|use|import|depend on)|callees/i, "callees"],
  [/architecture|structure|overview|how is.*organized|how.*structured|components|explain.*project|tell.*about.*project|describe.*project|communities/i, "architecture"],
  [/^how|explain how|how.*(?:work|used|called|defined|implement)/i, "how_works"],
];

const WHAT_IS_PATTERNS = [/^what is/i, /^where is/i, /^which file/i, /^find the/i];

export function detectIntent(msg) {
  const lower = (msg || "").toLowerCase();
  for (const [re, intent] of INTENTS) {
    if (re.test(lower)) return { intent, target: lower };
  }
  for (const re of WHAT_IS_PATTERNS) {
    if (re.test(lower)) return { intent: "what_is", target: lower };
  }
  return { intent: "what_is", target: lower };
}
