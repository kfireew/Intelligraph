import requests, json, time, os

BASE = 'http://127.0.0.1:5050'
TOKEN = 'REDACTED'
MODEL = 'qwen/qwen3-235b-a22b'

r = requests.get(f'{BASE}/projects')
projects = r.json()
print('=== PROJECTS ===')
for p in projects:
    print(json.dumps(p))

pid = projects[0]['id']

art_dir = f'C:/Users/kfirm/Downloads/Intelligraph/backend/data/artifacts/{pid}'
print(f'\n=== ARTIFACT SIZES (pid={pid}) ===')
total_art = 0
for f in os.listdir(art_dir):
    fp = os.path.join(art_dir, f)
    size = os.path.getsize(fp)
    total_art += size
    print(f'  {f}: {size:,} bytes ({size/1024:.1f} KB)')
print(f'  TOTAL: {total_art:,} bytes ({total_art/1024:.1f} KB)')

repo_dir = None
repo_base = 'C:/Users/kfirm/Downloads/Intelligraph/backend/data/repos'
if os.path.isdir(repo_base):
    for entry in os.listdir(repo_base):
        d = os.path.join(repo_base, entry)
        if os.path.isdir(d):
            repo_dir = d
            break

if repo_dir:
    total = 0
    filecount = 0
    for root, dirs, files in os.walk(repo_dir):
        for f in files:
            fp = os.path.join(root, f)
            try:
                total += os.path.getsize(fp)
                filecount += 1
            except:
                pass
    print(f'\n=== REPO DIR (kept alive, NX_MCP=true) ===')
    print(f'  Path: {repo_dir}')
    print(f'  Size: {total:,} bytes ({total/1024/1024:.2f} MB)')
    print(f'  Files: {filecount}')
else:
    print(f'\n=== REPO DIR: deleted (sparse fetch mode) ===')

r = requests.get(f'{BASE}/projects/{pid}/graph-data')
gd = r.json()
gf = gd.get('graphify', {})
full_json = json.dumps(gf)
full_tokens = len(full_json) // 4
print(f'\n=== FULL CONTEXT (graphify_data JSON) ===')
print(f'  JSON size: {len(full_json):,} bytes ({len(full_json)/1024:.1f} KB)')
print(f'  Estimated tokens: ~{full_tokens:,}')
print(f'  Nodes: {gd.get("nodes", 0)}')
print(f'  Edges: {gd.get("edges", 0)}')

queries = [
    'What is the main entry point of this app?',
    'How does the graph builder work?',
    'Explain the architecture of this project',
    'What calls build_graph?',
    'What would break if I changed build_graph?',
]

print(f'\n=== RETRIEVAL BENCHMARKS ===')
results = []
for q in queries:
    t0 = time.time()
    r = requests.post(f'{BASE}/graph/retrieve-context', json={'prompt': q, 'project_id': pid}, timeout=120)
    t1 = time.time()
    data = r.json()
    ctx = data.get('context', '')
    ctx_tokens = len(ctx) // 4
    latency = t1 - t0
    stats = data.get('context_stats', {})
    strategy = data.get('strategy', '?')
    files = data.get('files', [])
    raw_chunks = stats.get('raw_chunks', 0)
    source_available = stats.get('source_available', False)
    degraded = stats.get('degraded', False)
    print(f'\n  Q: "{q}"')
    print(f'  Strategy: {strategy}')
    print(f'  Latency: {latency:.2f}s')
    print(f'  Context: {len(ctx):,} chars (~{ctx_tokens} tokens)')
    print(f'  Files: {len(files)}')
    print(f'  Raw chunks: {raw_chunks}')
    print(f'  Source available: {source_available}')
    print(f'  Degraded: {degraded}')
    results.append({'q': q, 'latency': latency, 'tokens': ctx_tokens, 'strategy': strategy, 'files': len(files), 'chunks': raw_chunks, 'ctx': ctx})

print(f'\n=== LLM ANSWERS (model: {MODEL}) ===')
for res in results:
    t0 = time.time()
    llm_payload = {
        'model': MODEL,
        'messages': [
            {'role': 'system', 'content': res['ctx']},
            {'role': 'user', 'content': res['q']}
        ],
        'max_tokens': 500,
        'temperature': 0.3
    }
    try:
        r = requests.post(f'{BASE}/llm/ask', json={'url': 'https://openrouter.ai/api/v1/chat/completions', 'token': TOKEN, 'payload': llm_payload}, timeout=60)
        t1 = time.time()
        llm_data = r.json()
        if llm_data.get('status') == 200:
            body = json.loads(llm_data['body'])
            answer = body['choices'][0]['message']['content']
            usage = body.get('usage', {})
            prompt_tokens = usage.get('prompt_tokens', 0)
            completion_tokens = usage.get('completion_tokens', 0)
            total_tokens = usage.get('total_tokens', 0)
            print(f'\n  Q: "{res["q"]}"')
            print(f'  LLM latency: {t1-t0:.2f}s')
            print(f'  Tokens: prompt={prompt_tokens} completion={completion_tokens} total={total_tokens}')
            print(f'  Answer: {answer[:400]}')
        else:
            print(f'\n  Q: "{res["q"]}" -- LLM ERROR: {llm_data.get("body", "")[:200]}')
    except Exception as e:
        print(f'\n  Q: "{res["q"]}" -- EXCEPTION: {e}')

print('\n=== DONE ===')
