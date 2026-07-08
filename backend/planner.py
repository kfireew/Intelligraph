"""
ExecutionPlanner — Decomposes user query into a structured task plan.

Takes a natural language query, detects intents, and produces
a prioritized list of tasks. Each task carries its own policy
(compression, depth, operations) independent of other tasks.

Input:  prompt
Output: { tasks: [{ id, type, target, depth, compression, operations }, ...] }
"""

import re

_INTENT_PATTERNS = [
    (r"test|coverage", "tests"),
    (r"impact|blast radius|what breaks|what is affected|what would break|affect.*chang", "impact"),
    (r"who calls|what calls|who uses|what uses|who imports|depends on|callers of", "callers"),
    (r"what does.*(?:call|use|import|depend on)|callees", "callees"),
    (r"architecture|structure|overview|how is.*organized|how.*structured|components|explain.*project|tell.*about.*project|describe.*project|communities", "architecture"),
    (r"^how|explain how|how.*(?:work|used|called|defined|implement)", "how_works"),
    (r"\bnx\b.*|workspace\s+(?:project|app|lib|architectur)|affected\s+(?:project|app|lib)|targets?\s+(?:for|of|in)\s+.*|project\s+depend(?:s|ency|encies)|where (?:should|would|can) (?:i add|put|create|place)|project.json|nx\.json", "nx_architecture"),
]

_WHAT_IS_PATTERNS = [r"^what is", r"^where is", r"^which file", r"^find the"]

def detect_intent(prompt: str) -> dict:
    """Detect query intent. Returns {intent, target}."""
    lower = (prompt or "").lower()
    for pattern, intent in _INTENT_PATTERNS:
        if re.search(pattern, lower):
            return {"intent": intent, "target": lower}
    for pattern in _WHAT_IS_PATTERNS:
        if re.search(pattern, lower):
            return {"intent": "what_is", "target": lower}
    return {"intent": "what_is", "target": lower}

# ── Live Nx detection (for Nx MCP bridge) ──
# These patterns identify questions that require live Nx CLI tooling,
# not static workspace metadata. Static Nx questions use nx_architecture.

_LIVE_NX_PATTERNS = [
    r"what target|which target|run what|show targets|available targets",
    r"what (command|generator|scaffold)",
    r"affected|what would be affected",
    r"npx nx|nx help|show local nx",
    r"list generators|what generators",
]


def detect_live_nx_question(prompt: str) -> bool:
    """Check if a prompt is asking for live Nx tooling (not static metadata)."""
    lower = (prompt or "").lower()
    # Live Nx questions are usually about running/using nx, not about architecture
    for pattern in _LIVE_NX_PATTERNS:
        if re.search(pattern, lower):
            return True
    return False

from retrieval import task_policy

# Known compound patterns that produce multiple tasks
_COMPOUND_PATTERNS = [
    # "how X works and what breaks if Y"
    (r".*(?:how|explain)\s+.*(?:and|also|then).*(?:what\s+breaks|impact|affect).*", ["how_works", "impact"]),
    # "architecture and security of X"
    (r".*(?:architecture|structure|overview).*(?:and|also).*(?:security|safety|risk).*", ["architecture", "security"]),
    # "what is X and how does it work"
    (r".*(?:what\s+is|find|where).*(?:and|also).*(?:how|explain).*", ["what_is", "how_works"]),
    # "find the bug in X and fix it"
    (r".*(?:bug|error|issue|problem).*(?:and|also|then).*(?:fix|patch|solve|resolve).*", ["debug", "refactor"]),
]

# Single-intent extraction per task type
_TASK_EXTRACTORS = {
    "architecture":  r"(?:architecture|structure|overview|components|organization|design)\s*(?:of|for)?\s*(.+)?",
    "how_works":     r"(?:how|explain).*(?:does|work|implement|called|used)\s*(?:(?:the|a|an)\s+)?(.+)",
    "what_is":       r"(?:what|where|which|find|show)\s+(?:is|are|the|a|an|file|function|class)\s*(.+)?",
    "impact":        r"(?:impact|what breaks|affect|blast radius|risk)\s*(?:of|on|for|if|when)?\s*(.+)?",
    "callers":       r"(?:who|what)\s*(?:calls|uses|imports|depends on)\s*(.+)?",
    "callees":       r"(?:what does|what are)\s*(.+?)\s*(?:call|use|import|depend on)",
    "debug":         r"(?:bug|error|issue|problem|debug|trace)\s*(?:in|of|with)?\s*(.+)?",
    "refactor":      r"(?:refactor|rewrite|improve|optimize)\s*(.+)?",
    "nx_architecture": r"(?:nx|workspace|project|app|lib)\s*(?:architectur|structure|layout|organized|depend|target)?\s*(.+)?",
    "security":      r"(?:security|vulnerability|exploit|injection|xss|csrf)\s*(?:in|of|for)?\s*(.+)?",
    "tests":         r"(?:test|coverage|spec|unit|integration)\s*(?:for|of|in)?\s*(.+)?",
}


def extract_target(prompt: str, task_type: str) -> str:
    """Extract a target symbol or phrase from prompt for a task type."""
    pattern = _TASK_EXTRACTORS.get(task_type)
    if not pattern:
        return prompt[:80].rstrip("?").strip()
    m = re.search(pattern, prompt, re.IGNORECASE)
    if m and m.group(1) and m.group(1).strip().rstrip("?").strip():
        return m.group(1).strip().rstrip("?").strip()[:80]
    # Fallback: try to extract the noun phrase between "how does" and "work"
    if task_type == "how_works":
        m2 = re.search(r"(?:how|explain)\s+(?:does\s+)?(?:the\s+|a\s+|an\s+)?(.+?)(?:\s+work|\?)", prompt, re.IGNORECASE)
        if m2 and m2.group(1).strip():
            return m2.group(1).strip()[:80]
    return prompt[:80].rstrip("?").strip()


def plan_query(prompt: str) -> dict:
    """Decompose query into task plan.
    
    Returns { tasks: [{ id, type, target, depth, compression, operations }] }
    """
    prompt_lower = prompt.lower()

    # 1. Check for compound patterns first
    for pattern, intents in _COMPOUND_PATTERNS:
        if re.search(pattern, prompt_lower):
            tasks = []
            for i, intent in enumerate(intents):
                policy = task_policy(intent)
                target = extract_target(prompt, intent)
                tasks.append({
                    "id": i + 1,
                    "type": intent,
                    "target": target,
                    "depth": policy["depth"],
                    "compression": policy["compression"],
                    "operations": _operations_for(intent),
                    "requires_live_nx": False,
                    "nx_capability": None,
                })
            return {"tasks": tasks}

    # 1b. Check for live Nx question
    is_live_nx = detect_live_nx_question(prompt)
    if is_live_nx:
        nx_cap = None
        lower = prompt.lower()
        if "affected" in lower:
            nx_cap = "affected"
        elif "generator" in lower:
            nx_cap = "generator_info"
        elif "target" in lower or "run" in lower:
            nx_cap = "task_info"
        elif "help" in lower or "status" in lower:
            nx_cap = "status"
        policy = task_policy("nx_architecture")
        return {"tasks": [{
            "id": 1,
            "type": "how_works",
            "target": prompt[:80],
            "depth": 1,
            "compression": "none",
            "operations": [],
            "requires_live_nx": True,
            "nx_capability": nx_cap,
        }]}

    intent_info = detect_intent(prompt)
    intent = intent_info["intent"]
    policy = task_policy(intent)
    target = extract_target(prompt, intent)

    return {
        "tasks": [{
            "id": 1,
            "type": intent,
            "target": target,
            "depth": policy["depth"],
            "compression": policy["compression"],
            "operations": _operations_for(intent),
            "requires_live_nx": False,
            "nx_capability": None,
        }]
    }


def _operations_for(task_type: str) -> list:
    """Default graph operations per task type."""
    ops = {
        "nx_architecture": ["find_nx_projects", "expand_nx_deps"],
        "architecture": ["find_community_hubs", "expand_neighbors"],
        "how_works":    ["find_symbols", "expand_callers", "expand_callees"],
        "what_is":      ["find_symbols"],
        "impact":       ["find_symbols", "incoming_callers"],
        "callers":      ["find_symbols", "incoming_callers"],
        "callees":      ["find_symbols", "outgoing_callers"],
        "debug":        ["find_symbols", "expand_callers", "expand_callees"],
        "refactor":     ["find_symbols", "expand_callers", "expand_callees", "incoming_callers"],
        "security":     ["find_symbols", "incoming_callers", "expand_callers"],
        "tests":        ["find_symbols", "expand_callers"],
    }
    return ops.get(task_type, ["find_symbols"])