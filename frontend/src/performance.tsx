type Obj = Record<string, any>;
export const primaryMetrics = [
  "output_tps",
  "ttft_seconds",
  "prompt_tps",
  "request_seconds",
];
const labels = [
  "Output speed",
  "First token",
  "Prompt processing",
  "Request latency",
];
const descriptions: Record<string, string> = {
  output_tps:
    "Median client-observed output tokens/s, including thinking. (Output tokens − 1) ÷ (request time − first-token time).",
  ttft_seconds:
    "Median time from sending the request to its first output, including thinking.",
  prompt_tps:
    "Median backend prompt-processing speed, using processed tokens and processing time. Cached tokens are excluded.",
  request_seconds:
    "Median complete response time. Tool execution and time between requests are excluded.",
  prompt_estimate_tps:
    "Input tokens ÷ time to first token. Includes network and waiting time and may include cached input; this is not native prompt-processing speed.",
  update_gap_ms:
    "Time between streamed updates. An update may contain multiple tokens.",
};
export function findMetric(summary: Obj, id: string): Obj {
  return (
    summary?.performance?.metrics?.find((m: Obj) => m.id === id) || {
      id,
      label: labels[primaryMetrics.indexOf(id)] || id,
      value: null,
      direction: ["output_tps", "prompt_tps"].includes(id) ? "higher" : "lower",
      unavailable: "Not recorded",
      samples: 0,
      total: 0,
    }
  );
}
export function metricValue(m: Obj) {
  return typeof m.value === "number"
    ? `${m.value.toLocaleString(undefined, { minimumFractionDigits: m.precision ?? 1, maximumFractionDigits: m.precision ?? 1 })} ${m.unit}`
    : m.unavailable || "Not recorded";
}
export function MetricRanks({ metric: m }: { metric: Obj }) {
  if (!m.model_rank) return null;
  return (
    <div className="bs-small bs-metric-ranks">
      <span>
        Model #{m.model_rank.rank} of {m.model_rank.total}
        {m.model_rank.tied ? " (tied)" : ""}
      </span>
      <span>
        All models #{m.overall_rank.rank} of {m.overall_rank.total}
        {m.overall_rank.tied ? " (tied)" : ""}
      </span>
    </div>
  );
}
function Change({
  metric: m,
  details = false,
}: {
  metric: Obj;
  details?: boolean;
}) {
  if (m.value == null || !m.model_rank) return null;
  const previous = m.previous;
  if (!previous)
    return <div className="bs-small">No previous matching run</div>;
  const change = previous.improvement;
  return (
    <div className="bs-metric-change">
      <div
        className={
          change > 0 ? "bs-positive" : change < 0 ? "bs-negative" : "bs-small"
        }
      >
        {change == null
          ? "Change unavailable (previous value was zero)"
          : Math.abs(change) < 0.05
            ? "Unchanged"
            : `${Math.abs(change).toFixed(1)}${previous.unit === "pp" ? " pp" : "%"} ${change > 0 ? "better" : "worse"}`}{" "}
        vs previous
      </div>
      {details && (
        <details className="bs-small">
          <summary>Comparison conditions</summary>
          <p>
            Previous run: {previous.run_id} · {previous.target}
          </p>
          {previous.differences?.length ? (
            <dl className="bs-condition-differences">
              {previous.differences.map((d: Obj) => (
                <div key={d.field}>
                  <dt>{d.field}</dt>
                  <dd>
                    {JSON.stringify(d.a) ?? "Unknown"} →{" "}
                    {JSON.stringify(d.b) ?? "Unknown"}
                  </dd>
                </div>
              ))}
            </dl>
          ) : (
            <p>Recorded tuning conditions match.</p>
          )}
          <p>
            Descriptive differences between independent runs; they do not
            establish causation.
          </p>
        </details>
      )}
    </div>
  );
}
export function PerformanceCell({
  summary,
  id,
  ranks = false,
}: {
  summary: Obj;
  id: string;
  ranks?: boolean;
}) {
  const m = findMetric(summary, id);
  return (
    <div className="bs-performance-cell" title={descriptions[id]}>
      <div className={m.value == null ? "bs-small" : "bs-value"}>
        {metricValue(m)}
      </div>
      {ranks && (
        <>
          <MetricRanks metric={m} />
          <Change metric={m} />
        </>
      )}
    </div>
  );
}
export function PerformanceScorecard({ summary }: { summary: Obj }) {
  const perf = summary.performance || {};
  return (
    <section aria-label="Performance scorecard" className="bs-performance">
      <div className="bs-performance-heading">
        <div>
          <h2>Performance</h2>
          <span className="bs-small">
            Median per request · client-observed output includes thinking
          </span>
        </div>
        {summary.passed != null && (
          <span className="bs-quality-indicator">
            {summary.passed} / {summary.count ?? "—"} tasks passed
          </span>
        )}
      </div>
      <div className="bs-performance-grid">
        {primaryMetrics.map((id) => {
          const m = findMetric(summary, id);
          return (
            <section className="bs-surface" key={id} aria-label={m.label}>
              <h3>
                {m.label}{" "}
                <span aria-hidden="true">
                  {m.direction === "higher"
                    ? "↑"
                    : m.direction === "lower"
                      ? "↓"
                      : ""}
                </span>
              </h3>
              <div
                className={m.value == null ? "bs-metric-unavailable" : "bs-big"}
              >
                {metricValue(m)}
              </div>
              <p className="bs-small">
                {m.samples} / {m.total} requests ·{" "}
                {m.direction === "higher" ? "Higher" : "Lower"} is better
              </p>
              <MetricRanks metric={m} />
              <Change metric={m} details />
              <details className="bs-small bs-metric-help">
                <summary>How it’s measured</summary>
                <p>{descriptions[id]}</p>
                {m.unavailable_reason && <p>{m.unavailable_reason}</p>}
              </details>
            </section>
          );
        })}
      </div>
      {perf.ranking_unavailable && (
        <p className="bs-small">
          Unranked: {perf.ranking_unavailable}. Available measurements are
          descriptive.
        </p>
      )}
      <p className="bs-small">
        Ranks compare matching workloads in this machine’s history.{" "}
        {perf.request_errors ?? 0} request errors ·{" "}
        {perf.output_limit_requests ?? 0} responses reached the output limit.
      </p>
    </section>
  );
}
export function PerformanceDetails({ summary }: { summary: Obj }) {
  const metrics: Obj[] = summary.performance?.metrics || [];
  return (
    <details className="bs-surface bs-performance-details">
      <summary>
        Performance details · variability and supporting metrics
      </summary>
      <p className="bs-small">
        Each metric uses eligible evidence only. Failed requests remain in the
        request count. Tail percentiles require at least 100 samples.
      </p>
      <div className="bs-table-wrap">
        <table>
          <thead>
            <tr>
              <th>Metric</th>
              <th>Value</th>
              <th>Samples</th>
              <th>Middle 50%</th>
              <th>Tail</th>
              <th>Ranks and change</th>
            </tr>
          </thead>
          <tbody>
            {metrics.map((m) => (
              <tr key={m.id}>
                <td>
                  {m.label}
                  <div className="bs-small">{descriptions[m.id]}</div>
                </td>
                <td>{metricValue(m)}</td>
                <td>
                  {m.samples}
                  {m.coverage_unit ? ` ${m.coverage_unit}` : ` / ${m.total}`}
                </td>
                <td>
                  {m.distribution && m.id !== "task_score"
                    ? `${m.distribution.q1.toFixed(m.precision)}–${m.distribution.q3.toFixed(m.precision)} ${m.unit}`
                    : "—"}
                </td>
                <td>
                  {m.distribution?.tail != null
                    ? `${m.distribution.tail_label}: ${m.distribution.tail.toFixed(m.precision)} ${m.unit}`
                    : "—"}
                </td>
                <td>
                  <MetricRanks metric={m} />
                  <Change metric={m} details />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}
export function PerformanceComparison({ a, b }: { a: Obj; b: Obj }) {
  return (
    <section className="bs-surface">
      <h3>Performance comparison</h3>
      <div className="bs-table-wrap">
        <table>
          <thead>
            <tr>
              <th>Metric</th>
              <th>Baseline run</th>
              <th>Selected run</th>
            </tr>
          </thead>
          <tbody>
            {primaryMetrics.map((id) => (
              <tr key={id}>
                <td>{findMetric(a, id).label}</td>
                <td>
                  <PerformanceCell summary={a} id={id} ranks />
                </td>
                <td>
                  <PerformanceCell summary={b} id={id} ranks />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
