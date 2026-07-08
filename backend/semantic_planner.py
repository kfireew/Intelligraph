"""
semantic_planner.py — Embedding-based intent routing via semantic-router.

Replaces the regex catalog in planner.py with a semantic vector space approach
that handles multi-phrased questions, unusual wording, and synonyms.

Uses:
  - HuggingFaceEncoder (all-MiniLM-L6-v2, 23MB, local-only) for embeddings
  - RouteLayer for fast cosine-similarity intent classification
  - Custom OpenRouterLLM for dynamic route parameter extraction (target symbol)

The encoder and route layer are lazily initialized on first use and cached.
A regex fallback is used if the model or library is unavailable.
"""

import logging
import os
import re
import sys

log = logging.getLogger(__name__)

_VERBOSE = os.environ.get("INTELLIGRAPH_VERBOSE", "true").lower() == "true"

_MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "all-MiniLM-L6-v2")
_SCORE_THRESHOLD = 0.25

_router_instance = None
_llm_instance = None
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


# ── Custom LLM for dynamic route parameter extraction ────────────

class OpenRouterLLM:
    """Calls an OpenAI-compatible LLM endpoint for target extraction.

    Uses INTELLIGRAPH_LLM_URL / INTELLIGRAPH_LLM_TOKEN / INTELLIGRAPH_LLM_MODEL
    env vars. Falls back gracefully if not configured.
    """

    def __init__(self):
        import requests
        self._requests = requests
        self.name = "intelligraph-llm"
        self.url = os.environ.get("INTELLIGRAPH_LLM_URL", "").rstrip("/")
        self.token = os.environ.get("INTELLIGRAPH_LLM_TOKEN", "")
        self.model = os.environ.get("INTELLIGRAPH_LLM_MODEL", "gpt-4o-mini")
        self.ssl_verify = os.environ.get("LLM_SSL_VERIFY", "false").lower() == "true"
        self.timeout = 15

    def __call__(self, messages):
        """Call the LLM with a list of Message-like objects. Returns text."""
        if not self.url:
            return None
        try:
            payload = {
                "model": self.model,
                "messages": [{"role": m.role, "content": m.content} for m in messages],
                "max_tokens": 100,
                "temperature": 0.0,
            }
            headers = {"Content-Type": "application/json"}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            resp = self._requests.post(
                self.url, json=payload, headers=headers,
                timeout=self.timeout, verify=self.ssl_verify,
            )
            resp.encoding = "utf-8"
            if resp.status_code != 200:
                log.warning("OpenRouterLLM status=%s body=%s", resp.status_code, resp.text[:200])
                return None
            data = resp.json()
            return data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        except Exception as e:
            log.warning("OpenRouterLLM failed: %s", e)
            return None

    def extract_function_inputs(self, query, function_schema):
        """Extract target parameter from query using LLM."""
        import json
        prompt = (
            "You are a helpful assistant designed to output JSON. "
            "Given the following function schema "
            f"<< {function_schema} >> "
            "and query "
            f"<< {query} >> "
            "extract the parameters values from the query, in a valid JSON format. "
            'Example: {"target": "build_graph"}'
        )
        from semantic_router.schema import Message
        llm_input = [Message(role="user", content=prompt)]
        output = self(llm_input)
        if not output:
            raise Exception("No output generated for extract function input")
        output = output.replace("'", '"').strip().rstrip(",")
        result = json.loads(output)
        return result


# ── Route definitions ────────────────────────────────────────────

def _build_routes():
    from semantic_router import Route

    def _extract_target(query: str) -> str:
        """Extract the target symbol/component name from the query."""
        return query[:80].strip()

    target_schema = {
        "name": "extract_target",
        "description": "Extract the target symbol, file, or component name from a code intelligence question",
        "signature": "(target: str) -> str",
        "output": "<class 'str'>",
    }

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
                "what does this module do",
                "what does this component do",
                "how does this system operate",
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
                "project dependencies in the monorepo",
                "what nx projects exist",
                "how is the nx workspace organized",
                "show me the monorepo layout",
                "what are the nx project boundaries",
                "nx project graph",
                "workspace dependency graph",
            ],
            score_threshold=_SCORE_THRESHOLD,
        ),
    ]

    return routes


# ── Initialization ───────────────────────────────────────────────

def _init_router():
    """Lazily initialize the semantic router. Returns (router, llm) or (None, None)."""
    global _router_instance, _llm_instance, _init_error
    if _router_instance is not None:
        return _router_instance, _llm_instance
    if _init_error is not None:
        return None, None

    try:
        os.environ["TRANSFORMERS_VERBOSITY"] = "error"
        os.environ["TOKENIZERS_PARALLELISM"] = "false"

        from semantic_router import RouteLayer
        from semantic_router.encoders import HuggingFaceEncoder

        if not os.path.isdir(_MODEL_DIR):
            _init_error = f"Model dir not found: {_MODEL_DIR}"
            _vmsg("SEMANTIC PLANNER: %s", _init_error)
            return None, None

        _vmsg("SEMANTIC PLANNER: loading encoder from %s", _MODEL_DIR)
        encoder = HuggingFaceEncoder(
            name=_MODEL_DIR,
            model_kwargs={"local_files_only": True},
        )
        encoder.score_threshold = _SCORE_THRESHOLD

        routes = _build_routes()
        router = RouteLayer(encoder=encoder, routes=routes)

        _llm_instance = OpenRouterLLM()
        if not _llm_instance.url:
            _vmsg("SEMANTIC PLANNER: no LLM URL configured, target extraction will use regex fallback")
            _llm_instance = None

        _router_instance = router
        _vmsg("SEMANTIC PLANNER: ready (%d routes, threshold=%.2f)", len(routes), _SCORE_THRESHOLD)
        return _router_instance, _llm_instance
    except Exception as e:
        _init_error = str(e)
        _vmsg("SEMANTIC PLANNER: init failed: %s", e)
        log.warning("Semantic router init failed: %s", e, exc_info=True)
        return None, None


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

def route_query(prompt: str) -> list[dict]:
    """Route a user prompt to one or more intents.

    Returns:
        [{intent: str, target: str, score: float}, ...]
        Multiple entries for compound queries.
        Falls back to [{"intent": "what_is", "target": prompt[:80]}] if routing fails.
    """
    router, llm = _init_router()
    if router is None:
        return _regex_fallback(prompt)

    clauses = _split_clauses(prompt)
    if len(clauses) <= 1:
        clauses = [prompt]

    results = []
    seen_intents = set()
    for clause in clauses:
        result = _route_single(router, llm, clause, prompt)
        if result and result["intent"] not in seen_intents:
            results.append(result)
            seen_intents.add(result["intent"])

    if not results:
        return _regex_fallback(prompt)

    _vmsg("SEMANTIC PLANNER: prompt=%r -> %s", prompt[:80], results)
    return results


def _route_single(router, llm, clause: str, full_prompt: str) -> dict | None:
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

    target = _extract_target(llm, clause, intent, full_prompt)

    return {
        "intent": intent,
        "target": target,
        "score": score,
    }


def _extract_target(llm, clause: str, intent: str, full_prompt: str) -> str:
    """Extract the target symbol from the clause.

    Tries LLM-based extraction first, falls back to regex.
    """
    if llm:
        try:
            schema = {
                "name": "extract_target",
                "description": "Extract the target symbol, file, or component name from a code intelligence question",
                "signature": "(target: str) -> str",
                "output": "<class 'str'>",
            }
            result = llm.extract_function_inputs(clause, schema)
            target = result.get("target", "").strip()
            if target and len(target) > 1:
                return target[:80]
        except Exception as e:
            log.debug("LLM target extraction failed: %s", e)

    return _regex_extract_target(clause, intent)


def _regex_extract_target(prompt: str, intent: str) -> str:
    """Simplified regex target extraction — fallback when LLM is unavailable."""
    patterns = {
        "architecture":  r"(?:architecture|structure|overview|components|organization|design)\s*(?:of|for)?\s*(.+)?",
        "how_works":     r"(?:how|explain).*(?:does|work|implement|called|used)\s*(?:(?:the|a|an)\s+)?(.+)",
        "what_is":       r"(?:what|where|which|find|show)\s+(?:is|are|the|a|an|file|function|class)\s*(.+)?",
        "impact":        r"(?:impact|what breaks|affect|blast radius|risk)\s*(?:of|on|for|if|when)?\s*(.+)?",
        "callers":       r"(?:who|what)\s*(?:calls|uses|imports|depends on)\s*(.+)?",
        "callees":       r"(?:what does|what are)\s*(.+?)\s*(?:call|use|import|depend on)",
        "debug":         r"(?:bug|error|issue|problem|debug|trace)\s*(?:in|of|with)?\s*(.+)?",
        "refactor":      r"(?:refactor|rewrite|improve|optimize)\s*(.+)?",
        "security":      r"(?:security|vulnerability|exploit|injection|xss|csrf)\s*(?:in|of|for)?\s*(.+)?",
        "tests":         r"(?:test|coverage|spec|unit|integration)\s*(?:for|of|in)?\s*(.+)?",
        "nx_architecture": r"(?:nx|workspace|project|app|lib)\s*(?:architectur|structure|layout|organized|depend|target)?\s*(.+)?",
    }
    pattern = patterns.get(intent)
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
    r, _ = _init_router()
    return r is not None
