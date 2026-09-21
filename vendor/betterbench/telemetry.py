"""Optional streaming measurements; collecting them never changes generation."""

import math


class StreamTelemetry:
    def __init__(self, started):
        self.started = started
        self.first = self.last = self.answer = None
        self.gaps = []
        self.updates = 0
        self.timings = {}
        self.reasoning_channel = False

    def observe(self, chunk, at):
        timing = chunk.get("timings")
        if isinstance(timing, dict):
            for key in (
                "prompt_n",
                "prompt_ms",
                "predicted_n",
                "predicted_ms",
                "cache_n",
            ):
                value = timing.get(key)
                if type(value) in (float, int) and math.isfinite(value) and value >= 0:
                    self.timings[key] = value
        # Some compatible gateways retain Ollama's native nanosecond durations.
        for source, target, scale in (
            ("prompt_eval_count", "prompt_n", 1),
            ("prompt_eval_duration", "prompt_ms", 1e-6),
            ("eval_count", "predicted_n", 1),
            ("eval_duration", "predicted_ms", 1e-6),
        ):
            value = chunk.get(source)
            if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                self.timings[target] = value * scale if scale != 1 else value
        output = False
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            content = delta.get("content")
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if isinstance(reasoning, str) and reasoning:
                self.reasoning_channel = True
                output = True
            if isinstance(content, str) and content:
                output = True
                if self.answer is None:
                    self.answer = at
        if output:
            if self.first is None:
                self.first = at
            if self.last is not None:
                self.gaps.append((at - self.last) * 1000)
            self.last = at
            self.updates += 1

    def evidence(self, ended):
        def milliseconds(at):
            return (at - self.started) * 1000 if at is not None else None

        return {
            "timing_version": 1,
            "ttft_ms": milliseconds(self.first),
            "ttfa_ms": milliseconds(self.answer) if self.reasoning_channel else None,
            "first_output_ms": milliseconds(self.first),
            "last_output_ms": milliseconds(self.last),
            "completion_ms": milliseconds(ended),
            "elapsed_seconds": ended - self.started,
            "update_gaps_ms": list(self.gaps),
            "n_updates": self.updates,
            "timings": dict(self.timings) or None,
        }
