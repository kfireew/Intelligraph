"""Run architecture query on real project and verify CRG domain files appear."""
import sys, os, json
sys.path.insert(0, os.path.dirname(__file__))

repo_dir = os.path.join(os.path.dirname(__file__), "data", "repos", "receipt-ocr")
with open(os.path.join(repo_dir, "graphify-out", "graph.json")) as f:
    graphify_data = json.load(f)

proj = {"graphify_data": graphify_data, "repo_dir": repo_dir, "_G": None}

from retrieval import retrieve_context
result = retrieve_context(proj, "architecture")
ctx = result.get("context", "")
stats = result.get("context_stats", {})

print("=== context_stats ===")
for k, v in sorted(stats.items()):
    print(f"  {k}: {v}")

print(f"\n=== CRG domain files section present: {'Y' if '## Domain Workflow Files Found By CRG' in ctx else 'N'} ===")
if "## Domain Workflow Files Found By CRG" in ctx:
    idx = ctx.index("## Domain Workflow Files Found By CRG")
    end = ctx.index("## Codebase Structure") if "## Codebase Structure" in ctx else len(ctx)
    print(ctx[idx:end])

print(f"\n=== Key domain checks ===")
for needle in ["BackendBridge", "MainWindow", "get_database()", "thread_bridge",
                "phase", "column", "schema", "ocr", "vendor", "cache",
                "email_fetcher", "pipeline", "product_catalog", "Domain Workflow"]:
    print(f"  {'Y' if needle.lower() in ctx.lower() else 'N'} {needle}")

# Print section ordering
import re
headers = re.findall(r'^## [A-Z].*$', ctx, re.MULTILINE)
print(f"\n=== Section ordering ===")
for h in headers:
    print(f"  {h}")

print(f"\n=== Raw code files sampled ===")
if "## Source Code" in ctx:
    src = ctx[ctx.index("## Source Code"):]
    files_in_code = set(re.findall(r'``(.*?)``', src))
    for f in sorted(files_in_code):
        print(f"  {f}")