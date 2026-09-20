import { useState, useRef } from "react";
type Obj = Record<string, any>;
const format = (value: any) =>
  typeof value === "number"
    ? value.toLocaleString(undefined, { maximumFractionDigits: 1 })
    : "—";
const title = (value: string) => value.replaceAll("_", " ");
export const isSession = (profile: Obj | undefined) =>
  ["session", "vision"].includes(profile?.family);
export function sessionDefaults(profile: Obj | undefined) {
  return {
    difficulty: profile?.difficulty,
    task_selection: profile?.task_selection || "all",
    repetitions: profile?.repetitions || 1,
    review_mode:
      profile?.review_mode ||
      (profile?.family === "vision" ? "unattended" : "interactive"),
  };
}
export function SessionOptions({
  profile,
  value,
  onChange,
}: {
  profile: Obj;
  value: Obj;
  onChange: (value: Obj) => void;
}) {
  const tasks = (profile.tasks || []).filter(
    (t: Obj) => !value.difficulty || t.difficulty === value.difficulty,
  );
  const count =
    (value.task_selection === "all" ? tasks.length : 1) * value.repetitions;
  return (
    <section className="bs-session-options">
      <div className="bs-form-grid">
        {profile.family === "session" && (
          <label className="bs-field">
            Task difficulty
            <select
              aria-label="Task difficulty"
              value={value.difficulty}
              onChange={(e) =>
                onChange({
                  ...value,
                  difficulty: e.target.value,
                  task_selection: "all",
                })
              }
            >
              {profile.difficulties.map((d: string) => (
                <option key={d} value={d}>
                  {d[0].toUpperCase() + d.slice(1)}
                </option>
              ))}
            </select>
            <small>Difficulty changes the feature, not the sample count.</small>
          </label>
        )}
        <label className="bs-field">
          Task selection
          <select
            aria-label="Task selection"
            value={value.task_selection}
            onChange={(e) =>
              onChange({ ...value, task_selection: e.target.value })
            }
          >
            <option value="all">
              All tasks in this {profile.family === "vision" ? "suite" : "tier"}
            </option>
            {tasks.map((t: Obj) => (
              <option key={t.id} value={t.id}>
                {t.title}
              </option>
            ))}
          </select>
        </label>
        <label className="bs-field">
          Repetitions
          <select
            aria-label="Repetitions"
            value={value.repetitions}
            onChange={(e) =>
              onChange({ ...value, repetitions: Number(e.target.value) })
            }
          >
            {[1, 3, 5].map((n) => (
              <option key={n} value={n}>
                {n} per task
              </option>
            ))}
          </select>
        </label>
        {profile.family === "session" && (
          <label className="bs-field">
            Review mode
            <select
              aria-label="Review mode"
              value={value.review_mode}
              onChange={(e) =>
                onChange({ ...value, review_mode: e.target.value })
              }
            >
              <option value="interactive">
                Interactive · approve plans yourself
              </option>
              <option value="unattended">
                Unattended · fixed approval protocol
              </option>
            </select>
          </label>
        )}
      </div>
      <p className="bs-small">
        {count} independent attempts per model · clean checkout for each attempt
        {profile.family === "session"
          ? " · human review pauses are excluded from active time"
          : ""}
      </p>
    </section>
  );
}
function Artifact({
  run,
  path,
  label,
}: {
  run: Obj;
  path: string;
  label: string;
}) {
  return (
    <a
      href={`/api/runs/${run.id}/artifacts/${path.split("/").map(encodeURIComponent).join("/")}`}
      target="_blank"
      rel="noreferrer"
    >
      {label}
    </a>
  );
}
function ReviewPanel({ run, review }: { run: Obj; review: Obj }) {
  const [feedback, setFeedback] = useState(""),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [decision, setDecision] = useState("");
  const submission = useRef({ payload: "", key: "" });
  const submit = async (action: string) => {
    setBusy(true);
    setError("");
    try {
      const key = `${run.id}:${review.target}:${review.attempt_id}:${review.revision}:${action}:${feedback}`;
      if (submission.current.payload !== key)
        submission.current = {
          payload: key,
          key: Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) =>
            b.toString(16).padStart(2, "0"),
          ).join(""),
        };
      const id = submission.current.key;
      const response = await fetch(
        `/api/runs/${run.id}/reviews/${encodeURIComponent(review.target)}/${encodeURIComponent(review.attempt_id)}/${review.revision}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            action,
            feedback: action === "revise" ? feedback : "",
            idempotency_key: id,
          }),
        },
      );
      const body = await response.json();
      if (!response.ok) throw Error(body.detail || "Review failed");
      setDecision(action);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };
  const disabled = busy || !!decision || !!review.decision;
  return (
    <section className="bs-surface bs-review" aria-label="Pending review">
      <h2>
        {review.kind === "plan" ? "Review the plan" : "Review the prototype"}
      </h2>
      <p>
        {review.target} · {review.attempt_id} · revision {review.revision}
      </p>
      <Artifact
        run={run}
        path={review.artifact}
        label={review.kind === "plan" ? "Open plan" : "Open prototype"}
      />
      {review.kind === "plan" ? (
        <PlanPreview run={run} path={review.artifact} />
      ) : (
        <iframe
          title="Interactive prototype"
          sandbox="allow-scripts"
          src={`/api/runs/${run.id}/artifacts/${review.artifact}`}
          className="bs-prototype"
        />
      )}
      <label className="bs-field">
        Revision feedback
        <textarea
          aria-label="Revision feedback"
          value={feedback}
          maxLength={8000}
          disabled={disabled}
          onChange={(e) => setFeedback(e.target.value)}
          placeholder="Describe changes within the original requirements"
        />
      </label>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      {decision || review.decision ? (
        <p role="status">
          Decision recorded: {decision || review.decision}. The scheduler will
          resume when idle.
        </p>
      ) : (
        <div className="bs-inline">
          <button
            className="bs-button bs-primary"
            disabled={disabled}
            onClick={() => void submit("approve")}
          >
            Approve {review.kind}
          </button>
          <button
            className="bs-button"
            disabled={disabled || !feedback.trim()}
            onClick={() => void submit("revise")}
          >
            Request revision
          </button>
          <button
            className="bs-button"
            disabled={disabled}
            onClick={() => void submit("stop")}
          >
            Stop session
          </button>
        </div>
      )}
    </section>
  );
}
import { useEffect } from "react";
function PlanPreview({ run, path }: { run: Obj; path: string }) {
  const [plan, setPlan] = useState<Obj | null>(null);
  useEffect(() => {
    let active = true;
    fetch(`/api/runs/${run.id}/artifacts/${path}`)
      .then((r) => r.json())
      .then((p) => {
        if (active) setPlan(p);
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, [run.id, path]);
  return plan ? (
    <div className="bs-plan">
      <p>{plan.summary}</p>
      <ol>
        {plan.steps?.map((s: string, i: number) => (
          <li key={i}>{s}</li>
        ))}
      </ol>
      <h3>Verification</h3>
      <ul>
        {plan.checks?.map((s: string, i: number) => (
          <li key={i}>{s}</li>
        ))}
      </ul>
    </div>
  ) : (
    <p>Loading plan…</p>
  );
}
export function SessionDetail({
  run,
  summary,
  target,
}: {
  run: Obj;
  summary: Obj;
  target: string;
}) {
  const finished = [
    "completed",
    "failed",
    "interrupted",
    "cancelled",
    "invalid",
  ].includes(run.status);
  const pending = finished
    ? []
    : (run.reviews || []).filter((r: Obj) => !r.decision);
  const phase =
    !run.session_progress?.target || run.session_progress.target === target
      ? run.session_progress
      : null;
  const tasks: Obj[] = summary.tasks || [];
  const lastAttempt = tasks.find((task) => task.id === phase?.attempt_id);
  const verified = tasks.filter(
    (task) => task.verification_attempts > 0,
  ).length;
  const failed = tasks.filter((task) => task.status === "failed");
  const outputFailures = failed.filter(
    (task) => task.failure_kind === "output_limit",
  );
  const usage = summary.usage || {};
  const inputTokens = usage.prompt_tokens ?? usage.known_prompt_tokens;
  const outputTokens = usage.completion_tokens ?? usage.known_completion_tokens;
  const vision = run.host?.vision?.[target];
  return (
    <div className="bs-session-detail">
      {pending.map((r: Obj) => (
        <ReviewPanel
          key={`${r.target}:${r.attempt_id}:${r.revision}`}
          run={run}
          review={r}
        />
      ))}
      {finished && (
        <section className="bs-surface" aria-label="Attempt outcomes">
          <h3>
            {summary.passed ?? 0} / {summary.count ?? "—"} attempts passed
          </h3>
          <p className="bs-small">
            {failed.length} failed attempts
            {run.family === "session" &&
              ` · ${verified} / ${tasks.length} reached verification`}
          </p>
          {outputFailures.length > 0 && (
            <p>
              {outputFailures.length} attempts stopped because a response
              reached the {format(run.profile_spec?.parameters?.max_tokens)}
              -token limit for thinking and the answer. See each attempt below
              for its stopping point and saved responses.
            </p>
          )}
          {summary.passed === 0 && run.family === "session" && (
            <p className="bs-small">
              Successful-attempt timings are unavailable because no attempt
              passed. Total time and request evidence include the failed
              attempts.
            </p>
          )}
        </section>
      )}
      {phase && (
        <section className="bs-surface">
          <h3>{finished ? "Session timeline" : "Session progress"}</h3>
          <ol className="bs-phase-strip">
            {(run.family === "vision"
              ? ["preparation", "visual_review"]
              : ["planning", "review_wait", "implementation", "verification"]
            ).map((p) => (
              <li
                key={p}
                aria-current={
                  !finished && phase.phase === p ? "step" : undefined
                }
              >
                {title(p)}
              </li>
            ))}
          </ol>
          <p>
            {phase.attempt_id} ·{" "}
            {title(finished ? lastAttempt?.status || run.status : phase.phase)}{" "}
            · {phase.turns ?? 0} model turns
            {!finished && phase.generation?.active && (
              <span>
                {" "}
                ·{" "}
                {phase.generation.characters_received
                  ? `Receiving response (${phase.generation.characters_received.toLocaleString()} characters)`
                  : "Waiting for model response"}
              </span>
            )}
          </p>
          {run.queue_seconds != null && (
            <p className="bs-small">
              Initial queue wait: {format(run.queue_seconds)}s · excluded from
              active time
            </p>
          )}
          {phase.timing?.timeline?.length > 0 && (
            <details>
              <summary>Phase timeline</summary>
              <ol>
                {phase.timing.timeline
                  .slice(-20)
                  .map((event: Obj, i: number) => (
                    <li key={i}>
                      {title(event.phase)} · {format(event.seconds)}s
                    </li>
                  ))}
              </ol>
            </details>
          )}
        </section>
      )}
      {run.profile_spec?.requires_vision && (
        <section className="bs-surface">
          <h3>Vision conditions</h3>
          <dl className="bs-kv">
            <dt>Advertised image support</dt>
            <dd>
              {vision?.advertised == null
                ? "Unknown"
                : vision.advertised
                  ? "Available"
                  : "Unavailable"}
            </dd>
            <dt>Projector identity</dt>
            <dd>
              {vision?.projector_sha256 ||
                vision?.projector_revision ||
                vision?.projector_path ||
                "Unknown"}
            </dd>
            <dt>Projector offload</dt>
            <dd>
              {vision?.offload == null ? "Unknown" : String(vision.offload)}
            </dd>
            <dt>Encoder-only latency</dt>
            <dd>
              {vision?.encoder_latency_ms == null
                ? "Not exposed by backend"
                : `${format(vision.encoder_latency_ms)} ms`}
            </dd>
          </dl>
          <p className="bs-small">
            Time to first token includes work beyond image encoding. Screenshot
            dimensions and browser settings are saved with the artifacts.
          </p>
        </section>
      )}
      <p className="bs-small">
        {summary.passed ?? 0} / {summary.count ?? "—"} successful attempts ·{" "}
        {run.profile_spec?.repetitions} repetitions per task
        {summary.delta != null
          ? ` · ${format(summary.delta)} percentage points vs baseline`
          : ""}
      </p>
      <div className="bs-stats">
        {(summary.session_metrics || []).map((m: Obj) => (
          <section className="bs-surface" key={m.name}>
            <span className="bs-small">{m.label}</span>
            <div className="bs-big" style={{ fontSize: 28 }}>
              {format(m.value)} <small>{m.unit}</small>
            </div>
            <span className="bs-small">
              {m.value == null &&
              summary.passed === 0 &&
              ["implementation_seconds", "active_seconds"].includes(m.name)
                ? "No successful attempts"
                : m.direction === "lower"
                  ? "Lower is better"
                  : "Higher is better"}
            </span>
          </section>
        ))}
      </div>
      <section className="bs-surface">
        <h3>Session evidence · {target}</h3>
        <p className="bs-small">
          {summary.compaction_count ?? 0} compactions · {usage.requests ?? 0}{" "}
          requests ·{" "}
          {usage.complete === false && inputTokens != null ? "at least " : ""}
          {format(inputTokens)} input tokens ·{" "}
          {usage.complete === false && outputTokens != null ? "at least " : ""}
          {format(outputTokens)} output tokens
        </p>
        {usage.complete === false && (
          <p className="bs-small">
            Token usage is incomplete
            {usage.reported_requests != null
              ? `: ${usage.reported_requests} of ${usage.requests} requests reported usage`
              : ""}
            . Available totals are lower bounds; missing usage is unknown.
          </p>
        )}
        {summary.output_limit_requests > 0 && (
          <p className="bs-small">
            {summary.output_limit_requests} responses reached the output limit.
            Their time and reported tokens are included.
          </p>
        )}
        {tasks.map((task: Obj) => (
          <details key={task.id}>
            <summary>
              {task.id} · {task.status} · {format(task.active_seconds)}s active
              {task.failure_kind && ` · ${title(task.failure_kind)}`}
            </summary>
            <p>{task.detail || "Evidence retained for this attempt."}</p>
            <p>{task.failure_kind && `Outcome: ${title(task.failure_kind)}`}</p>
            {task.status === "failed" && (
              <p>
                Stopped during{" "}
                {title(
                  task.failure_phase ||
                    task.requests?.at(-1)?.phase ||
                    "unknown phase",
                )}
                {run.family === "session" &&
                  ` · ${task.verification_attempts ?? 0} verification attempts`}
              </p>
            )}
            <dl className="bs-timing">
              {Object.entries(task.phase_seconds || {}).map(
                ([phase, seconds]) => (
                  <ReactFragment key={phase} phase={phase} seconds={seconds} />
                ),
              )}
            </dl>
            {task.review_history?.length > 0 && (
              <details>
                <summary>Review history</summary>
                <ol>
                  {task.review_history.map((review: Obj, i: number) => (
                    <li key={i}>
                      {review.kind} · revision {review.revision} ·{" "}
                      {review.decision}
                      {review.source === "fixed_protocol"
                        ? " · unattended protocol"
                        : ""}
                      {review.feedback && <p>{review.feedback}</p>}
                    </li>
                  ))}
                </ol>
              </details>
            )}
            {task.artifact_root && (
              <p>
                <Artifact
                  run={run}
                  path={`${task.artifact_root}/attempt.json`}
                  label="Full timing and request evidence"
                />
              </p>
            )}
            {run.family === "session" && task.requests?.length > 0 && (
              <details>
                <summary>Model requests · {task.requests.length}</summary>
                <div className="bs-table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>Response</th>
                        <th>Phase</th>
                        <th>Outcome</th>
                        <th>Seconds</th>
                        <th>Input tokens</th>
                        <th>Output tokens</th>
                      </tr>
                    </thead>
                    <tbody>
                      {task.requests.map((request: Obj, i: number) => (
                        <tr key={i}>
                          <td>
                            {task.artifact_root ? (
                              <Artifact
                                run={run}
                                path={`${task.artifact_root}/response-${String(i + 1).padStart(4, "0")}.json`}
                                label={`Response ${i + 1}`}
                              />
                            ) : (
                              i + 1
                            )}
                          </td>
                          <td>{title(request.phase || "unknown")}</td>
                          <td>
                            {request.finish_reason === "length"
                              ? "Output limit"
                              : request.error ||
                                request.finish_reason ||
                                "Unknown"}
                          </td>
                          <td>{format(request.elapsed_seconds)}</td>
                          <td>{format(request.usage?.prompt_tokens)}</td>
                          <td>{format(request.usage?.completion_tokens)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </details>
            )}
            {task.status === "passed" &&
              task.artifact_root &&
              task.verification_attempts > 0 && (
                <p className="bs-inline">
                  <Artifact
                    run={run}
                    path={`${task.artifact_root}/verification-${task.verification_attempts}/change.patch`}
                    label="Verified patch"
                  />
                  <Artifact
                    run={run}
                    path={`${task.artifact_root}/commit-message.txt`}
                    label="Suggested commit message"
                  />
                  <Artifact
                    run={run}
                    path={`${task.artifact_root}/verification-${task.verification_attempts}/verification.json`}
                    label="Verification evidence"
                  />
                  <Artifact
                    run={run}
                    path={`${task.artifact_root}/verification-${task.verification_attempts}/changed-files.txt`}
                    label="Changed files"
                  />
                  {run.profile_spec?.suite === "visual-design" && (
                    <Artifact
                      run={run}
                      path={`${task.artifact_root}/verification-${task.verification_attempts}/browser.json`}
                      label="Interaction and layout checks"
                    />
                  )}
                </p>
              )}
            {run.profile_spec?.suite === "visual-design" &&
              task.status === "passed" && (
                <>
                  <p>
                    {task.human_approved
                      ? "Human design approval recorded"
                      : "Automated acceptance · no human design approval"}
                  </p>
                  <iframe
                    title={`Prototype ${task.id}`}
                    sandbox="allow-scripts"
                    loading="lazy"
                    className="bs-prototype"
                    src={`/api/runs/${run.id}/artifacts/${task.artifact_root}/verification-${task.verification_attempts}/prototype.html`}
                  />
                </>
              )}
          </details>
        ))}
      </section>
    </div>
  );
}
import { Fragment } from "react";
function ReactFragment({ phase, seconds }: { phase: string; seconds: any }) {
  return (
    <Fragment>
      <dt>{title(phase)}</dt>
      <dd>{format(seconds)}s</dd>
    </Fragment>
  );
}
export function SessionComparison({ paired }: { paired: Obj }) {
  return (
    <section className="bs-surface">
      <h3>Time on matching successful attempts</h3>
      <p>
        {paired.matched_count} matched attempts. Completion rates above include
        unsuccessful attempts.
      </p>
      {paired.warning && <p className="bs-alert">{paired.warning}</p>}
      <table>
        <thead>
          <tr>
            <th>Measurement</th>
            <th>Baseline</th>
            <th>Selected</th>
            <th>Improvement</th>
          </tr>
        </thead>
        <tbody>
          {paired.timings.map((t: Obj) => (
            <tr key={t.name}>
              <td>{title(t.name)}</td>
              <td>{format(t.a)}s</td>
              <td>{format(t.b)}s</td>
              <td>
                {t.improvement_percent == null
                  ? "—"
                  : `${format(t.improvement_percent)}%`}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
