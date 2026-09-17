# Bench Studio validation

Implementation acceptance evidence is recorded here; raw results and logs remain in the deployment's private `data/validation` and `data/runs` directories.

- Initial Python engine/integration/application run: **162 passed**. Expanded scheduler, idempotency, updater-failure and recovery suite: **18 application tests passed**.
- Playwright: **3 passed**, covering browser launch, sequential both-model selection, logs, cancel, rerun review, profile navigation, keyboard activation, report/export links, comparison screen, narrow layout and dark appearance. Desktop and phone screenshots inspected.
- Offline coding verifier: four known fixtures (correct/incorrect Python and TypeScript); **2 passed, 2 failed exactly as expected**, including EvalPlus extended tests and actual TypeScript compilation/execution.
- Harbor isolated oracle integration: **reward 1.0**, reference patch applied and required tests passed through the actual custom environment adapter; no model requests.
- Repository image preparation, full reference validation, production inference smoke tests and Portal update verification are in progress. Results are added as these checks finish.
