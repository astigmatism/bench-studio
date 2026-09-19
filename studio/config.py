import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("DATA_ROOT", ROOT / "data"))
ENDPOINT = os.environ.get("LLM_ENDPOINT", "http://127.0.0.1:11434/v1").rstrip("/")
RUNTIME = os.environ.get("RUNTIME_URL", "http://127.0.0.1:11436/api/status")
SETTINGS = {"endpoint": ENDPOINT, "runtime_url": RUNTIME}
REVISION = os.environ.get("SOURCE_REVISION", "development")
WORKER_IMAGE = os.environ.get("WORKER_IMAGE", "local/bench-studio-worker:current")
VERIFIER_IMAGE = os.environ.get("VERIFIER_IMAGE", "local/bench-studio-verifier:current")
SESSION_IMAGE = os.environ.get("SESSION_IMAGE", "local/bench-studio-session:current")
SESSION_SMOKE_TARGET = os.environ.get("SESSION_SMOKE_TARGET", "daytime")
