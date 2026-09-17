import hashlib, json, urllib.request
from pathlib import Path

root = Path("/opt/multiple")
(root / "safe_subprocess").mkdir(parents=True, exist_ok=True)
base = "https://raw.githubusercontent.com/nuprl/MultiPL-E/3025a531af7450e7df8b96fe0440e9804480bbad/"
for src, dst in [
    ("evaluation/src/eval_ts.py", "eval_ts.py"),
    ("evaluation/src/safe_subprocess/__init__.py", "safe_subprocess/__init__.py"),
    ("LICENSE", "LICENSE"),
]:
    data = urllib.request.urlopen(base + src, timeout=30).read()
    (root / dst).write_bytes(data)
    print(src, hashlib.sha256(data).hexdigest())
