"""Evidence-based output diagnostics; never rewrite responses or retry requests."""


def output_diagnostics(response):
    answer = response.get("response", response.get("content", "")) or ""
    reasoning = response.get("reasoning", response.get("reasoning_content", "")) or ""
    exhausted = response.get("finish_reason") == "length"
    repeated = 0
    # Conservative signal: at least 512 characters of exact terminal repetition.
    # This is a diagnostic, not a correctness judgment or a stopping rule.
    tail = reasoning[-12000:]
    for width in range(1, min(128, len(tail) // 4) + 1):
        unit = tail[-width:]
        n = 0
        while n + width <= len(tail) and tail[len(tail)-n-width:len(tail)-n] == unit:
            n += width
        if n >= max(512, width * 4):
            repeated = n
            break
    return {
        "output_exhausted": exhausted,
        "answer_chars": len(answer),
        "reasoning_chars": len(reasoning),
        "repetition_detected": bool(repeated),
        "repeated_tail_chars": repeated,
        "failure_kind": ("output_exhausted" if exhausted else "empty_answer" if not answer.strip() else None),
    }


def exception_message(exc):
    children = getattr(exc, "exceptions", None)
    if children:
        return "; ".join(exception_message(e) for e in children)
    return f"{type(exc).__name__}: {exc}"
