# AGENTS.md — Bench Studio

## Deployment checkouts must stay clean

A deployed Bench Studio checkout is updated only by Service Portal's
**Update and restart** action (`scripts/update-and-restart.sh` →
`scripts/update.py`). The updater refuses to run when `git status --porcelain`
reports anything, when local history diverges from `origin/main`, or when the
branch is not `main`.

Rules for agents and contributors:

- Do not edit source in a checkout that a running deployment uses. Make changes
  in a separate development clone.
- A source change is finished only when it is committed and pushed to
  `origin/main`, unless the user explicitly asks to leave it uncommitted. Then
  the user deploys it with the Portal button.
- Before ending a task, confirm `git status --porcelain` is empty in any
  deployment checkout you touched.
- Build and test outputs (`frontend/node_modules`, `frontend/dist`,
  `frontend/test-results`, caches, `data/`, `.env`) are gitignored and do not
  make the checkout dirty. New generated outputs must be added to `.gitignore`.

## LLM Router contract

- Router integration (benchmark requests, model discovery, run identity,
  availability and retries) must uphold `docs/llm-router-contract.md`. Update
  its conformance map with any such change. Benchmarks never fall back to
  another model. Never edit the vendored contract text; replace it only when
  the LLM Router maintainer announces a new version.

## Verification

- Python: `PYTHONPATH=.:vendor .venv/bin/pytest tests vendor/tests`.
- Frontend: `cd frontend && npm ci && npm run build && npx playwright test`.
- See `README.md` (Development) and `docs/recovery.md` for update semantics.
