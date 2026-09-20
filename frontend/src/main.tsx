import React, { useEffect, useState, useCallback, useRef } from "react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  ArrowLeft,
  Bookmark,
  Check,
  CheckCircle2,
  ChartNoAxesCombined,
  Code2,
  Download,
  GitCompareArrows,
  GitPullRequest,
  Info,
  Layers,
  LoaderCircle,
  Moon,
  Play,
  Plus,
  RotateCcw,
  Square,
  Sun,
  Zap,
} from "lucide-react";
import {
  ResponsiveContainer,
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  CartesianGrid,
  BarChart,
  Bar,
  Legend,
} from "recharts";
import "./style.css";
import {
  isSession,
  sessionDefaults,
  SessionOptions,
  SessionDetail,
  SessionComparison,
} from "./sessions";
type Obj = Record<string, any>;
const runName = (run: Obj) =>
  (
    ({
      coding: "Coding throughput",
      "coding-checks": "Function checks",
    }) as Record<string, string>
  )[run.profile] ||
  run.profile_spec?.name ||
  run.profile;
const terminal = new Set([
  "completed",
  "failed",
  "invalid",
  "cancelled",
  "interrupted",
]);
const fmt = (x: any, n = 1) =>
  typeof x === "number"
    ? x.toLocaleString(undefined, { maximumFractionDigits: n })
    : "—";
const date = (s: string) =>
  new Date(s).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
const label = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : "");
const elapsed = (r: Obj) => {
  if (!r.started_at) return "Not started";
  const v = Math.max(
    0,
    Math.floor(
      (Date.parse(r.finished_at || new Date().toISOString()) -
        Date.parse(r.started_at)) /
        1000,
    ),
  );
  return `${Math.floor(v / 60)}m ${v % 60}s`;
};
async function api(path: string, body?: any) {
  const r = await fetch(
    "/api" + path,
    body === undefined
      ? undefined
      : {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
  );
  const d = await r.json();
  if (!r.ok)
    throw Error(
      typeof d.detail === "string"
        ? d.detail
        : JSON.stringify(d.detail || d.error || d),
    );
  return d;
}
function Pill({ status }: { status: string }) {
  return (
    <span
      className={
        "bs-pill " +
        (status === "completed"
          ? "good"
          : ["failed", "invalid", "blocked"].includes(status)
            ? "warn"
            : terminal.has(status)
              ? ""
              : "accent")
      }
    >
      {label(status)}
    </span>
  );
}
function Button({
  children,
  onClick,
  primary = false,
  disabled = false,
}: {
  children: React.ReactNode;
  onClick?: () => void;
  primary?: boolean;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      className={"bs-button" + (primary ? " bs-primary" : "")}
      onClick={onClick}
      disabled={disabled}
    >
      {children}
    </button>
  );
}
function ProfileIcon({ family }: { family: string }) {
  return family === "agent" ? (
    <GitPullRequest size={18} />
  ) : family === "quality" ? (
    <Code2 size={18} />
  ) : (
    <Zap size={18} />
  );
}
function App() {
  const [view, setView] = useState("runs"),
    [runs, setRuns] = useState<Obj[]>([]),
    [profiles, setProfiles] = useState<Obj[]>([]),
    [models, setModels] = useState<Obj[]>([]),
    [runtime, setRuntime] = useState<Obj>({}),
    [health, setHealth] = useState<Obj>({}),
    [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [selected, setSelected] = useState<string[]>([]),
    [detail, setDetail] = useState<string>(""),
    [launchProfile, setLaunchProfile] = useState("coding"),
    [rerun, setRerun] = useState<Obj | null>(null),
    [connection, setConnection] = useState(true),
    [setupBusy, setSetupBusy] = useState<"start" | "stop" | null>(null),
    [setupSmoke, setSetupSmoke] = useState(false),
    [refresh, setRefresh] = useState(0);
  const setupSubmission = useRef<string | null>(null);
  const load = useCallback(async () => {
    try {
      setRuns(await api("/runs"));
      setHealth(await api("/health"));
      setProfiles(await api("/profiles"));
    } catch (e) {
      setError(String(e));
    }
  }, []);
  const loadModels = useCallback(async () => {
    try {
      const d = await api("/models");
      setModels(d.models);
      setRuntime(d.runtime);
    } catch (e) {
      setModels([]);
      setRuntime({ error: String(e), ready: false });
    }
  }, []);
  useEffect(() => {
    load();
    loadModels();
    const es = new EventSource("/api/events");
    let pending: ReturnType<typeof setTimeout> | undefined;
    es.onopen = () => setConnection(true);
    es.onerror = () => setConnection(false);
    es.onmessage = () => {
      if (!pending)
        pending = setTimeout(() => {
          load();
          setRefresh((x) => x + 1);
          pending = undefined;
        }, 500);
    };
    const timer = setInterval(() => {
      loadModels();
      load();
    }, 15000);
    return () => {
      es.close();
      clearInterval(timer);
      clearTimeout(pending);
    };
  }, [load, loadModels]);
  const navigate = (v: string) => {
    setView(v);
    setError("");
    setNotice("");
    window.scrollTo({ top: 0, behavior: "instant" });
  };
  const openRun = (id: string) => {
    setDetail(id);
    navigate("detail");
  };
  const launch = (p = "coding", r: Obj | null = null) => {
    setLaunchProfile(p);
    setRerun(r);
    loadModels();
    navigate("launch");
  };
  const mutate = async (path: string, body: any = {}) => {
    try {
      await api(path, body);
      await load();
      return true;
    } catch (e) {
      setError(String(e));
      return false;
    }
  };
  const stop = async (r: Obj) => {
    if (confirm(`Stop ${runName(r)}? Partial results will be retained.`))
      await mutate(`/runs/${r.id}/cancel`);
  };
  const setupAction = async (
    action: "start" | "stop",
    runSmoke = setupSmoke,
  ) => {
    if (setupBusy) return;
    setSetupBusy(action);
    setError("");
    setNotice("");
    try {
      if (action === "start" && !setupSubmission.current)
        setupSubmission.current = Array.from(
          crypto.getRandomValues(new Uint8Array(16)),
          (b) => b.toString(16).padStart(2, "0"),
        ).join("");
      const response = await api(
        `/session-setup/${action}`,
        action === "start"
          ? { idempotency_key: setupSubmission.current, run_smoke: runSmoke }
          : {},
      );
      setupSubmission.current = null;
      const message =
        action === "stop"
          ? "Stop requested. Setup is cleaning up its work."
          : response.active
            ? "Setup request accepted. Waiting for the controller to begin preparation."
            : "This setup request has already finished. Refreshing its status.";
      setNotice(
        action === "stop"
          ? "Stop request accepted."
          : response.active
            ? "Setup request accepted."
            : "This setup request has already finished.",
      );
      setHealth((previous) => ({
        ...previous,
        session_setup: {
          ...previous.session_setup,
          phase: action === "stop" ? "stopping" : "requested",
          detail: message,
          can_start: false,
          can_stop: action === "start" && !!response.active,
        },
      }));
      await load();
    } catch (e) {
      setError(`Could not ${action} suite setup: ${String(e)}`);
    } finally {
      setSetupBusy(null);
    }
  };
  const current = runs.find((r) => r.id === detail);
  return (
    <div id="app">
      <header className="bs-header">
        <div className="bs-brand">
          <span className="bs-mark">
            <ChartNoAxesCombined size={20} />
          </span>
          Bench Studio
        </div>
        <nav className="bs-nav" aria-label="Main navigation">
          {[
            ["runs", "Runs"],
            ["profiles", "Profiles"],
            ["compare", "Compare"],
          ].map(([v, t]) => (
            <button
              key={v}
              aria-current={view === v ? "page" : undefined}
              onClick={() => navigate(v)}
            >
              {t}
            </button>
          ))}
        </nav>
        <div className="bs-host">
          <span
            className="bs-dot"
            style={{
              background: runtime.ready ? "var(--bs-good)" : "var(--bs-warn)",
            }}
          />
          {models.length} models ·{" "}
          {runtime.ready ? "runtime ready" : "runtime unavailable"}
        </div>
      </header>
      <main className="bs-main">
        {health.session_setup && (
          <div
            role="status"
            aria-label="Benchmark suite setup"
            className={`bs-alert bs-setup${health.session_setup.phase === "ready" ? " bs-setup-ready" : ""}`}
          >
            <div>
              <strong>
                {health.session_setup.phase === "ready"
                  ? "Benchmark suites available: "
                  : "Benchmark suite setup: "}
              </strong>
              {health.session_setup.detail}
            </div>
            {(health.session_setup.can_start ||
              health.session_setup.can_stop) && (
              <div className="bs-setup-suite">
                {health.session_setup.can_start && (
                  <>
                    {health.session_setup.phase !== "ready" && (
                      <label>
                        <input
                          type="checkbox"
                          checked={setupSmoke}
                          disabled={!!setupBusy}
                          onChange={(event) => {
                            setSetupSmoke(event.target.checked);
                            setupSubmission.current = null;
                          }}
                        />
                        Also run model smoke tests (optional; uses the model)
                      </label>
                    )}
                    <button
                      className="bs-button"
                      disabled={!!setupBusy}
                      onClick={() =>
                        setupAction(
                          "start",
                          health.session_setup.phase === "ready" || setupSmoke,
                        )
                      }
                    >
                      {setupBusy === "start"
                        ? "Starting setup…"
                        : health.session_setup.phase === "ready"
                          ? "Run optional model checks"
                          : "Start setup"}
                    </button>
                  </>
                )}
                {health.session_setup.can_stop && (
                  <button
                    className="bs-button"
                    disabled={!!setupBusy}
                    onClick={() => setupAction("stop")}
                  >
                    {setupBusy === "stop" ? "Stopping setup…" : "Stop setup"}
                  </button>
                )}
              </div>
            )}
            {Object.entries(health.session_setup.suites || {}).map(
              ([suite, value]) => {
                const state = value as Obj;
                const name =
                  profiles.find((p) => p.id === suite)?.name || suite;
                return (
                  <div key={suite} className="bs-setup-suite">
                    <div>
                      <strong>
                        {name}:{" "}
                        {state.available
                          ? "Available"
                          : label(state.phase.replaceAll("_", " "))}
                      </strong>
                      {state.available && state.phase !== "ready" && (
                        <span>
                          {" "}
                          · Model smoke test:{" "}
                          {label(state.phase.replaceAll("_", " "))}
                        </span>
                      )}
                      {state.detail && <span> · {state.detail}</span>}
                      {typeof state.turns === "number" && (
                        <span> · {state.turns} model turns</span>
                      )}
                      {state.generation?.active && (
                        <span>
                          {" "}
                          · Receiving response (
                          {state.generation.characters_received.toLocaleString()}{" "}
                          characters)
                        </span>
                      )}
                    </div>
                    {state.run_id && (
                      <button
                        className="bs-quiet"
                        onClick={() => openRun(state.run_id)}
                        aria-label={`View ${name} smoke run`}
                      >
                        View run
                      </button>
                    )}
                  </div>
                );
              },
            )}
          </div>
        )}
        {error && (
          <div role="alert" className="error">
            {error}
            <button
              className="bs-quiet"
              style={{ marginLeft: 15 }}
              onClick={() => setError("")}
            >
              Dismiss
            </button>
          </div>
        )}
        {!connection && (
          <div className="bs-alert">
            Live connection lost. Reconnecting; jobs continue on the server.
          </div>
        )}
        {notice && (
          <div role="status" className="bs-toast">
            {notice}
          </div>
        )}
        {view === "runs" && (
          <>
            <div className="bs-heading">
              <div>
                <div className="bs-eyebrow">Your local model lab</div>
                <h1>Every change, measured.</h1>
                <p className="bs-sub">
                  Run a profile. Keep a baseline. See what improved.
                </p>
              </div>
              <Button primary onClick={() => launch()}>
                <Plus size={16} />
                New benchmark
              </Button>
            </div>
            {runs
              .filter((r) => !terminal.has(r.status))
              .map((r) => (
                <section key={r.id} className="bs-surface bs-active">
                  <div>
                    <div className="bs-inline">
                      <Pill status={r.status} />
                      <span className="bs-small">{elapsed(r)}</span>
                    </div>
                    <h3 style={{ marginTop: 10 }}>
                      {runName(r)}{" "}
                      <span className="bs-small">
                        / {r.requested_targets.map(label).join(" + ")}
                      </span>
                    </h3>
                    <div className="bs-small" style={{ marginTop: 10 }}>
                      {r.progress}
                    </div>
                    {Object.entries(r.targets || {}).map(([t, i]: any) => (
                      <div className="bs-small" key={t}>
                        {label(t)}:{" "}
                        {i.progress?.category || i.phase || i.status}{" "}
                        {i.progress?.request
                          ? `· request ${i.progress.request}`
                          : ""}
                      </div>
                    ))}
                  </div>
                  <div className="bs-active-tools">
                    <Button onClick={() => openRun(r.id)}>
                      <Activity size={15} />
                      View run
                    </Button>
                    <Button onClick={() => stop(r)}>
                      <Square size={14} />
                      Stop
                    </Button>
                  </div>
                </section>
              ))}
            <div className="bs-section-head">
              <h2>Run history</h2>
              <Button
                disabled={selected.length !== 2}
                onClick={() => navigate("compare")}
              >
                <GitCompareArrows size={16} />
                Compare {selected.length} selected
              </Button>
            </div>
            <div className="bs-table-wrap">
              <table>
                <thead>
                  <tr>
                    <th />
                    <th>Benchmark</th>
                    <th>Model</th>
                    <th>Result</th>
                    <th>vs baseline</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {runs.map((r) => (
                    <tr key={r.id}>
                      <td className="bs-checkcell">
                        <input
                          type="checkbox"
                          aria-label={`Select ${r.id}`}
                          checked={selected.includes(r.id)}
                          onChange={(e) =>
                            setSelected(
                              e.target.checked
                                ? [...selected, r.id]
                                : selected.filter((x) => x !== r.id),
                            )
                          }
                        />
                      </td>
                      <td>
                        <button
                          className="bs-quiet"
                          onClick={() => openRun(r.id)}
                        >
                          {runName(r)}
                        </button>
                        <div className="bs-small">{date(r.created_at)}</div>
                        {r.note && <div className="bs-small">{r.note}</div>}
                      </td>
                      <td>
                        {r.requested_targets.map((t: string) => (
                          <div key={t}>
                            {label(t)}
                            <div className="bs-small">
                              {r.resolved?.[t]?.canonical || t}
                            </div>
                          </div>
                        ))}
                      </td>
                      <td>
                        {Object.entries(r.summary || {}).map(([t, s]: any) => (
                          <div key={t}>
                            <div className="bs-value">
                              {fmt(s.score)} <small>{s.unit}</small>
                            </div>
                            <div className="bs-small">
                              {r.requested_targets.length > 1
                                ? label(t) + " · "
                                : ""}
                              {s.passed !== undefined
                                ? `${s.passed} / ${s.count} passed`
                                : s.count
                                  ? `${s.count} samples`
                                  : ""}
                            </div>
                          </div>
                        ))}
                      </td>
                      <td>
                        {Object.entries(r.summary || {}).map(([t, s]: any) => (
                          <div
                            key={t}
                            className={s.delta > 0 ? "bs-positive" : "bs-small"}
                          >
                            {s.delta != null
                              ? `${s.delta >= 0 ? "+" : ""}${fmt(s.delta)} ${s.delta_unit}`
                              : s.baseline?.run_id === r.id
                                ? "Baseline"
                                : "—"}
                          </div>
                        ))}
                      </td>
                      <td>
                        <Pill status={r.status} />
                        {r.load_warning && (
                          <div className="bs-small">Shared activity</div>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {!runs.length && (
                <div className="bs-empty">
                  Your first baseline starts with a benchmark.
                </div>
              )}
            </div>
            <div className="bs-note">
              <Bookmark size={15} />
              Baselines are saved per profile and target. Each model retains its
              own score.
            </div>
          </>
        )}
        {view === "profiles" && (
          <>
            <div className="bs-heading">
              <div>
                <h1>A profile for every question.</h1>
                <p className="bs-sub">
                  Fixed workloads, visible settings, repeatable results.
                </p>
              </div>
              <Button primary onClick={() => launch()}>
                <Plus size={16} />
                New benchmark
              </Button>
            </div>
            <div className="bs-grid">
              {profiles.map((p) => (
                <button
                  className="bs-profile"
                  key={p.id}
                  onClick={() => launch(p.id)}
                >
                  <span className="bs-profile-icon">
                    <ProfileIcon family={p.family} />
                    <span className="bs-pill">{p.family}</span>
                  </span>
                  <h3>{p.name}</h3>
                  <p>{p.description}</p>
                  <span className="bs-bottom">
                    {p.engine} · v{p.version}
                    {!p.builtin ? " · custom" : ""}
                  </span>
                </button>
              ))}
            </div>
          </>
        )}
        {view === "launch" && (
          <Launcher
            profiles={profiles}
            models={models}
            initialProfile={launchProfile}
            rerun={rerun}
            back={() => navigate("runs")}
            submitted={async (r) => {
              await load();
              openRun(r.id);
            }}
            profileSaved={(p) => {
              setProfiles((xs) => [...xs, p]);
              setNotice("Custom profile saved.");
            }}
            onError={setError}
          />
        )}
        {view === "detail" && current && (
          <RunDetail
            run={current}
            back={() => navigate("runs")}
            rerun={() => launch(current.profile, current)}
            stop={() => stop(current)}
            baseline={async (t) => {
              if (await mutate(`/runs/${current.id}/baseline`, { target: t }))
                setNotice("Baseline saved for this profile and target.");
            }}
            refresh={refresh}
          />
        )}
        {view === "compare" && (
          <Comparison runs={runs} initial={selected} onError={setError} />
        )}
      </main>
      <footer className="bs-end">
        <span>Local inference · results stay on this machine</span>
        <span className="revision">
          Bench Studio 1.0 · {health.revision?.slice(0, 10) || "development"}
        </span>
      </footer>
    </div>
  );
}
function Launcher({
  profiles,
  models,
  initialProfile,
  rerun,
  back,
  submitted,
  profileSaved,
  onError,
}: {
  profiles: Obj[];
  models: Obj[];
  initialProfile: string;
  rerun: Obj | null;
  back: () => void;
  submitted: (r: Obj) => void;
  profileSaved: (p: Obj) => void;
  onError: (s: string) => void;
}) {
  const [pid, setPid] = useState(initialProfile),
    [targets, setTargets] = useState<string[]>(rerun?.requested_targets || []),
    [size, setSize] = useState(
      rerun?.profile_spec?.size ||
        (profiles
          .find((p) => p.id === initialProfile)
          ?.sizes.includes("standard")
          ? "standard"
          : profiles.find((p) => p.id === initialProfile)?.sizes[0] ||
            "standard"),
    ),
    [params, setParams] = useState<Obj>(rerun?.profile_spec?.parameters || {}),
    [note, setNote] = useState(""),
    [mode, setMode] = useState(rerun?.mode || "sequential"),
    [busy, setBusy] = useState(false),
    [customName, setCustomName] = useState("");
  const p = profiles.find((x) => x.id === pid);
  const qualification =
    !!rerun?.profile_spec?.qualification && pid === rerun?.profile;
  const [session, setSession] = useState<Obj>(
    sessionDefaults(
      rerun?.profile_spec || profiles.find((x) => x.id === initialProfile),
    ),
  );
  const values = { ...p?.parameters, ...params };
  useEffect(() => {
    if (!targets.length && models.some((m) => m.available))
      setTargets([models.find((m) => m.available)!.alias]);
  }, [models]);
  const choose = (id: string) => {
    setPid(id);
    setParams({});
    setSession(sessionDefaults(profiles.find((p) => p.id === id)));
    setMode("sequential");
    setSize(
      profiles.find((p) => p.id === id)?.sizes?.includes("standard")
        ? "standard"
        : profiles.find((p) => p.id === id)?.sizes?.[0] || "standard",
    );
  };
  const selected = models.filter((m) => targets.includes(m.alias));
  const unsupported =
    selected.some((m) => !m.available) || selected.length !== targets.length;
  const count =
    p?.family === "quality"
      ? size === "quick"
        ? 4
        : size === "standard"
          ? 40
          : "all eligible"
      : p?.family === "agent"
        ? size === "quick"
          ? 2
          : size === "standard"
            ? 5
            : 20
        : p?.spec?.phases?.[0] === "prefill"
          ? `${p.spec.config.prefill_depths.length} depths`
          : p?.spec?.categories?.length * p?.spec?.config?.runs_per_category;
  const submission = useRef({ payload: "", key: "" });
  const launch = async () => {
    const payload = JSON.stringify({
      targets,
      pid,
      size,
      params,
      mode,
      note,
      session,
    });
    if (submission.current.payload !== payload)
      submission.current = {
        payload,
        key: Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) =>
          b.toString(16).padStart(2, "0"),
        ).join(""),
      };
    setBusy(true);
    onError("");
    try {
      const r = await api("/runs", {
        targets,
        profile: pid,
        size,
        overrides: params,
        mode: targets.length > 1 ? mode : "sequential",
        note,
        idempotency_key: submission.current.key,
        ...(isSession(p) ? session : {}),
        ...(qualification ? { qualification: true } : {}),
      });
      submitted(r);
    } catch (e) {
      onError(String(e));
    } finally {
      setBusy(false);
    }
  };
  const save = async () => {
    try {
      const r = await api("/profiles", {
        profile: pid,
        size,
        overrides: params,
        name: customName,
        ...(isSession(p) ? session : {}),
      });
      profileSaved(r);
      setPid(r.id);
      setParams({});
      setCustomName("");
    } catch (e) {
      onError(String(e));
    }
  };
  return (
    <>
      <button className="bs-quiet bs-back" onClick={back}>
        <ArrowLeft size={16} />
        Back to runs
      </button>
      <div className="bs-heading">
        <div>
          <h1>New benchmark</h1>
          <p className="bs-sub">
            Choose what to test. Every profile has repeatable defaults.
          </p>
        </div>
      </div>
      {rerun && (
        <div className="bs-alert">
          <Info size={16} />
          Review the currently loaded models below. Your previous run used{" "}
          {Object.values(rerun.resolved || {})
            .map(
              (r: any) => r.canonical + " (" + fmt(r.context, 0) + " context)",
            )
            .join(" + ")}
          .
        </div>
      )}
      <h3>1. Choose a loaded model</h3>
      <div className="bs-models">
        {models.map((m) => (
          <button
            disabled={!m.available}
            className="bs-model"
            key={m.alias}
            aria-pressed={targets.includes(m.alias)}
            onClick={() =>
              setTargets(
                targets.includes(m.alias)
                  ? targets.filter((t) => t !== m.alias)
                  : [...targets, m.alias],
              )
            }
          >
            <span className="bs-spread">
              <span className="bs-inline">
                {m.alias === "nighttime" ? (
                  <Moon size={17} />
                ) : (
                  <Sun size={17} />
                )}
                <strong>{label(m.alias)}</strong>
              </span>
              {targets.includes(m.alias) && <CheckCircle2 size={17} />}
            </span>
            <span className="bs-model-name">{m.canonical}</span>
            <span className="bs-model-name">
              {fmt(m.context, 0)} context · {m.gpus?.join(" + ")}
            </span>
            <span className="bs-model-name">
              {m.available
                ? m.processing
                  ? "Busy · will queue"
                  : "Available"
                : m.error}
            </span>
          </button>
        ))}
      </div>
      {!models.length && (
        <div className="error">
          Model discovery is unavailable. Check the runtime before launching.
        </div>
      )}
      <h3>2. Choose a profile</h3>
      <div className="bs-grid" style={{ marginTop: 12 }}>
        {profiles
          .filter((p) =>
            [
              "coding-sessions",
              "vision-checks",
              "visual-design",
              "coding",
              "coding-checks",
              "repository-tasks",
            ].includes(p.id),
          )
          .map((q) => (
            <button
              className="bs-profile"
              key={q.id}
              aria-pressed={pid === q.id}
              onClick={() => choose(q.id)}
            >
              <span className="bs-profile-icon">
                <ProfileIcon family={q.family} />
                <span className="bs-pill">{q.family}</span>
              </span>
              <h3>{q.name}</h3>
              <p>{q.description}</p>
            </button>
          ))}
      </div>
      <div className="bs-form-grid">
        <label className="bs-field">
          Profile
          <select
            aria-label="Profile"
            value={pid}
            onChange={(e) => choose(e.target.value)}
          >
            {profiles.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </label>
        {!isSession(p) && (
          <label className="bs-field">
            Test size
            <select
              aria-label="Test size"
              value={size}
              onChange={(e) => setSize(e.target.value)}
            >
              {(p?.sizes || ["standard"]).map((s: string) => (
                <option key={s}>{s}</option>
              ))}
            </select>
            <small>
              {count} {p?.family === "speed" ? "measured samples" : "tasks"}
            </small>
          </label>
        )}
        <label className="bs-field">
          Reasoning
          <select
            aria-label="Reasoning"
            value={values.reasoning_effort || "default"}
            onChange={(e) =>
              setParams({ ...params, reasoning_effort: e.target.value })
            }
          >
            {["default", "off", "low", "medium", "xhigh"]
              .filter(
                (e) =>
                  e === "default" ||
                  selected.every((m) => m.reasoning?.efforts?.[e]),
              )
              .map((e) => (
                <option value={e} key={e}>
                  {e === "default" ? "Runtime default" : label(e)}
                </option>
              ))}
          </select>
        </label>
      </div>
      {isSession(p) && (
        <SessionOptions
          profile={p!}
          value={session}
          onChange={(value) => {
            if (value.difficulty !== session.difficulty)
              setParams((old) => ({
                ...old,
                ...p!.tier_budgets[value.difficulty],
              }));
            setSession(value);
          }}
        />
      )}
      <div className="bs-form-grid two">
        <label className="bs-field">
          Run note
          <input
            value={note}
            onChange={(e) => setNote(e.target.value)}
            maxLength={1000}
            placeholder="e.g. Raised context to 160K"
          />
        </label>
        {targets.length > 1 && (
          <label className="bs-field">
            Run both models
            <select
              aria-label="Run both models"
              value={mode}
              onChange={(e) => setMode(e.target.value)}
            >
              <option value="sequential">
                Sequential · isolated comparison
              </option>
              {!isSession(p) && (
                <option value="parallel">Simultaneous · shared load</option>
              )}
            </select>
          </label>
        )}
      </div>
      <details>
        <summary>Advanced parameters · profile defaults</summary>
        <div className="bs-form-grid">
          {Object.keys(p?.parameters || {})
            .filter((k) => k !== "reasoning_effort")
            .filter(
              (k) =>
                k !== "reasoning_budget_tokens" ||
                (targets.length > 0 &&
                  targets.every((t) =>
                    Object.values(
                      models.find((m) => m.alias === t)?.reasoning
                        ?.per_effort || {},
                    ).some((v: any) => v.reasoning_budget_tokens !== undefined),
                  )),
            )
            .map((k) => (
              <label className="bs-field" key={k}>
                {k === "max_tokens"
                  ? "Total output tokens (thinking + answer)"
                  : k === "reasoning_budget_tokens"
                    ? "Thinking token limit (optional)"
                    : label(k.replaceAll("_", " "))}
                <input
                  type="number"
                  value={values[k] ?? ""}
                  placeholder={
                    k === "max_tokens"
                      ? "Per-workload default"
                      : k === "reasoning_budget_tokens"
                        ? "Runtime default"
                        : ""
                  }
                  step={["temperature", "top_p"].includes(k) ? 0.05 : 1}
                  disabled={
                    k === "temperature" && p?.spec?.phases?.[0] === "prefill"
                  }
                  onChange={(e) =>
                    setParams({
                      ...params,
                      [k]:
                        e.target.value === "" ? null : Number(e.target.value),
                    })
                  }
                />
              </label>
            ))}
        </div>
        <div className="bs-note">
          <Info size={15} />
          Context capacity is read from AI Runtime. Task selection and
          generation settings are recorded with the run.
        </div>
        {p?.family !== "speed" && (
          <div className="bs-alert">
            The total output limit includes thinking. A thinking limit below it
            leaves room for an answer. Extra-high reasoning may exhaust the
            allowance or repeat; a larger total limit alone does not prevent
            loops. Blank thinking limit preserves the runtime default. Neither
            control changes your model or context configuration.
          </div>
        )}
        <div className="bs-form-grid two">
          <label className="bs-field">
            Save these settings as a profile
            <input
              value={customName}
              onChange={(e) => setCustomName(e.target.value)}
              placeholder="My coding baseline"
              maxLength={70}
            />
          </label>
          <div style={{ alignSelf: "end" }}>
            <Button disabled={!customName.trim()} onClick={save}>
              Save reusable profile
            </Button>
          </div>
        </div>
      </details>
      {(p?.family === "agent" || p?.family === "session") && (
        <div className="bs-alert">
          {p?.family === "session"
            ? "Sessions use isolated applications and a bounded active-time budget."
            : "Repository tasks use a fixed local subset and a bounded agent budget."}
          They can take up to {values.task_timeout / 60} minutes per task.
        </div>
      )}
      <div className="bs-launch-summary">
        <div>
          {targets.map(label).join(" + ") || "Select a model"} / {p?.name}
          <div className="bs-small">
            1 request per model · queues until idle ·{" "}
            {p?.family === "speed"
              ? "native throughput"
              : p?.family === "agent"
                ? "tasks resolved"
                : "tasks passed"}
          </div>
        </div>
        <Button
          primary
          disabled={
            busy ||
            !targets.length ||
            unsupported ||
            !p ||
            (isSession(p) &&
              p?.preparation?.ready === false &&
              !(qualification && p?.preparation?.prepared)) ||
            (p?.requires_vision &&
              selected.some(
                (m) =>
                  !(m.vision || m.resolved?.metadata?.capabilities?.vision),
              ))
          }
          onClick={launch}
        >
          {busy ? <LoaderCircle size={16} /> : <Play size={16} />}Queue
          benchmark
        </Button>
      </div>
    </>
  );
}
function RunDetail({
  run: r,
  back,
  rerun,
  stop,
  baseline,
  refresh,
}: {
  run: Obj;
  back: () => void;
  rerun: () => void;
  stop: () => void;
  baseline: (t: string) => void;
  refresh: number;
}) {
  const [target, setTarget] = useState(r.requested_targets[0]),
    [tab, setTab] = useState("outcomes"),
    [logs, setLogs] = useState("");
  useEffect(() => {
    if (tab === "logs")
      api(`/runs/${r.id}/logs`)
        .then((d) => setLogs(d.text))
        .catch((e) => setLogs(String(e)));
  }, [tab, r.id, refresh]);
  const t = r.requested_targets.includes(target)
      ? target
      : r.requested_targets[0],
    s = r.summary?.[t] || {},
    resolved = r.resolved?.[t] || {},
    params = r.profile_spec?.parameters || {};
  const prefill = s.metric === "prefill_tps",
    speed = r.family === "speed" || r.legacy;
  const chart = (s.metrics || []).map((m: Obj) => ({
    name: m.category || m.language || String(m.target_depth),
    value: m.decode_med ?? m.pp_med ?? m.rate,
    actual: m.prompt_tokens_med,
  }));
  return (
    <>
      <button className="bs-quiet bs-back" onClick={back}>
        <ArrowLeft size={16} />
        Back to runs
      </button>
      <div className="bs-heading">
        <div className="bs-inline">
          <h1>{runName(r)}</h1>
          <Pill status={r.status} />
        </div>
        <div className="bs-inline">
          {!terminal.has(r.status) && (
            <Button onClick={stop}>
              <Square size={15} />
              Stop
            </Button>
          )}
          <Button
            disabled={r.status !== "completed" || s.score == null}
            onClick={() => baseline(t)}
          >
            <Bookmark size={15} />
            Set baseline
          </Button>
          <Button primary onClick={rerun}>
            <RotateCcw size={15} />
            Run again
          </Button>
        </div>
      </div>
      <p className="bs-sub">
        {r.id} · {date(r.created_at)} · {r.mode}
      </p>
      {r.note && <p className="bs-sub">{r.note}</p>}
      {r.error && <div className="error">{r.error}</div>}
      {r.load_warning && <div className="bs-alert">{r.load_warning}</div>}
      {r.health_warning && <div className="bs-alert">{r.health_warning}</div>}
      {s.validation_warning && (
        <div className="bs-alert">{s.validation_warning}</div>
      )}
      {s.partial && (
        <div className="bs-alert">
          Partial evidence: {s.completed_count ?? 0} of {s.count} tasks
          completed. No final score is published.
        </div>
      )}
      {s.infrastructure_error && s.infrastructure_error !== r.error && (
        <div className="error">{s.infrastructure_error}</div>
      )}
      {!!(s.tasks || []).filter((task: Obj) => task.finish_reason === "length")
        .length && (
        <div className="bs-alert">
          {
            (s.tasks || []).filter(
              (task: Obj) => task.finish_reason === "length",
            ).length
          }{" "}
          answers exhausted the total output budget.
          {s.repetition_count
            ? ` ${s.repetition_count} responses showed repetitive reasoning.`
            : ""}{" "}
          These are completion failures, not evidence that a finished solution
          was incorrect.
        </div>
      )}
      {!terminal.has(r.status) && (
        <section
          className="bs-surface"
          style={{ marginTop: 18 }}
          aria-live="polite"
        >
          <Pill status={r.status} />
          <p className="bs-sub">{r.progress}</p>
          <div className="bs-small">
            Elapsed {elapsed(r)} · requests continue when this page closes
          </div>
        </section>
      )}
      <div className="bs-filter" aria-label="Model results">
        {r.requested_targets.map((x: string) => (
          <button key={x} aria-pressed={t === x} onClick={() => setTarget(x)}>
            {label(x)}
          </button>
        ))}
      </div>
      {isSession(r.profile_spec) ? (
        <SessionDetail run={r} summary={s} target={t} />
      ) : (
        <div className="bs-stats">
          <section className="bs-surface">
            <span className="bs-small">
              {prefill
                ? "Prompt throughput"
                : speed
                  ? "Weighted throughput"
                  : r.family === "agent"
                    ? "Tasks resolved"
                    : s.score_label || "Coding pass rate"}
            </span>
            <div className="bs-big">
              {fmt(s.score)}
              <small> {s.unit}</small>
            </div>
            <span className="bs-small">
              {s.score_label || "Completed runs only"}
              {s.delta != null
                ? ` · ${s.delta >= 0 ? "+" : ""}${fmt(s.delta)} ${s.delta_unit} vs baseline`
                : ""}
            </span>
          </section>
          <section className="bs-surface">
            <span className="bs-small">
              {speed ? "Measured samples" : "Tasks passed"}
            </span>
            <div className="bs-big">
              {speed ? fmt(s.count, 0) : fmt(s.passed, 0)}
              {!speed && <small> / {s.count ?? "—"}</small>}
            </div>
            <span className="bs-small">
              {speed
                ? "Raw measurements retained"
                : isSession(r.profile_spec)
                  ? `${r.profile_spec.repetitions} repetitions per task`
                  : "One attempt per task"}
            </span>
          </section>
          <section className="bs-surface">
            <span className="bs-small">Duration</span>
            <div className="bs-big" style={{ fontSize: 28 }}>
              {elapsed(r)}
            </div>
            <span className="bs-small">
              {label(t)} ·{" "}
              {r.profile_spec?.difficulty || r.profile_spec?.size || "standard"}
            </span>
          </section>
        </div>
      )}
      <div className="bs-detail-grid">
        <section className="bs-surface">
          <h3>
            {prefill
              ? "Context-depth curve"
              : speed
                ? "By workload"
                : isSession(r.profile_spec)
                  ? "By task"
                  : "By language"}
          </h3>
          {chart.length ? (
            <div className="chart">
              <ResponsiveContainer width="100%" height="100%">
                {prefill ? (
                  <LineChart
                    data={chart}
                    margin={{ top: 5, right: 30, left: 0, bottom: 5 }}
                  >
                    <CartesianGrid
                      stroke="var(--bs-line)"
                      strokeDasharray="3 3"
                    />
                    <XAxis
                      dataKey="actual"
                      tickFormatter={(value) => fmt(Number(value), 0)}
                      type="number"
                      domain={["dataMin", "dataMax"]}
                      name="Actual input tokens"
                      tick={{ fontSize: 11 }}
                    />
                    <YAxis width={65} tick={{ fontSize: 11 }} />
                    <Tooltip
                      contentStyle={{
                        background: "var(--bs-surface)",
                        borderColor: "var(--bs-line)",
                      }}
                    />
                    <Line
                      isAnimationActive={false}
                      dataKey="value"
                      name="Prompt tok/s"
                      stroke="var(--bs-accent)"
                      strokeWidth={2}
                    />
                  </LineChart>
                ) : (
                  <BarChart data={chart} layout="vertical">
                    <XAxis type="number" tick={{ fontSize: 11 }} />
                    <YAxis
                      type="category"
                      dataKey="name"
                      width={100}
                      tick={{ fontSize: 11 }}
                    />
                    <Tooltip
                      contentStyle={{
                        background: "var(--bs-surface)",
                        borderColor: "var(--bs-line)",
                      }}
                    />
                    <Bar
                      dataKey="value"
                      name={s.unit || "Rate"}
                      fill="var(--bs-accent)"
                      radius={[0, 4, 4, 0]}
                    />
                  </BarChart>
                )}
              </ResponsiveContainer>
            </div>
          ) : (
            <div className="bs-empty">
              Measurements appear as phases finish.
            </div>
          )}
          {prefill && (
            <div className="bs-small">
              X: actual input tokens · Y: prompt tokens per second
            </div>
          )}
        </section>
        <section className="bs-surface">
          <h3>Configuration snapshot</h3>
          <dl className="bs-kv">
            <dt>Model</dt>
            <dd>{resolved.canonical}</dd>
            <dt>Context window</dt>
            <dd>{fmt(resolved.context, 0)} tokens</dd>
            <dt>GPU pair</dt>
            <dd>{resolved.service?.gpu_names?.join(" + ") || "Unavailable"}</dd>
            <dt>Reasoning</dt>
            <dd>{params.reasoning_effort || "runtime default"}</dd>
            <dt>Temperature / seed</dt>
            <dd>
              {params.temperature ?? "recorded in raw report"} /{" "}
              {params.seed ?? "—"}
            </dd>
            <dt>Profile version</dt>
            <dd>{r.profile_spec?.version || "Legacy"}</dd>
            <dt>Engine</dt>
            <dd>{r.profile_spec?.engine || "BetterBench 0.6.0"}</dd>
            <dt>Runtime revision</dt>
            <dd>{resolved.runtime_revision?.slice(0, 12) || "—"}</dd>
          </dl>
        </section>
      </div>
      <div className="bs-tabs">
        {["outcomes", "logs", "artifacts", "configuration"].map((x) => (
          <button key={x} aria-pressed={tab === x} onClick={() => setTab(x)}>
            {x === "outcomes"
              ? speed
                ? "Requests"
                : "Tasks"
              : x === "artifacts"
                ? "Reports & exports"
                : label(x)}
          </button>
        ))}
      </div>
      {tab === "logs" && (
        <pre className="bs-log">{logs || "Waiting for logs."}</pre>
      )}
      {tab === "configuration" && (
        <pre className="bs-log">
          {JSON.stringify(
            {
              profile: r.profile_spec,
              generation: r.generation,
              resolved: r.resolved,
              host: r.host,
            },
            null,
            2,
          )}
        </pre>
      )}
      {tab === "artifacts" && (
        <section className="bs-surface" style={{ marginTop: 18 }}>
          <div className="bs-spread">
            <h3>Everything behind this result</h3>
            <a className="bs-button" href={`/api/runs/${r.id}/export`}>
              <Download size={16} />
              Export bundle
            </a>
          </div>
          <div style={{ marginTop: 15 }}>
            {(r.artifacts || []).map((p: string) => (
              <div key={p} className="bs-artifact">
                <a
                  target="_blank"
                  rel="noreferrer"
                  href={`/api/runs/${r.id}/artifacts/${p.split("/").map(encodeURIComponent).join("/")}`}
                >
                  {p}
                </a>
              </div>
            ))}
          </div>
        </section>
      )}
      {tab === "outcomes" && !isSession(r.profile_spec) && (
        <div className="bs-table-wrap" style={{ marginTop: 18 }}>
          <table>
            <thead>
              <tr>
                <th>Task / request</th>
                <th>Outcome</th>
                <th>Details</th>
              </tr>
            </thead>
            <tbody>
              {(s.tasks || []).map((task: Obj, i: number) => (
                <tr key={task.id || i}>
                  <td>
                    {task.id}
                    <div className="bs-small">{task.language}</div>
                  </td>
                  <td>
                    <span
                      className={
                        "bs-pill " +
                        (task.status === "passed" ? "good" : "warn")
                      }
                    >
                      {task.status}
                    </span>
                  </td>
                  <td className="bs-small">
                    {task.prefill_tps != null
                      ? `${fmt(task.prefill_tps)} prompt tok/s · ${fmt(task.prompt_tokens, 0)} input tokens · ${fmt(task.ttft_ms)} ms TTFT`
                      : task.decode_tps != null
                        ? `${fmt(task.decode_tps)} tok/s · ${fmt(task.ttft_ms)} ms TTFT`
                        : task.error ||
                          task.detail ||
                          task.finish_reason ||
                          "Tests completed"}
                    {task.usage && (
                      <div>
                        {fmt(task.usage.completion_tokens, 0)} output tokens ·{" "}
                        {task.finish_reason}
                      </div>
                    )}
                    {task.diagnostics && (
                      <div>
                        {fmt(task.diagnostics.answer_chars, 0)} answer
                        characters · {fmt(task.diagnostics.reasoning_chars, 0)}{" "}
                        thinking characters
                      </div>
                    )}
                    {task.trial_path && (
                      <a
                        target="_blank"
                        rel="noreferrer"
                        href={`/api/runs/${r.id}/artifacts/${t}/${task.trial_path}`}
                      >
                        Trial evidence
                      </a>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {!s.tasks?.length && (
            <div className="bs-empty">
              No task results yet. Open Logs to follow execution.
            </div>
          )}
        </div>
      )}
    </>
  );
}
function Comparison({
  runs,
  initial,
  onError,
}: {
  runs: Obj[];
  initial: string[];
  onError: (s: string) => void;
}) {
  const eligible = runs.filter((r) => r.status === "completed");
  const [a, setA] = useState(initial[0] || eligible[0]?.id || ""),
    [b, setB] = useState(initial[1] || eligible[1]?.id || ""),
    [ta, setTa] = useState(""),
    [tb, setTb] = useState(""),
    [data, setData] = useState<Obj | null>(null);
  const ar = runs.find((r) => r.id === a),
    br = runs.find((r) => r.id === b);
  const at = ar?.requested_targets.includes(ta) ? ta : ar?.requested_targets[0],
    bt = br?.requested_targets.includes(tb) ? tb : br?.requested_targets[0];
  useEffect(() => {
    setData(null);
    if (a && b && at && bt)
      api(
        `/compare?a=${a}&b=${b}&ta=${encodeURIComponent(at)}&tb=${encodeURIComponent(bt)}`,
      )
        .then((d) => {
          setData(d);
          onError("");
        })
        .catch((e) => onError(String(e)));
  }, [a, b, at, bt]);
  const pick = (
    which: "a" | "b",
    rid: string,
    t: string,
    setR: (s: string) => void,
    setT: (s: string) => void,
  ) => {
    const r = runs.find((r) => r.id === rid);
    return (
      <div className="bs-surface">
        <label className="bs-field">
          {which === "a" ? "Baseline run" : "Compare run"}
          <select value={rid} onChange={(e) => setR(e.target.value)}>
            <option value="">Select a run</option>
            {eligible.map((x) => (
              <option key={x.id} value={x.id}>
                {runName(x)} · {date(x.created_at)}
              </option>
            ))}
          </select>
        </label>
        <label className="bs-field" style={{ marginTop: 12 }}>
          Model
          <select value={t || ""} onChange={(e) => setT(e.target.value)}>
            {r?.requested_targets.map((x: string) => (
              <option key={x} value={x}>
                {label(x)}
              </option>
            ))}
          </select>
        </label>
      </div>
    );
  };
  const chart = data
    ? (data.a.metrics || []).map((m: Obj, i: number) => ({
        name: m.category || m.language || String(m.target_depth),
        baseline: m.decode_med ?? m.pp_med ?? m.rate,
        selected:
          (data.b.metrics || []).find(
            (r: Obj) =>
              (r.category || r.language || String(r.target_depth)) ===
              (m.category || m.language || String(m.target_depth)),
          )?.decode_med ??
          (data.b.metrics || []).find(
            (r: Obj) =>
              (r.category || r.language || String(r.target_depth)) ===
              (m.category || m.language || String(m.target_depth)),
          )?.pp_med ??
          (data.b.metrics || []).find(
            (r: Obj) =>
              (r.category || r.language || String(r.target_depth)) ===
              (m.category || m.language || String(m.target_depth)),
          )?.rate,
      }))
    : [];
  return (
    <>
      <div className="bs-heading">
        <div>
          <h1>Did the change help?</h1>
          <p className="bs-sub">
            Compare the same workload and see exactly what changed.
          </p>
        </div>
      </div>
      <div className="bs-compare-grid">
        {pick("a", a, at, setA, setTa)}
        {pick("b", b, bt, setB, setTb)}
      </div>
      {data && (
        <>
          {data.paired && <SessionComparison paired={data.paired} />}
          <div className="bs-compare-grid">
            {[data.a, data.b].map((s: Obj, i: number) => (
              <div
                className={"bs-compare-card " + (i ? "current" : "")}
                key={i}
              >
                <span className="bs-pill">
                  {i ? "Selected run" : "Baseline run"}
                </span>
                <h3 style={{ marginTop: 12 }}>{s.canonical}</h3>
                <div className="bs-big">
                  {fmt(s.score)} <small>{s.unit}</small>
                </div>
                <span className="bs-small">{s.count} samples / tasks</span>
              </div>
            ))}
          </div>
          <section className="bs-surface">
            <h3>What moved</h3>
            <div className="chart">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={chart}>
                  <CartesianGrid stroke="var(--bs-line)" vertical={false} />
                  <XAxis dataKey="name" tick={{ fontSize: 11 }} />
                  <YAxis tick={{ fontSize: 11 }} />
                  <Tooltip
                    contentStyle={{
                      background: "var(--bs-surface)",
                      borderColor: "var(--bs-line)",
                    }}
                  />
                  <Legend />
                  <Bar dataKey="baseline" fill="var(--bs-muted)" />
                  <Bar dataKey="selected" fill="var(--bs-accent)" />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </section>
          <section className="bs-surface" style={{ marginTop: 18 }}>
            <h3>Configuration differences</h3>
            {data.differences.length ? (
              <div className="bs-table-wrap" style={{ marginTop: 16 }}>
                <table>
                  <thead>
                    <tr>
                      <th>Setting</th>
                      <th>Baseline</th>
                      <th>Selected</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.differences.map((d: Obj) => (
                      <tr key={d.field}>
                        <td>{d.field}</td>
                        <td>
                          {typeof d.a === "object"
                            ? JSON.stringify(d.a)
                            : String(d.a ?? "—")}
                        </td>
                        <td>
                          {typeof d.b === "object"
                            ? JSON.stringify(d.b)
                            : String(d.b ?? "—")}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="bs-note">
                <Check size={15} />
                Recorded model, context, sampling, runtime and hardware match.
              </div>
            )}
          </section>
          <div className="bs-alert">
            <Info size={16} />
            {data.warning}
          </div>
        </>
      )}
    </>
  );
}
createRoot(document.getElementById("root")!).render(<App />);
