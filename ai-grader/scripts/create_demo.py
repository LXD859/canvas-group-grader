"""Create a local synthetic project and a matching reviewable task bundle."""
import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from grader.demo import create_demo, demo_results
from grader.domain import packet
from grader.store import Store

store = Store(root / 'data', recover=False)
with store.lock:
    candidates = [item for item in store.list() if item['name'].endswith('（离线任务包）')]
    p = store.load(candidates[0]['id']) if candidates else None
    if not p or not p['source'].get('demo'):
        p = create_demo(store)
        p['name'] += '（离线任务包）'
        store.save(p)
folder = root / 'dist' / ('demo-' + p['id'][:8])
folder.mkdir(parents=True, exist_ok=True)
(folder / 'grading-task.zip').write_bytes(packet(store, p, p['tasks'][0]))
(folder / 'results.json').write_text(json.dumps(demo_results(p), ensure_ascii=False, indent=2), encoding='utf-8')
(folder / 'README.txt').write_text('这些材料完全为合成示例。打开本机批阅器，选择项目 ' + p['id'] + ' 后导入 results.json。该文件只匹配本次创建的项目。\n', encoding='utf-8')
print(folder)
