# Performance measurements

Run summaries expose an additive `performance` object in list, detail, and comparison responses. Existing scores, baseline keys, and comparison eligibility retain their meanings. `studio/performance.py` builds projections from saved request evidence without changing raw artifacts. A history snapshot summarizes each run once, then groups and sorts eligible metric values in memory; baseline enrichment reuses those summaries.

The headline values are medians over eligible requests, not token-weighted averages:

| Metric | Measurement |
| --- | --- |
| Output speed | `(completion_tokens - 1) / (elapsed_seconds - ttft_ms / 1000)`; includes thinking |
| First token | Request start to first streamed output, including thinking |
| Prompt processing | Backend `prompt_n / (prompt_ms / 1000)`; excludes cached tokens |
| Request latency | Request start to complete response |

Output speed requires actual integer usage greater than one token and a positive measured duration after the first output. Request errors and incomplete streams contribute to coverage/failure counts, not performance samples. A complete generation that reaches its output limit still has valid timing; the output-limit count and task outcome remain visible. Requests from tasks that fail verification are included. Backend processing timings, client-observed output speed, and the prompt-tokens/TTFT estimate are distinct metrics with separate measurement identities.

Supporting values include time per output token, first answer when a separate reasoning channel identifies it, inter-update gaps, native generation speed, task pass rate, and successful-attempt active/implementation time. Gaps are not called per-token latency because one stream update may contain several tokens. Distributions expose the middle 50%; p5 speed and p95 latency require at least 100 eligible samples. Task scores are aggregate percentages and do not have request distributions.

`performance.metrics` contains each metric's ID, label, unit, direction, display precision, method, value, coverage, distribution, unavailable reason, model/all-model ranks, and previous comparison. `tasks[].performance` supplies task aggregates and `requests[].performance_values` supplies compact request measurements. The scorecard explains missing historical fields as **Not recorded**; a new recording with no optional backend timing reports **Not exposed**.

Rankings use matching workload definitions and canonical model identities, independently of target aliases. Cohorts include task selection, workload size/difficulty, repetition count, execution mode, task time/turn budgets, harness/verifier versions, and session fixture/image identities. Names, copied-profile IDs, runtime revisions, context allocation, and generation tuning parameters are not cohort boundaries; recorded changes are exposed in comparison conditions. Missing old workload definitions fall back conservatively to their profile lineage. Each run/target result is one ranked entry; targets within the same run are never previous-run references. Rankings use all retained eligible history, while changes use the most recent earlier eligible matching result.

Only completed results with complete benchmark evidence are ranked; infrastructure errors, partial results, and detected unrelated activity are excluded. Recovered request errors do not invalidate an otherwise completed benchmark. Different measurement methods never share ranks. Ties use displayed precision and competition ranking. Pass-rate changes use percentage points; other changes use direction-aware percentages, withheld when the previous value is zero. Successful-attempt timing ranks also require identical successful task sets and review-feedback signatures.

Optional stream telemetry is shared by the session/repository and function collectors and the vendored speed client. It retains first/last output, identifiable first answer, completion time, update gaps, real usage, and exposed backend/cache counts and durations. Repository measurements live in each trial's `agent/performance-requests` directory. Missing/corrupt optional repository timing cannot change the verifier's verdict. Native timing collection does not enable backend flags, change the router, or issue additional model requests.

The metric choices follow the [llama.cpp community's prompt/generation comparisons](https://github.com/ggml-org/llama.cpp/discussions/4167), [vLLM's latency definitions](https://docs.vllm.ai/en/latest/benchmarking/cli/), and [llama.cpp's response timing fields](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md).
