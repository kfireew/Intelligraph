import json
data = json.load(open(r'C:\Users\kfirm\Downloads\Intelligraph\bench\results.json'))
corpus = 698418
print('Per-Query Results:')
print(f'{"ID":5s} {"Category":10s} | {"Graphify":>25s} | {"CRG":>25s} | {"Intelligraph":>25s}')
print(f'{"":5s} {"":10s} | {"tok F1    MRR  files":>25s} | {"tok F1    MRR  files":>25s} | {"tok F1    MRR  files":>25s}')
print('-' * 100)
for q in data['queries']:
    sy = q['systems']
    gf = sy.get('graphify', {})
    cr = sy.get('crg', {})
    ig = sy.get('intelligraph', {})
    print(f'{q["query_id"]:5s} {q["category"]:10s} | {gf.get("context_tokens",0):3d}t {gf.get("f1",0):.3f} {gf.get("mrr",0):.3f} {len(gf.get("files_returned",[])):3d}f | {cr.get("context_tokens",0):3d}t {cr.get("f1",0):.3f} {cr.get("mrr",0):.3f} {len(cr.get("files_returned",[])):3d}f | {ig.get("context_tokens",0):3d}t {ig.get("f1",0):.3f} {ig.get("mrr",0):.3f} {len(ig.get("files_returned",[])):3d}f')
print()
print('Token Reduction (vs 698K corpus):')
for s in ['graphify', 'crg', 'intelligraph']:
    avg = sum(q['systems'][s]['context_tokens'] for q in data['queries']) / 20
    red = corpus / avg if avg > 0 else 0
    print(f'  {s:15s}: avg {avg:.0f} tokens -> {red:.0f}x reduction')
