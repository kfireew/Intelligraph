"""
semantic_planner.py — Embedding-based intent routing + target extraction.

Replaces the regex catalog in planner.py with a semantic vector space approach
that handles multi-phrased questions, unusual wording, and synonyms.

Uses only the bundled all-MiniLM-L6-v2 model (23MB). No external LLM needed.

Two-stage pipeline:
  1. Intent routing: RouteLayer with template utterances per intent
  2. Target extraction: CRG FTS (when available) or embedding similarity
     against graphify node names (fallback)

The encoder and route layer are lazily initialized on first use and cached.
A regex fallback is used if the model or library is unavailable.
"""

import logging
import os
import re
import sys

import numpy as np

log = logging.getLogger(__name__)

_VERBOSE = os.environ.get("INTELLIGRAPH_VERBOSE", "true").lower() == "true"

_MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "all-MiniLM-L6-v2")
_SCORE_THRESHOLD = 0.25

_router_instance = None
_encoder_instance = None
_init_error = None


def _vmsg(msg, *args):
    if not _VERBOSE:
        return
    from datetime import datetime
    ts = datetime.now().strftime("%H:%M:%S")
    if args:
        try:
            msg = msg % args
        except Exception:
            pass
    print(f"[{ts}] {msg}", flush=True)


# ── Route definitions ────────────────────────────────────────────

def _build_routes():
    from semantic_router import Route

    routes = [
        Route(
            name="architecture",
            utterances=[
                "give me an overview of the codebase",
                "how is the project organized",
                "explain the architecture",
                "what are the main components",
                "describe the project structure",
                "what does the system look like overall",
                "how is the codebase structured",
                "show me the high-level design",
                "what is the overall layout",
                "help me understand the project architecture",
                "what modules exist in this codebase",
                "how is the code organized",
                "what is the architecture of this system",
                "describe the system architecture",
                "what is the overall architecture",
                "explain the system design",
                "architecture of the parser module",
                "architecture of the clustering component",
                "what is the structure of the retrieval system",
                "describe the layout of the codebase",
            ],
            score_threshold=_SCORE_THRESHOLD,
        ),
        Route(
            name="how_works",
            utterances=[
                "how does the parser work",
                "walk me through the flow",
                "explain the implementation",
                "what's the execution path",
                "how is this function used",
                "how does the data flow through the system",
                "can you explain how this module operates",
                "what happens when this function is called",
                "how does this feature work",
                "explain the logic behind this",
                "what is the algorithm used here",
                "how is this component implemented",
                "trace the execution flow",
                "what does the bridge module do",
                "what does this component do",
                "how does this system operate",
                "how does the extraction pipeline work",
                "explain how the retrieval system works",
                "how does the MCP server work",
            ],
            score_threshold=_SCORE_THRESHOLD,
        ),
        Route(
            name="impact",
            utterances=[
                "what breaks if I change this function",
                "blast radius of modifying this",
                "what depends on this module",
                "impact of changing this component",
                "what would be affected by this change",
                "who is impacted by this modification",
                "what are the downstream effects",
                "what will break if I refactor this",
                "if I modify this what else changes",
                "what code relies on this",
                "what is the ripple effect of changing this",
                "what code depends on this being stable",
                "what would break if this changed",
                "what code relies on the config module",
                "what depends on calling this",
            ],
            score_threshold=_SCORE_THRESHOLD,
        ),
        Route(
            name="what_is",
            utterances=[
                "where is this function defined",
                "find the file containing this class",
                "which file has this symbol",
                "what is this variable",
                "where can I find the definition",
                "locate the implementation of this",
                "show me where this is declared",
                "find this symbol in the codebase",
                "where does this come from",
                "what does this refer to",
                "find the declaration of",
                "which module defines this",
                "where is the entry point defined",
                "locate where this is defined",
                "find the definition of",
            ],
            score_threshold=_SCORE_THRESHOLD,
        ),
        Route(
            name="callers",
            utterances=[
                "who calls this function",
                "what uses this method",
                "find all invocations of this",
                "who are the callers of this",
                "what code references this symbol",
                "where is this function invoked",
                "show me all callers of this",
                "what depends on calling this",
                "who triggers this function",
                "what invokes this method",
                "find all references to this function",
                "what triggers this",
                "who initiates this call",
            ],
            score_threshold=_SCORE_THRESHOLD,
        ),
        Route(
            name="callees",
            utterances=[
                "what does this function call",
                "what does this method use",
                "what does this depend on",
                "what does this import",
                "what are the callees of this",
                "what functions does this invoke",
                "what does this module rely on",
                "what services does this use",
                "what does this component interact with",
            ],
            score_threshold=_SCORE_THRESHOLD,
        ),
        Route(
            name="tests",
            utterances=[
                "find tests for this module",
                "show test coverage",
                "where are the unit tests",
                "what tests cover this code",
                "find the test file for this",
                "are there any tests for this",
                "show me the spec file",
                "what integration tests exist",
                "find test cases for",
                "show me the test suite",
                "what is covered by tests",
            ],
            score_threshold=_SCORE_THRESHOLD,
        ),
        Route(
            name="debug",
            utterances=[
                "trace this bug",
                "find the error in this code",
                "debug this issue",
                "something is wrong with this function",
                "why is this not working",
                "help me find the root cause",
                "trace the error path",
                "where is the bug in",
                "investigate this exception",
                "what's causing this failure",
            ],
            score_threshold=_SCORE_THRESHOLD,
        ),
        Route(
            name="refactor",
            utterances=[
                "how to refactor this",
                "improve the code in this module",
                "optimize this function",
                "clean up this code",
                "simplify this implementation",
                "what's the better way to write this",
                "how can I restructure this",
                "modernize this code",
                "reduce complexity in this",
            ],
            score_threshold=_SCORE_THRESHOLD,
        ),
        Route(
            name="security",
            utterances=[
                "security vulnerabilities in this",
                "is this vulnerable to injection",
                "check for security issues",
                "what are the security risks",
                "is this code safe",
                "find potential security flaws",
                "check for XSS or CSRF",
                "are there any exploits in this",
                "security audit of this module",
            ],
            score_threshold=_SCORE_THRESHOLD,
        ),
        Route(
            name="nx_architecture",
            utterances=[
                "nx workspace structure",
                "what nx projects exist in the monorepo",
                "how is the nx workspace organized",
                "show me the nx monorepo layout",
                "what are the nx project boundaries",
                "nx project graph and dependencies",
                "nx workspace dependency graph",
                "which nx projects depend on each other",
                "show me the nx project configuration",
            ],
            score_threshold=_SCORE_THRESHOLD,
        ),
    ]

    return routes


# ── Embedding-based target extraction ────────────────────────────

class EmbeddingTargetExtractor:
    """Extracts target symbol from query by embedding similarity against
    graphify node names.

    Builds an embedding index of meaningful node names (Function, Class)
    from the graphify graph. When queried, embeds the question and finds
    the closest node name by cosine similarity.

    For 10K nodes: ~15MB in RAM, <50ms per query.
    """

    def __init__(self):
        self._indices = {}  # proj_id -> (names_array, embeddings_array)

    def get_target(self, query: str, graphify_data: dict, proj_id=None) -> str | None:
        """Extract target symbol from query via embedding similarity.

        Args:
            query: The user's natural language question
            graphify_data: The project's graphify graph data
            proj_id: Optional project ID for caching

        Returns:
            Best matching node name, or None if no good match.
        """
        encoder = _get_encoder()
        if encoder is None:
            return None

        names, embeddings = self._get_or_build_index(graphify_data, proj_id, encoder)
        if names is None or len(names) == 0:
            return None

        # Embed the query
        q_emb = encoder.encode([query], show_progress_bar=False, convert_to_numpy=True)[0]

        # Cosine similarity (embeddings are already normalized by sentence-transformers)
        scores = embeddings @ q_emb
        top_idx = int(np.argmax(scores))
        top_score = float(scores[top_idx])
        top_name = str(names[top_idx])

        # Clean up: if name is a long sentence (documentation node), skip it
        if len(top_name) > 60 or " " in top_name:
            # Try to find a shorter, symbol-like name with decent score
            for idx in np.argsort(scores)[::-1][:10]:
                candidate = str(names[idx])
                if len(candidate) <= 60 and " " not in candidate:
                    top_name = candidate
                    top_score = float(scores[idx])
                    break
            else:
                _vmsg("EMBED TARGET: query='%s' only long names found, skipping", query[:50])
                return None

        # Threshold: below 0.3 is too weak to trust
        if top_score < 0.3:
            _vmsg("EMBED TARGET: query='%s' best='%s' score=%.2f (below threshold)", query[:50], top_name, top_score)
            return None

        _vmsg("EMBED TARGET: query='%s' -> '%s' (score=%.2f)", query[:50], top_name, top_score)
        return top_name

    def _get_or_build_index(self, graphify_data: dict, proj_id, encoder):
        """Get cached index or build a new one."""
        cache_key = proj_id or id(graphify_data)

        if cache_key in self._indices:
            return self._indices[cache_key]

        # Build index from graphify node names
        nodes = graphify_data.get("nodes", [])
        if not nodes:
            self._indices[cache_key] = (None, None)
            return None, None

        # Filter to meaningful names — symbol-like, not documentation
        _GENERIC_NAMES = {
            "main", "init", "run", "config", "module", "server", "client",
            "handler", "manager", "base", "core", "utils", "helper",
            "test", "setup", "teardown", "fixture", "mock",
        }
        _CODE_EXTENSIONS = {".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go",
                            ".rs", ".rb", ".php", ".c", ".cpp", ".h", ".cs",
                            ".scala", ".kt", ".swift", ".md", ".txt", ".json",
                            ".yaml", ".yml", ".toml", ".xml", ".html", ".css",
                            ".sh", ".sql", ".lua", ".jl"}
        names = []
        seen = set()
        for n in nodes:
            name = n.get("label") or n.get("id") or ""
            name_str = str(name)
            name_lower = name_str.lower()
            # Skip: too short, private, test, generic, documentation, files
            if (len(name_str) <= 2
                    or name_str.startswith("_")
                    or name_str.startswith("test_")
                    or name_str in seen
                    or name_lower in _GENERIC_NAMES
                    or " " in name_str
                    or len(name_str) > 60
                    # Skip file names (have extensions)
                    or any(name_lower.endswith(ext) for ext in _CODE_EXTENSIONS)
                    # Skip names that are just paths
                    or "/" in name_str or "\\" in name_str):
                continue
            seen.add(name_str)
            names.append(name_str)

        if not names:
            self._indices[cache_key] = (None, None)
            return None, None

        # Embed all names
        embeddings = encoder.encode(names, show_progress_bar=False, convert_to_numpy=True)

        self._indices[cache_key] = (np.array(names), embeddings)
        _vmsg("EMBED TARGET: built index for proj=%s, %d names", cache_key, len(names))
        return self._indices[cache_key]


_target_extractor = EmbeddingTargetExtractor()


# ── Initialization ───────────────────────────────────────────────

_encoder_error = None

def _get_encoder():
    """Lazily initialize the sentence transformer encoder. Returns encoder or None."""
    global _encoder_instance, _encoder_error
    if _encoder_instance is not None:
        return _encoder_instance
    if _encoder_error is not None:
        return None

    try:
        os.environ["TRANSFORMERS_VERBOSITY"] = "error"
        os.environ["TOKENIZERS_PARALLELISM"] = "false"

        from sentence_transformers import SentenceTransformer

        if not os.path.isdir(_MODEL_DIR):
            _encoder_error = f"Model dir not found: {_MODEL_DIR}"
            _vmsg("SEMANTIC PLANNER: %s", _encoder_error)
            return None

        _vmsg("SEMANTIC PLANNER: loading encoder from %s", _MODEL_DIR)
        _encoder_instance = SentenceTransformer(_MODEL_DIR)
        _vmsg("SEMANTIC PLANNER: encoder ready (dim=%d)", _encoder_instance.get_embedding_dimension())
        return _encoder_instance
    except Exception as e:
        _encoder_error = str(e)
        _vmsg("SEMANTIC PLANNER: encoder init failed: %s", e)
        log.warning("Encoder init failed: %s", e, exc_info=True)
        return None


def _init_router():
    """Lazily initialize the semantic router. Returns router or None."""
    global _router_instance, _init_error
    if _router_instance is not None:
        return _router_instance
    if _init_error is not None:
        return None

    try:
        from semantic_router import RouteLayer
        from semantic_router.encoders import HuggingFaceEncoder

        if not os.path.isdir(_MODEL_DIR):
            _init_error = f"Model dir not found: {_MODEL_DIR}"
            return None

        _vmsg("SEMANTIC PLANNER: loading encoder from %s", _MODEL_DIR)
        encoder = HuggingFaceEncoder(
            name=_MODEL_DIR,
            model_kwargs={"local_files_only": True},
        )
        encoder.score_threshold = _SCORE_THRESHOLD

        routes = _build_routes()
        _router_instance = RouteLayer(encoder=encoder, routes=routes)
        _vmsg("SEMANTIC PLANNER: ready (%d routes, threshold=%.2f)", len(routes), _SCORE_THRESHOLD)
        return _router_instance
    except Exception as e:
        _init_error = str(e)
        _vmsg("SEMANTIC PLANNER: router init failed: %s", e)
        log.warning("Semantic router init failed: %s", e, exc_info=True)
        return None


# ── Clause splitting for compound queries ────────────────────────

_SPLIT_PATTERN = re.compile(
    r"\b(?:and|also|then|plus|\+)\b",
    re.IGNORECASE,
)

def _split_clauses(prompt: str) -> list[str]:
    """Split compound queries into individual clauses."""
    parts = _SPLIT_PATTERN.split(prompt)
    return [p.strip() for p in parts if p.strip() and len(p.strip()) > 3]


# ── Main entry point ─────────────────────────────────────────────

def route_query(prompt: str, graphify_data: dict = None, proj_id=None) -> list[dict]:
    """Route a user prompt to one or more intents.

    Two-stage:
      1. Intent routing via embedding similarity (RouteLayer)
      2. Target extraction via CRG FTS (if providers given) or
         embedding similarity against graphify node names (fallback)

    Args:
        prompt:         User's natural language question
        graphify_data:  Optional graphify graph data for embedding-based target extraction
        proj_id:        Optional project ID for index caching

    Returns:
        [{intent: str, target: str, score: float}, ...]
        Multiple entries for compound queries.
        Falls back to regex if semantic router is unavailable.
    """
    router = _init_router()
    if router is None:
        return _regex_fallback(prompt)

    # Route the full prompt first — produces better results for compound queries
    # like "how does map work and how can I add entities to it" where clause
    # splitting would route each half to a different (wrong) intent.
    # Only split if the full prompt doesn't route at all.
    full_result = _route_single(router, prompt, prompt, graphify_data, proj_id)
    if full_result:
        results = [full_result]
        _vmsg("SEMANTIC PLANNER: prompt=%r -> %s", prompt[:80], results)
        return results

    # Fall back to clause splitting for genuinely compound queries
    clauses = _split_clauses(prompt)
    if len(clauses) <= 1:
        clauses = [prompt]

    results = []
    seen_intents = set()
    for clause in clauses:
        result = _route_single(router, clause, prompt, graphify_data, proj_id)
        if result and result["intent"] not in seen_intents:
            results.append(result)
            seen_intents.add(result["intent"])

    if not results:
        return _regex_fallback(prompt)

    _vmsg("SEMANTIC PLANNER: prompt=%r -> %s", prompt[:80], results)
    return results


def _route_single(router, clause: str, full_prompt: str, graphify_data: dict, proj_id) -> dict | None:
    """Route a single clause. Returns {intent, target, score} or None."""
    try:
        choice = router(clause)
    except Exception as e:
        log.warning("Route failed for '%s': %s", clause[:50], e)
        return None

    intent = choice.name if choice else None
    score = choice.similarity_score if choice and choice.similarity_score else 0.0

    if not intent:
        return None

    # Target extraction: try CRG FTS first (via providers), then embedding fallback
    target = _extract_target(clause, intent, full_prompt, graphify_data, proj_id)

    return {
        "intent": intent,
        "target": target,
        "score": score,
    }


def _extract_target(clause: str, intent: str, full_prompt: str, graphify_data: dict, proj_id) -> str:
    """Extract target symbol from clause.

    Priority:
      1. CRG provider FTS (if available — set externally via set_providers())
      2. Embedding similarity against graphify node names (if graphify_data available)
      3. Regex fallback
    """
    # If the clause is just the intent word itself (e.g. "architecture"),
    # it's a generic query — no specific target to extract.
    _GENERIC_CLAUSES = {"architecture", "overview", "structure", "tests",
                        "security", "impact", "debug", "refactor"}
    if clause.lower().strip() in _GENERIC_CLAUSES:
        return clause.lower().strip()

    # 1. Try CRG provider FTS
    if _active_providers:
        for provider in _active_providers:
            try:
                target = provider.extract_target(clause)
                if target:
                    return target
            except Exception as e:
                log.warning("Provider extract_target failed: %s", e)

    # 2. Try embedding similarity against graphify node names
    if graphify_data:
        target = _target_extractor.get_target(clause, graphify_data, proj_id)
        if target:
            return target

    # 3. Regex fallback
    return _regex_extract_target(clause, intent)


# ── Provider injection ───────────────────────────────────────────

_active_providers = []

def set_providers(providers: list):
    """Set the active intelligence providers for target extraction.

    Called by retrieval.py after initializing providers (CRG, etc.).
    """
    global _active_providers
    _active_providers = providers or []


# ── Regex fallback ───────────────────────────────────────────────

_TASK_EXTRACTORS = {
    "architecture":  r"(?:architecture|structure|overview|components|organization|design)\s*(?:of|for)?\s*(.+)?",
    "how_works":     r"(?:how|explain).*(?:does|work|implement|called|used)\s*(?:(?:the|a|an)\s+)?(.+)",
    "what_is":       r"(?:what|where|which|find|show)\s+(?:is|are|the|a|an|file|function|class)\s*(.+)?",
    "impact":        r"(?:impact|what breaks|affect|blast radius|risk)\s*(?:of|on|for|if|when)?\s*(?:I\s+(?:change|modify|update|edit|refactor|delete|remove)\s+)?(.+)?",
    "callers":       r"(?:who|what)\s*(?:calls|uses|imports|depends on)\s*(.+)?",
    "callees":       r"(?:what does|what are)\s*(.+?)\s*(?:call|use|import|depend on)",
    "debug":         r"(?:bug|error|issue|problem|debug|trace)\s*(?:in|of|with)?\s*(.+)?",
    "refactor":      r"(?:refactor|rewrite|improve|optimize)\s*(.+)?",
    "nx_architecture": r"(?:nx|workspace|project|app|lib)\s*(?:architectur|structure|layout|organized|depend|target)?\s*(.+)?",
    "security":      r"(?:security|vulnerability|exploit|injection|xss|csrf)\s*(?:in|of|for)?\s*(.+)?",
    "tests":         r"(?:test|coverage|spec|unit|integration)\s*(?:for|of|in)?\s*(.+)?",
}


def _regex_extract_target(prompt: str, intent: str) -> str:
    """Regex-based target extraction — fallback when no providers or embeddings."""
    pattern = _TASK_EXTRACTORS.get(intent)
    if pattern:
        m = re.search(pattern, prompt, re.IGNORECASE)
        if m and m.group(1) and m.group(1).strip().rstrip("?").strip():
            return m.group(1).strip().rstrip("?").strip()[:80]
    return prompt[:80].rstrip("?").strip()


def _regex_fallback(prompt: str) -> list[dict]:
    """Fallback to regex-based intent detection if semantic router is unavailable."""
    from planner import _OLD_detect_intent, _OLD_extract_target
    info = _OLD_detect_intent(prompt)
    intent = info["intent"]
    target = _OLD_extract_target(prompt, intent)
    _vmsg("SEMANTIC PLANNER: regex fallback -> intent=%s target=%s", intent, target[:40])
    return [{"intent": intent, "target": target, "score": 0.0}]


def is_available() -> bool:
    """Check if semantic router is available."""
    r = _init_router()
    return r is not None
