# Local workspace application

React + TypeScript client, FastAPI service and SQLite persistence. Dependencies are prepared in the task image. No network services are required.

Run `npm --prefix frontend run build`, `python -m pytest tests -q`, then `python -m uvicorn backend.app:app --host 127.0.0.1 --port 8111` to serve the built application. Set APP_DATABASE to use a separate SQLite database for tests. Do not commit generated databases, dependencies or build output.

The seeded issues are Fix login (open), Refresh docs (closed), and Add export (open). Inventory contains Adapters (2), Cables (38), and Batteries (12). Keep existing creation and list APIs working. API errors use HTTP status codes; client controls have accessible labels.

Keep issue and inventory detail cards as semantic articles, and tabular inventory as table rows. New order detail cards should also be articles. Save confirmations use role="status"; validation errors use role="alert". These accessible landmarks are part of the interaction contract, not a prescribed visual design.
