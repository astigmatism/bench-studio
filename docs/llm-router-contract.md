> **Vendored copy — do not edit.** LLM Router client contract, version 1.1, copied from
> llm-router commit `1ecc04758ad4d0a6954713defad4d02ff3e8f351`. Canonical source:
> https://github.com/astigmatism/llm-router/blob/main/docs/CLIENT_CONTRACT.md
> Replace this copy only when the router maintainer announces a new contract version.

# LLM Router client contract

**Version 1.1 · 2026-10-07.** Applies to LLM Router `57e4096` and later, with AI Runtime `e0530db` and later. Version 1.1 adds the benchmark exception in §3; see [changes](#15-changes).

This is the contract between the LLM Router and every application that sends it inference requests. It states what the router guarantees and what a client must do to keep working while models, contexts and configurations change underneath it. When another document disagrees with this one, this one wins. Detailed schemas and examples are in the [references](#14-references).

**MUST**, **MUST NOT**, **SHOULD** and **MAY** are used in their usual sense: **SHOULD** means "do this unless you have a specific, documented reason not to".

## 1. Roles

| Party | Owns |
|---|---|
| **AI Runtime** | Which models run, on which GPUs, with what context and slot count. Configurations are switched by hand at any time, without notice to clients. It declares each model's qualified capabilities, including whether it is NSFW. |
| **LLM Router** | One stable API in front of whatever is running. It handles admission and queueing, enforces context and capability limits, translates between protocols, describes what is available (`/v1/router/capabilities`), and pushes changes (`/v1/router/events`). |
| **Client** | Choosing a model for each request, managing conversation history, and reacting to changes and errors as this contract describes. |

The router never does any of these:

- substitute one model for another (a request for an unavailable model fails rather than being redirected);
- download, load or switch models;
- keep conversation state between requests;
- impose a total generation or queue deadline;
- infer anything about a model from its name.

## 2. Connecting

| Client location | OpenAI-compatible base URL | Ollama-compatible base URL |
|---|---|---|
| LAN | `http://192.168.1.4:11434/v1` | `http://192.168.1.4:11434` |
| Container on Rosalina's `local-ai-ollama_default` network | `http://ai-router:11434/v1` | `http://ai-router:11434` |

| Route | Use |
|---|---|
| `POST /v1/chat/completions` | OpenAI Chat Completions |
| `POST /v1/responses` (alias `/responses`) | OpenAI Responses, stateless |
| `POST /api/chat` | Ollama chat |
| `POST /api/generate` | Ollama text completion; send `think: false` (reasoning is unsupported on this route); prefer `/api/chat` |
| `GET /v1/models`, `GET /v1/models/{id}`, `GET /api/tags`, `POST /api/show`, `GET /api/ps` | Model listings (§4) |
| `GET /v1/router/capabilities`, `GET /v1/router/events` | Deployment state and change events (§4) |

- **Authentication:** none. A client MUST NOT send the router's admin token or any real credential. If an SDK insists on an API key, send the placeholder `local-only`. The router is a trusted-LAN service and MUST NOT be exposed beyond it.
- **Identification:** a client SHOULD send `X-Client-Name: <application-name>` on every request. The router records it in request history and on its dashboard; without it, requests are identified by user agent or IP address.
- **Not provided:** embeddings (400 `UNSUPPORTED_PROFILE_CAPABILITY`) and model management, meaning pull, create, copy, push and delete (`MODEL_MANAGEMENT_DISABLED`).

## 3. Model identity

| ID | Meaning | Stability |
|---|---|---|
| `daytime` | The Daytime service. Present in **every** configuration. | Stable; use it. |
| `nighttime` | The Nighttime service. Present only in **paired** configurations; a solo configuration stops it. | Stable; use it. |
| `local-active` | Legacy alias of `daytime`. | Kept for compatibility. |
| Canonical IDs, such as `qwen3.8-27b-abliterated-q6_k` | The specific model currently behind a service. | **Can change** with any configuration change. |

- A client MUST send service IDs (`daytime`, `nighttime`). It MUST NOT configure, persist or compare canonical IDs. Responses report the canonical ID in `model`; record it as information only.
- A client SHOULD always send a model. An omitted model selects `daytime`.
- An unknown ID returns **404 `MODEL_NOT_FOUND`**.
- A known service that the current configuration stops returns **503 `SERVICE_OFFLINE`**. It is then listed in `offline_services` (§4).
- **Exception for benchmarking and evaluation clients.** A client whose results must belong to one exact model MAY pin it for a run. At the start of the run it resolves the service ID to its canonical ID (`ids[service]`), records the canonical ID with the model's `revision`, and sends the canonical ID for the rest of the run. The router then never lets that run reach a different model. While pinned, the client treats each response like this:
  - 404 `MODEL_NOT_FOUND`: the model changed;
  - 503 `SERVICE_OFFLINE`: the model is offline;
  - neither is ever a reason to switch models.

  Such a client still discovers models from the capabilities document (§4). It MUST NOT keep canonical IDs as configuration across runs: new runs start from a service ID.

## 4. Knowing what is available

### Capabilities document

`GET /v1/router/capabilities` returns one document describing the whole deployment, with an `ETag` (send `If-None-Match` to get a 304 when nothing has changed). A client MUST read it at startup, and MUST NOT fail to start when the router is unreachable: start degraded and retry. The fields a client relies on:

| Field | Meaning |
|---|---|
| `revision` | Content hash of everything below except load. If `revision` is unchanged, nothing relevant has changed. |
| `router.accepting_requests` | False while the router drains for a configuration switch, or during maintenance. |
| `configuration.id`, `configuration.exclusive` | The AI Runtime configuration, and whether it is solo. |
| `models[]` | Each usable model: `service` (the ID to send), `aliases`, `available`, `slots`, `context_window`, `input_modalities`, `capabilities`, `nsfw`, `capability_score`, and full `metadata`. |
| `offline_services[]` | Services this configuration deliberately stops: `model`, `aliases`, `display_name`, `reason`. |
| `ids` | Every accepted ID → its current canonical model. |
| `load` (only with `?include=load`) | Per model: `active`, `queued`, `free_slots`. |

Clients MUST ignore unknown fields. `metadata` is identical to the model's `x_ollama_router` object in `/v1/models`. The OpenAI and Ollama listings (`/v1/models`, `/api/tags`) show only models that can be selected right now, so an offline service is absent from them; only the capabilities document says why.

### Change events

`GET /v1/router/events` is a Server-Sent Events stream.

- **Each connection** starts with the complete document as `event: capabilities` (`id:` is its `revision`), then `event: load`.
- **A new `capabilities` event**, always the complete document, follows every change: immediately for a runtime publication, drain or maintenance change; within about 10 s for a backend health change.
- **`load` events** carry slot occupancy, at most once per second.
- **`: keepalive` comments** arrive every 15 s.

During a switch, a subscriber sees: draining (`accepting_requests: false`), then the new configuration, then `accepting_requests: true`.

A long-running client SHOULD hold one subscription for its lifetime and replace its copy of the document whenever `revision` changes. While disconnected it MUST fall back to polling the capabilities endpoint (every 30 s, with `If-None-Match`). It reconnects with backoff starting at the stream's `retry:` value (3 s), up to 30 s. A connection with no bytes for 60 s, keepalives included, is dead. A short-lived or request-scoped client MAY instead fetch the document before each request.

### NSFW flag and capability score

- **`nsfw`** is `true` for models the runtime declares abliterated (refusals removed), `false` for models it declares not, and `null` if undeclared. Treat `null` as "not known to be NSFW".
- **`capability_score`** is an automatic 0–100 ranking; higher is more capable. It is comparable only between models of this router. It is built from three parts:
  - parameter count, scaled down for heavier quantization;
  - context window;
  - vision, tools and reasoning support.

  Availability, load and speed never change it. It cannot measure answer quality: it can't account for what abliteration costs, or compare a mixture-of-experts model fairly with a dense one.

## 5. Choosing a model

A client chooses the service for **each request** from its current document. It chooses in one of two ways and SHOULD make the choice configurable:

- **By capability:** the most capable `available` model that has the features the request needs, optionally restricted to `nsfw: true`, with or without falling back to any model. Prefer this when a service wants Nighttime *because* it is uncensored. The reference clients implement it as `pick_service(doc, nsfw=True, require=[...], fallback_any=True)`.
- **By name:** a preferred service ID plus an ordered fallback list, for example `nighttime` then `daytime`. The reference clients implement it as `resolve(doc, preferred, fallbacks)`.

Apply these rules whichever way you choose:

| Situation | Client action |
|---|---|
| `router.accepting_requests` is false | **Wait** and retry the same choice later (§7). MUST NOT fall back: every model is draining. |
| Chosen model listed and `available` | Use it. |
| Chosen model offline, unavailable or absent | Use the next candidate that is `available` and supports the request's needs. A client MAY disable fallback; it then reports the service as temporarily unavailable. |
| Nothing usable | Report "temporarily unavailable" and retry later. |
| The preferred model returns | Use it again from the next request. Fallback MUST NOT be sticky. |

- **Feature checks:** before sending, check the target model. Images need `image` in `input_modalities`; tools need `tools` in `capabilities`; reasoning needs `thinking` in `capabilities` and the requested effort in `metadata.reasoning.efforts`. A missing capability makes that model unsuitable for the request; don't send and hope.
- **Behavioral differences:** Daytime is not abliterated and may refuse what Nighttime answers. A client that falls back MUST surface a refusal clearly and MUST NOT retry it in a loop.
- **Logging:** a client SHOULD log every change of served model and its reason, and show it where it already shows model status.

Benchmarking and evaluation clients are the exception. A benchmark's identity is its model, so such a client MUST NOT fall back. It treats an offline or unavailable target as "waiting", not as a changed or invalid run.

## 6. Sending requests

- **Streaming:** a client MUST set `stream` explicitly. `/v1/chat/completions` and `/api/chat` **stream when `stream` is omitted**; `/v1/responses` does not.
- **Statelessness:** every request MUST carry the full conversation the model needs. Responses `previous_response_id` and `store: true` are rejected with 400 `STATEFUL_REQUEST_UNSUPPORTED`.
- **Output limits:** output is unrestricted unless the client asks for a limit. To limit it, send a positive integer: `max_tokens` or `max_completion_tokens` (Chat Completions), `max_output_tokens` (Responses), or `options.num_predict` (Ollama, where `-1` means unrestricted). A client MUST NOT send `-1` or `0` on OpenAI routes, and MUST NOT send two conflicting limits. A requested limit is honored exactly, subject to context (§8).
- **Reasoning:** thinking is **on by default**, at the chat template's default effort, with no separate budget. It uses output tokens and time.
  - Choose an effort with `think` (Ollama: `false`, `true`, or an effort), `reasoning_effort` (Chat Completions), or `reasoning.effort` (Responses).
  - The efforts are `off`, `default`, `low`, `medium` and `xhigh`. The aliases `none` → off, `minimal` → low, and `high`/`max` → xhigh are accepted.
  - A client that wants short, fast answers SHOULD turn thinking off explicitly.
- **Tools:** only function tools are supported. The client executes calls and returns results with matching call IDs and the complete preceding history. Provider-executed tools, such as web search, are rejected. Send tools only to models whose `capabilities` include `tools`.
- **Images:** only inline base64 is accepted:
  - Ollama: `messages[].images`;
  - Chat Completions: `image_url` with a `data:image/...;base64,...` URL;
  - Responses: `input_image` with a data URL.

  Remote URLs and file paths are rejected.
- **Structured output:** `response_format` (OpenAI) or `format` (Ollama) is supported. Output that isn't valid JSON ends with an error (§9), never as a success.
- **Sampling:** sampling controls (`temperature`, `top_p`, `top_k`, `seed`, `stop` and similar, top-level or in Ollama `options`) are forwarded or rejected with 400; never silently dropped. Backend controls such as `options.num_ctx` are rejected (`BACKEND_CONTROL_FORBIDDEN`). `seed` accepts 0–4294967295, or -1 for random.

## 7. Capacity, queueing and time

- **Slots:** each model admits `slots` generations at once (usually 1). Aliases of one model share its slots. Extra requests wait in a first-in, first-out queue for that model; Daytime and Nighttime queue independently. A client SHOULD NOT send more concurrent requests to a model than its `slots`. Fallback traffic joins the fallback model's queue, behind its regular users, such as the coding agents on Daytime.
- **Waiting while queued:**
  - Streaming Chat Completions receive `: waiting for inference slot` comments every 15 s.
  - Streaming Ollama chat receives keepalive frames every 15 s: `{"message":{"role":"assistant","content":""},"done":false,"x_router":{"status":"in_progress"},...}`. A client MUST ignore them.
  - Streaming Responses receive `response.created` and `response.in_progress` immediately.
  - Non-streaming requests receive nothing until the result.
- **Deadlines:** there is no queue or total-generation deadline. A client's read timeout MUST allow for queueing plus generation, or the client MUST stream. A backend that stops making progress for 120 s ends the request as an error.
- **Cancellation:** closing the connection cancels the request immediately, including while it is queued. A client SHOULD NOT abandon and resubmit a queued request, because that loses its place.
- **Draining:** during a configuration switch or maintenance, new requests receive 503 `BACKEND_DRAINING` or `MAINTENANCE_MODE`. Requests already accepted, including queued ones, finish normally. A switch usually takes one to two minutes. A client MUST keep waiting and retrying (backoff from 2 s up to 30 s) for at least 10 minutes, then choose its model again: the configuration may have changed.
- **Health checks:** a client SHOULD NOT use `GET /health` to judge model availability; use the capabilities document.

## 8. Context

- **Budget:** each request must fit the target model's per-request `context_window`. The router counts the actual formatted prompt with the model's tokenizer, including the chat template, tools and images. A request is admitted when **formatted input + requested output + `metadata.context_safety_reserve` (1024) ≤ `context_window`**. A request with unrestricted output needs room for at least one generated token.
- **Admission:** a request that doesn't fit, with a requested limit, is rejected with 400 `context_length_exceeded` and the exact arithmetic. Nothing is truncated silently.
- **Recovery for unrestricted requests:** an unrestricted request whose history doesn't fit, or whose generation reaches the end of the context, triggers lossy recovery. The router keeps the system and developer instructions and the latest user turn, fits an excerpt of earlier work, and inserts a visible notice beginning `[Physical context boundary reached.` into the answer. This is **not** lossless memory. `x_router.context_transitions` counts how many times it happened.
- **Managing history:** a client that needs exact history MUST manage it itself: trim or summarize before sending, or send an explicit output limit so overflow is rejected rather than recovered. Tool-call history is shortened only when that is safe; otherwise the request fails with `CONTEXT_RECOVERY_UNAVAILABLE`.
- **Model changes:** a client MUST take limits from the model that will actually serve the request, and MUST recompute its budget when the served model changes. Daytime and Nighttime windows differ, and either may be larger. A client MUST NOT hard-code a context size.

## 9. Results and terminal states

- **Success:** a completed response has a finish reason: `stop`, `tool_calls`, or `length` (the requested output limit was reached). Chat Completions results carry `x_router: {status, stop_reason, record_id, context_transitions}`; Ollama results carry `done_reason`; Responses carry `status` and `incomplete_details`.
- **Incomplete:** a response with `x_router.status: "incomplete"`, Responses `status: "incomplete"`, or Ollama `done_reason: "error"` is **not** a completed answer, even when it contains text. A client MUST NOT treat it as one. In particular:
  - a thinking-only or empty answer fails with `EMPTY_UPSTREAM_RESPONSE`;
  - partial tool arguments fail with `MALFORMED_UPSTREAM_TOOL_ARGUMENTS` and MUST NOT be executed;
  - invalid structured output fails with `MALFORMED_STRUCTURED_OUTPUT`.
- **Errors inside a stream:** once a streaming response has started, including while queued, a later error is delivered **inside the stream** after an HTTP 200:
  - Chat Completions: a `data:` frame with an `error` object (including `code`) and `x_router.status: "incomplete"`, possibly followed by `data: [DONE]`;
  - Ollama: a final line with `"done": true`, `"done_reason": "error"`, a **string** `error` and `x_router.status: "incomplete"`, with the code in `x_router.stop_reason` when known;
  - Responses: `response.failed` or `error` events.

  A streaming client MUST check the final frame, and MUST treat a stream that ends without a normal finish as incomplete.
- **Tracing:** `x-router-generation-id` (a response header) and `x_router.record_id` identify the router's archive record. Include them in bug reports.

## 10. Errors

Every router error carries a machine-readable code:

- **Ollama and Chat Completions:** `{"error": {"code": "…", "message": "…"}}`.
- **Responses, and some Chat Completions errors:** the OpenAI shape `{"error": {"message": "…", "type": "…", "param": "…", "code": "…"}}`.
- **Inside a stream:** see §9.

A client MUST read `error.code` from the object form, and SHOULD tolerate a string `error`. React to the code first, then the HTTP status:

| Code (HTTP status) | Meaning | Client action |
|---|---|---|
| `SERVICE_OFFLINE` (503) | The current configuration deliberately stops this service | Fall back now (§5). Not a failure: don't count it toward retry caps, budgets or circuit breakers. |
| `BACKEND_UNAVAILABLE` (503) | The service's backend isn't healthy | Fall back if possible; otherwise retry with backoff. |
| `MODEL_NOT_FOUND` (404) | The ID isn't offered | Fall back if configured, and log a warning: usually a stale or misspelled ID. |
| `BACKEND_DRAINING`, `MAINTENANCE_MODE` (503) | Switch or maintenance in progress | Wait (§7), then choose again. |
| `context_length_exceeded` (400) | The request doesn't fit | Shrink the request, or choose a model with a larger window. Don't retry unchanged. |
| `TOO_MANY_SUBSCRIBERS` (503, events only) | Event-stream limit reached | Poll the capabilities endpoint instead. |
| Other 5xx; 408, 429; network errors and timeouts | Transient | Retry the same service with backoff. |
| Other 4xx | The request is invalid | Fix the request. Don't retry and don't fall back. |

New codes may be added. An unknown 5xx code is transient; an unknown 4xx code is a request error. The reference clients' `classify_error(status, body)` implements this table.

## 11. Change and compatibility

- **Schema versions:** the capabilities document has `schema_version: 1`; model metadata (`x_ollama_router`) has `schema_version: 2`; the capability score carries its own `version`. Fields may be **added** at any time without a version change, and clients MUST ignore fields they don't know. Removing a field or changing its meaning increments the schema version. A client SHOULD warn when it sees a schema version it wasn't written for.
- **Contract versions:** this contract is versioned at the top. A change that requires client changes increments the major version (2, 3, …) and is announced to client maintainers. A minor version (1.1, 1.2, …) only clarifies or adds permissions and never requires client changes, so copies of an earlier minor version stay valid until the next update handoff.
- **Configurations:** models, context windows, slot counts, the canonical IDs behind service IDs, and the existence of `nighttime` all change with configuration and are not part of the contract. Only the rules for discovering them are.
- **Copies in client projects:** every client project keeps this contract in its own repository, so whoever works on that project, person or AI agent, sees it there:
  - **Copy:** keep a verbatim copy at `docs/llm-router-contract.md`. Begin it with this header, then the contract text unchanged:

    ```markdown
    > **Vendored copy — do not edit.** LLM Router client contract, version <N>, copied from
    > llm-router commit `<sha>`. Canonical source:
    > https://github.com/astigmatism/llm-router/blob/main/docs/CLIENT_CONTRACT.md
    > Replace this copy only when the router maintainer announces a new contract version.
    ```

  - **Conformance map:** follow the copy with a section `## How <project> upholds this contract`, or put it in its own file linked from there. It maps each item of the conformance checklist (§13) to the code and tests that meet it, and records any deviation the contract permits, such as fallback disabled under §5.
  - **Agent rule:** add a rule to the project's `AGENTS.md`, or its equivalent: any change that touches router requests, model selection or discovery must uphold `docs/llm-router-contract.md` and keep the conformance map current.
  - **Updates:** when this contract's version changes, the router maintainer sends each project a handoff to replace its copy and review its map.

## 12. Data handling

- **Archiving:** the router archives every generation in full — messages, images, tools, reasoning and output — for up to seven days or 1 GiB, for diagnosis and recovery. Clients MUST NOT send secrets they don't want retained, and SHOULD tell their own users that conversations pass through a logged service.
- **NSFW content:** output from an `nsfw: true` model may be explicit. A client that exposes it to people is responsible for any age, audience or consent controls its context needs.

## 13. Conformance checklist

A client conforms when it meets every item below and has a test for each MUST:

- [ ] Sends service IDs only, or, for a benchmark, a canonical ID pinned per run (§3). Persists no canonical IDs as configuration. Sends `X-Client-Name`.
- [ ] Reads the capabilities document at startup without failing when the router or a model is unavailable.
- [ ] Long-running: subscribes to `/v1/router/events` and polls while disconnected. Request-scoped: reads the document before each request.
- [ ] Chooses the model per request by capability or by name, with configurable fallback (or, for benchmarks, deliberately none). Returns to the preferred model when it is available again.
- [ ] Waits, without switching, while the router drains or is in maintenance.
- [ ] Classifies errors by `error.code`, as in §10. `SERVICE_OFFLINE` doesn't consume retry or budget allowances.
- [ ] Sets `stream` explicitly; handles queue keepalives and errors inside streams; never treats an incomplete response as complete.
- [ ] Takes context, reserve, slots and features from the serving model, and recomputes them when it changes. Hard-codes no context size.
- [ ] Uses read timeouts that allow for queueing, or streams. Doesn't abandon and resubmit queued requests.
- [ ] Logs model changes and fallbacks, and surfaces refusals from a fallback model instead of retrying them.
- [ ] Keeps a verbatim copy of this contract at `docs/llm-router-contract.md`, a conformance map, and an `AGENTS.md` rule that upholds it (§11).

## 14. References

- [Deployment capabilities and change events](CAPABILITIES.md): the full document schema, event timing and score formula.
- [API reference](API.md), including [llama.cpp sampling controls](API.md#llamacpp-sampling-controls).
- [Model discovery](MODEL_DISCOVERY.md): the `x_ollama_router` metadata fields.
- [Primary integration](PRIMARY_INTEGRATION.md): output policy, queueing, context recovery and archives.
- Reference clients, tested against the router: [Python](clients/router_watch.py) (standard library) and [JavaScript](clients/router-watch.mjs) (Node 18+ and browsers).
- [Client handoff](handoffs/2026-10-07-capability-subscribers.md): how each current project should change to meet this contract.

## 15. Changes

| Version | Change |
|---|---|
| 1.1 | §3: benchmarking and evaluation clients may pin a canonical ID for the duration of a run. §11: minor versions defined. |
| 1 | Initial contract. |

## How Bench Studio upholds this contract

Bench Studio is a **benchmarking client**. It uses both benchmark provisions of this contract:

- **Canonical pinning per run (§3).** A run starts from a service ID (`daytime`, `nighttime`). At launch the service is resolved to its canonical model, which is recorded in the run's `resolved[<service>]` with the model `revision`, context, engine image and container, and every request of the run sends that canonical ID. Canonical IDs are never configuration: launches, `./bench run` and `SESSION_SMOKE_TARGET` accept service IDs or their aliases only, and every new run (including **Run again**) starts from a current service.
- **No fallback (§5).** A benchmark's identity is its model. Bench Studio never substitutes Daytime or any other model for a target. An offline or unavailable target means *waiting*: a queued run stays queued and starts when its model returns unchanged; a running run whose model goes offline ends as **failed** (`model offline (<configuration>)`), never invalid. Only a real identity change (a different canonical model, revision, context, metadata, engine or container, a pinned ID that returns 404 `MODEL_NOT_FOUND`, or a target absent with no `offline_services` entry) makes a run **invalid**, or a queued run **blocked**.

Paths are relative to the repository root. The router integration is mainly in `common.py` (discovery snapshot, `resolve`, error classification, request gating), `studio/runner.py` (scheduler), `studio/router_events.py` and `studio/router_watch.py` (subscription), and the request paths `worker.py`, `invoke.py`, `studio/quality_worker.py`, `studio/router_stream.py` (used by `studio/session_core.py`, `studio/session_job.py` and `studio/harbor_agent.py`/`studio/agent_job.py`) and `vendor/betterbench/client.py`. Contract tests are in `tests/test_router_contract.py` (with the fixture router in `tests/router_fixture.py`) unless another file is named.

### Conformance checklist (§13)

| # | Checklist item | How Bench Studio meets it | Tests |
|---|---|---|---|
| 1 | Service IDs only, or a canonical ID pinned per run; no canonical IDs as configuration; `X-Client-Name` | `studio/api.py` `select_targets` accepts service IDs and aliases (recorded as the service) and refuses canonical IDs; the run pins `resolve(...)["canonical"]`, which every request sends (`worker.py` probe and BetterBench `--model`, `studio/quality_worker.py` `main`, `studio/session_core.py` `generate_once`, `studio/session_job.py` vision payload, `studio/agent_job.py` model name). `studio/runner.py` `repin` records the running instance at start when only the container differs. `studio/session_setup.py` matches `SESSION_SMOKE_TARGET` against service IDs and aliases only. Baselines and rankings key results by the measured canonical model; that is result data, not request configuration. Requests send `X-Client-Name: bench-studio/<run id>` (`common.client_headers`; `BETTERBENCH_CLIENT_NAME` for BetterBench; `BENCH_STUDIO_CLIENT_NAME` for session and Harbor subprocesses); discovery, the capabilities document and the event stream send `bench-studio`. | `test_requests_send_client_name`; `tests/test_studio.py` `test_new_launch_is_one_service_and_aliases_record_the_service`, `test_canonical_ids_are_never_selected_and_offline_services_never_substituted`; `tests/test_session_setup.py` `test_optional_checks_resolve_current_models_without_old_aliases`; `test_queued_run_waits_while_offline_and_starts_when_it_returns`; `vendor/tests/test_router_errors.py` `test_client_name_header_is_sent_when_configured` |
| 2 | Reads the capabilities document at startup without failing when the router or a model is unavailable | `common.fetch_capabilities` reads `GET /v1/router/capabilities?include=load`; `common.snapshot` adds AI Runtime `/api/status` only for container identity. The runner's `studio/router_events.py` `Subscriber.run` reads the document in a background thread and tolerates failure; `studio/runner.py` `start_router_watch` never blocks startup. The reports server contacts no router at startup; `GET /api/models` answers 503 with `router.reachable: false` while the router is down, and `studio/discovery.py` `fetch` tolerates an unreachable AI Runtime. Unavailable or incomplete models are listed as unavailable, never as changed. | `test_runner_starts_with_the_router_unreachable`, `test_reports_server_starts_with_the_router_unreachable`; `tests/test_integration.py` `test_discovery_marks_unhealthy_remote_model_unavailable`, `test_partial_metadata_is_rejected`; `tests/test_studio.py` `test_historical_multi_model_runs_remain_readable_when_discovery_fails` |
| 3 | Long-running: subscribes to events, polls while disconnected. Request-scoped: reads the document before each request | The runner (long-running) holds one subscription for its lifetime with the reference client (`studio/router_watch.py`, `RouterWatch.run_forever`): stream, reconnect backoff 3–30 s, 60 s dead-connection timeout, and a 30 s `If-None-Match` poll while disconnected. Each new revision is written to the SQLite events table (`router_capabilities`, shown by the UI through `/api/events`), stored as state `router`, recorded as a drain window, and wakes the scheduler to re-check active and queued runs at once (`runner.router_changed`). Between revisions the runner re-checks running runs every 30 s while the stream is live (container restarts are runtime-only facts) and every 10 s while it is down, and queued runs every 10 s (load is polled). Workers are request-scoped: `common.wait_for_runtime` reads the document before every request (function checks, sessions, vision, Harbor), `invoke.py` `gate` before every BetterBench request, and `worker.py` `check_target` every 10 s during a phase. The UI reloads models on `router_capabilities` events, with a 60 s fallback poll. | `test_new_revision_records_event_and_triggers_immediate_recheck`, `test_running_run_is_rechecked_on_new_revision_not_every_cycle`; `frontend/tests/router.spec.ts` "a router change event refreshes the model list" |
| 4 | Chooses the model per request by name or capability, configurable fallback (benchmarks: deliberately none); returns to the preferred model | By name: each request goes to the run's service, pinned to its canonical model. Fallback is deliberately disabled (§5, permitted deviation). `common.resolve` never returns another model: an absent target is `ModelOffline` (listed in `offline_services`) or `ModelConfigurationChanged`. Return: a queued run starts automatically when its model is available again with the same identity (`studio/runner.py` `consider`); a running run whose model went offline is failed and rerun by the user. | `tests/test_integration.py` `test_absent_alias_never_falls_back`; `test_offline_target_never_falls_back_to_daytime`, `test_offline_target_is_not_a_configuration_change`, `test_absent_target_without_offline_entry_is_a_configuration_change`, `test_queued_run_waits_while_offline_and_starts_when_it_returns`, `test_queued_run_blocks_when_target_returns_as_another_model`, `test_running_run_fails_not_invalid_when_target_goes_offline`, `test_running_run_is_invalid_when_target_returns_as_another_model` |
| 5 | Waits, without switching, while the router drains or is in maintenance | `common.switching_reason` (`router.accepting_requests: false`, or AI Runtime draining) makes `resolve` raise `RouterSwitching` before any verdict, so a catalog published mid-drain is judged only after the drain. Waits last at least ten minutes (`ROUTER_SWITCH_WAIT`, backoff 2–30 s): `common.wait_for_runtime` before each request, `runner.check_current` for running runs, `runner.consider` for queued runs, `worker.py` `settle`. Afterwards the model is resolved again; a change follows rows 4 and 6. Requests the router refused with `BACKEND_DRAINING`/`MAINTENANCE_MODE` (never admitted) are sent again after the wait (`common.send_when_ready`, `session_core.generate_once`, `session_job.vision_attempt`, `harbor_agent.RouterLLM`), outside measured time; a refused request does not spend a session turn. A speed phase that overlaps a drain (seen by the worker, by the request gate, or recorded by the runner's subscriber in `data/router-drains.json`) is excluded to `excluded/<phase>-<n>/` and measured again (`worker.py` `measure`), at most three times. Launches are refused while the router switches. | `test_switching_router_defers_every_verdict`, `test_router_switch_waits_beyond_health_grace_without_failing`, `test_request_gate_waits_out_a_switch_then_rechecks_identity`, `test_phase_overlapping_a_drain_is_excluded_and_measured_again`, `test_repeated_drains_fail_the_run_without_invalidating_it`, `test_drain_windows_overlap_only_their_phase`, `test_drain_rejection_is_resent_after_the_switch_and_offline_is_not`, `test_session_resends_drain_rejection_without_spending_a_turn`; `frontend/tests/router.spec.ts` "router switching is shown while the router drains" |
| 6 | Classifies errors by `error.code` (§10); `SERVICE_OFFLINE` consumes no retry or budget allowance | `common.router_error_code` reads `error.code` from object errors (tolerating string errors) and `x_router.stop_reason` of incomplete stream frames; `common.router_error` maps code first, then status: `SERVICE_OFFLINE` → `ModelOffline`; `MODEL_NOT_FOUND` for the pinned ID → `ModelConfigurationChanged`; `BACKEND_DRAINING`, `MAINTENANCE_MODE` → `RouterSwitching`; `BACKEND_UNAVAILABLE`, other 5xx, 408, 429 and network errors → `RuntimeUnavailable`; `context_length_exceeded` → `BenchmarkItemError` (the item fails, the run continues; `ContextBudgetError` in sessions); other 4xx → request error. Used by `studio/quality_worker.py` `generate`, `studio/router_stream.py` `completion`, `worker.py` `compatibility_probe` and `invoke.py` `classify` (with `error_code` kept by `vendor/betterbench/client.py`). Classes cross process boundaries as `error_kind` (worker manifests, `failure.json`, session and Harbor outcomes) and are rebuilt by `common.classified` in `runner.poll`. Bench Studio has no retry caps or circuit breakers; `SERVICE_OFFLINE` is never counted as a model failure or a session turn — it ends or pauses the run as row 4 describes. | `test_error_codes_map_to_benchmark_outcomes`, `test_stream_error_codes_include_ollama_stop_reason`, `test_every_request_path_classifies_router_errors`, `test_worker_classification_reaches_the_run`, `test_quality_run_records_context_rejection_as_a_failed_item`; `vendor/tests/test_router_errors.py` |
| 7 | Sets `stream` explicitly; handles queue keepalives and errors inside streams; never treats an incomplete response as complete | Every request sets `"stream": true` (`worker.py` probe, `vendor/betterbench/client.py` `_build_payload`, `studio/quality_worker.py` `generate`, and `studio/router_stream.py` `completion`, which overrides any profile parameter for sessions, vision and Harbor). Comment keepalives (`: waiting for inference slot`) are ignored; only `data:` frames are parsed. An error frame or `x_router.status: "incomplete"` ends the request as an error in every path (BetterBench marks the sample not ok). A response counts only with `[DONE]` (where applicable), a `stop`/`length` finish reason and real token usage; empty answers are `EmptyResponseError`. | `test_every_request_path_streams_explicitly_and_ignores_queue_keepalives`, `test_every_request_path_rejects_an_error_inside_the_stream`; `tests/test_integration.py` `test_streaming_compatibility_requires_usage_and_clean_end`, `test_invalid_samples_cannot_be_published`; `vendor/tests/test_router_errors.py` `test_error_frame_inside_a_stream_is_never_a_completed_sample` |
| 8 | Takes context, reserve, slots and features from the serving model; recomputes on change; hard-codes no context size | `common.resolve` takes `context_window` and `metadata.context_safety_reserve` from the serving model's entry; they set BetterBench's `max_model_len`/prefill margin, Harbor's input limit, session compaction and the launch output-budget check. Launch checks vision, reasoning efforts and reasoning-budget support against the model's metadata. Benchmarks send one request at a time and start only when the target's router load (`load.active`/`queued`) and backend are idle. The pinned model cannot change during a run (a change invalidates it); each new run takes the current model's limits. | `test_launch_budgets_against_the_serving_models_context`; `tests/test_integration.py` `test_discovery_follows_future_runtime_model_and_context`, `test_idle_gate_catches_existing_work`; `tests/test_sessions.py` `test_unavailable_vision_and_parallel_launch_rejected` |
| 9 | Read timeouts allow for queueing, or streams; never abandons and resubmits queued requests | All requests stream with per-read timeouts (600 s; 120 s for the probe) that queue keepalives (every 15 s) keep alive. No request is cancelled to be resubmitted; only requests the router refused while draining (not admitted, so not queued) are sent again. Benchmark time limits (session active time, vision 600 s, Harbor task time) are part of each workload and are documented with it. | `test_every_request_path_streams_explicitly_and_ignores_queue_keepalives`, `test_drain_rejection_is_resent_after_the_switch_and_offline_is_not` |
| 10 | Logs model changes and fallbacks; surfaces refusals from a fallback model | The runner logs every router revision (configuration, the model behind each service and its availability, offline services and reasons, drain state, schema warnings) and records it as an event; run logs record waiting for an offline model, its return, the start instance (`resolved_at_queue` when the container changed), excluded phases and their reasons. The UI shows each model's configuration, service, availability, capability score and NSFW badge, the offline services with reasons, and "router switching configuration". There is no fallback, so no fallback refusals exist to surface. | `test_new_revision_records_event_and_triggers_immediate_recheck`, `test_queued_run_waits_while_offline_and_starts_when_it_returns`, `test_models_api_exposes_router_state_and_offline_services`; `frontend/tests/router.spec.ts` |
| 11 | Verbatim copy at `docs/llm-router-contract.md`, conformance map, `AGENTS.md` rule | This file (copy of version 1.1 under the required header, followed by this map) and the "LLM Router contract" section of `AGENTS.md`. | `test_contract_copy_is_verbatim_with_header_map_and_agent_rule` |

### Recorded deviations and interpretations

- **Fallback disabled (§5, permitted).** See above. `SERVICE_OFFLINE` and `MODEL_NOT_FOUND` are never reasons to switch models (§3).
- **Transient failures are not replayed.** For other 5xx, 408, 429 and network failures, §10 suggests retrying the same service. A benchmark never replays an inference request whose output may have started, because a replay can change results. Instead, readiness waits (90 s for health, ten minutes for a switch) happen before every new request, and the failed request fails its run (or its item), which the user reruns. Only requests the router refused while draining are sent again.
- **Queued runs compare the model, not the container.** A queued run's identity check covers the canonical model, revision, quantization, reasoning, context, reserve, engine image and AI Runtime revision. The container instance (ID, start time, restart count) is compared from the start of the run onwards, because a solo configuration restarts Nighttime's container. The run records its start instance (and keeps the queue-time record in `resolved_at_queue`).
- **Configuration ID, capability score and NSFW are report facts.** They are recorded per run (`resolved[<service>].router`, run summaries and the run's configuration snapshot) and are not part of `identity()` or `model_fingerprint`. The AI Runtime profile, which equals the configuration ID, was therefore removed from the identity; a switch that leaves the target's model and container untouched does not invalidate its run.
- **Incomplete metadata is unavailable, not changed.** `complete: false` reports a failed source (§4 "retain the last safe limits"), so it is treated as temporarily unavailable rather than as a new model.
- **Reference client patch.** `studio/router_watch.py` is the reference client with one marked change: a `headers` argument so the capabilities and event-stream requests carry `X-Client-Name` (§2). Bench Studio uses only `RouterWatch`, not `resolve` or `pick_service`, which implement fallback.
- **Harbor time limits.** Harbor's per-task time limit is wall-clock and includes any router-switch wait before a request; a long switch during a repository task can exhaust that task's time.
