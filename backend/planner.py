"""
ExecutionPlanner — Decomposes user query into a structured task plan.

Takes a natural language query, detects intents, and produces
a prioritized list of tasks. Each task carries its own policy
(compression, depth, operations) independent of other tasks.

Input:  prompt
Output: { tasks: [{ id, type, target, depth, compression, operations }, ...] }

Intent routing uses semantic-router (embedding-based) when available,
falling back to regex patterns if the model is not loaded.
"""

import re

# ── Regex patterns (fallback when semantic-router unavailable) ──

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


def _OLD_detect_intent(prompt: str) -> dict:
    """Regex-based intent detection. Used as fallback."""
    lower = (prompt or "").lower()
    for pattern, intent in _INTENT_PATTERNS:
        if re.search(pattern, lower):
            return {"intent": intent, "target": lower}
    for pattern in _WHAT_IS_PATTERNS:
        if re.search(pattern, lower):
            return {"intent": "what_is", "target": lower}
    return {"intent": "what_is", "target": lower}


# Known compound patterns that produce multiple tasks (regex fallback)
_COMPOUND_PATTERNS = [
    (r".*(?:how|explain)\s+.*(?:and|also|then).*(?:what\s+breaks|impact|affect).*", ["how_works", "impact"]),
    (r".*(?:architecture|structure|overview).*(?:and|also).*(?:security|safety|risk).*", ["architecture", "security"]),
    (r".*(?:what\s+is|find|where).*(?:and|also).*(?:how|explain).*", ["what_is", "how_works"]),
    (r".*(?:bug|error|issue|problem).*(?:and|also|then).*(?:fix|patch|solve|resolve).*", ["debug", "refactor"]),
]

# Single-intent extraction per task type (regex fallback)
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


def _OLD_extract_target(prompt: str, task_type: str) -> str:
    """Regex-based target extraction. Used as fallback."""
    pattern = _TASK_EXTRACTORS.get(task_type)
    if not pattern:
        return prompt[:80].rstrip("?").strip()
    m = re.search(pattern, prompt, re.IGNORECASE)
    if m and m.group(1) and m.group(1).strip().rstrip("?").strip():
        return m.group(1).strip().rstrip("?").strip()[:80]
    if task_type == "how_works":
        m2 = re.search(r"(?:how|explain)\s+(?:does\s+)?(?:the\s+|a\s+|an\s+)?(.+?)(?:\s+work|\?)", prompt, re.IGNORECASE)
        if m2 and m2.group(1).strip():
            return m2.group(1).strip()[:80]
    return prompt[:80].rstrip("?").strip()


# ── Public API (preserved for backward compat) ──

def detect_intent(prompt: str) -> dict:
    """Detect query intent. Returns {intent, target}.

    Tries semantic-router first, falls back to regex.
    """
    routes = _semantic_route(prompt)
    if routes:
        return {"intent": routes[0]["intent"], "target": routes[0]["target"]}
    return _OLD_detect_intent(prompt)


def extract_target(prompt: str, task_type: str) -> str:
    """Extract a target symbol or phrase from prompt for a task type.

    Uses semantic-router target if available, falls back to regex.
    """
    routes = _semantic_route(prompt)
    for r in routes:
        if r["intent"] == task_type and r.get("target"):
            return r["target"]
    return _OLD_extract_target(prompt, task_type)


# ── Live Nx detection (stays regex — very specific patterns) ──

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
    for pattern in _LIVE_NX_PATTERNS:
        if re.search(pattern, lower):
            return True
    return False


from retrieval import task_policy

# ── Semantic router integration ──

_semantic_cache = {}


def _semantic_route(prompt: str) -> list[dict]:
    """Try semantic-router, return list of {intent, target, score} or []."""
    if prompt in _semantic_cache:
        return _semantic_cache[prompt]
    try:
        from semantic_planner import route_query
        results = route_query(prompt)
        if results:
            _semantic_cache[prompt] = results
            return results
    except Exception:
        pass
    return []


def plan_query(prompt: str) -> dict:
    """Decompose query into task plan.

    Uses semantic-router for intent detection + target extraction.
    Falls back to regex patterns if semantic-router is unavailable.

    Returns { tasks: [{ id, type, target, depth, compression, operations }] }
    """
    # Try semantic-router first
    routes = _semantic_route(prompt)

    if routes:
        tasks = []
        for i, r in enumerate(routes):
            intent = r["intent"]
            policy = task_policy(intent)
            target = r.get("target") or _OLD_extract_target(prompt, intent)
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

    # ── Regex fallback ──
    prompt_lower = prompt.lower()

    # 1. Check for compound patterns first
    for pattern, intents in _COMPOUND_PATTERNS:
        if re.search(pattern, prompt_lower):
            tasks = []
            for i, intent in enumerate(intents):
                policy = task_policy(intent)
                target = _OLD_extract_target(prompt, intent)
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

    intent_info = _OLD_detect_intent(prompt)
    intent = intent_info["intent"]
    policy = task_policy(intent)
    target = _OLD_extract_target(prompt, intent)

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
