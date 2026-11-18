import { Activity, ArrowUpRight, Check, ChevronRight, Circle, MessageSquare, PenLine } from "lucide-react";
import { type ReactNode, useEffect, useState } from "react";
import type {
  EvidenceFact,
  EvidenceSource,
  PhoenixStatus,
  ScenarioSpec,
  ScenarioState,
  ScenarioStatus,
  ScenarioSummary,
} from "../shared/types.ts";
import { AgentChat } from "./AgentChat.tsx";
import { api, usePoll } from "./client.ts";

const STATE_LABEL: Record<ScenarioState, string> = {
  absent: "Not deployed",
  settling: "Settling",
  reproduced: "Reproduced",
  changed: "Changed",
};

// ---------------------------------------------------------------- routing

// Hash routes keep every view linkable:
//   #/ or #/agent?q=...    Ask the agent (the user-facing product), optionally prefilled
//   #/scenarios            every scenario (reviewer)
//   #/scenarios/<id>       one scenario and its answer key (reviewer)
type Route = { page: "agent"; question: string } | { page: "scenarios" } | { page: "scenario"; id: string };

function parseHash(hash: string): Route {
  const [path = "", query = ""] = hash.replace(/^#/, "").split("?");
  const scenario = /^\/scenarios\/([\w-]+)$/.exec(path);
  if (scenario?.[1]) return { page: "scenario", id: scenario[1] };
  if (path === "/scenarios") return { page: "scenarios" };
  return { page: "agent", question: new URLSearchParams(query).get("q") ?? "" };
}

const scenarioHref = (id: string) => `#/scenarios/${id}`;
const agentHref = (question?: string) => (question ? `#/agent?q=${encodeURIComponent(question)}` : "#/agent");

function useRoute(): Route {
  const [route, setRoute] = useState(() => parseHash(window.location.hash));
  useEffect(() => {
    const onChange = () => setRoute(parseHash(window.location.hash));
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return route;
}

// ---------------------------------------------------------------- shell

export function App() {
  const route = useRoute();
  const health = usePoll(api.health, 10_000, "health");
  const phoenix = usePoll(api.phoenix, 10_000, "phoenix");
  const list = usePoll(api.scenarios, 5_000, "list");
  const [scenariosOpen, setScenariosOpen] = useState(true);

  const scenarios = list.data ?? [];
  const current = route.page === "scenario" ? scenarios.find((s) => s.id === route.id) : undefined;

  return (
    <div className="shell">
      <header className="topbar">
        <a className="product" href="#/">
          agentic-diagnoster
        </a>
        <div className="statuses">
          <StatusDot ok={health.data?.reachable} pending="Checking cluster…" up="Cluster connected" down="Cluster unreachable" />
          <StatusDot ok={phoenix.data?.reachable} pending="Checking Phoenix…" up="Phoenix running" down="Phoenix not running" />
        </div>
      </header>

      {health.data && !health.data.reachable && (
        <div className="banner" role="alert">
          {health.data.message ?? "Can't reach the cluster"}
        </div>
      )}

      <div className="layout">
        <nav className="sidebar" aria-label="Main">
          <a className="nav-row nav-page" href={agentHref()} aria-current={route.page === "agent" ? "page" : undefined}>
            <MessageSquare className="nav-icon" size={16} strokeWidth={1.5} aria-hidden="true" />
            Ask the agent
          </a>

          {/* The label opens the scenarios page; the arrow only folds the list. */}
          <div
            className="nav-row nav-group"
            aria-current={route.page === "scenarios" ? "page" : undefined}
          >
            <button
              className="nav-toggle"
              aria-expanded={scenariosOpen}
              aria-controls="scenario-list"
              aria-label={scenariosOpen ? "Hide scenarios" : "Show scenarios"}
              onClick={() => setScenariosOpen((o) => !o)}
            >
              <ChevronRight className="nav-icon disclosure" size={16} strokeWidth={1.5} aria-hidden="true" />
            </button>
            <a className="nav-group-link" href="#/scenarios">
              Scenarios
            </a>
          </div>
          {scenariosOpen && (
            <ul className="scenario-list" id="scenario-list">
              {scenarios.map((s) => (
                <li key={s.id}>
                  <a
                    className="nav-row scenario-item"
                    href={scenarioHref(s.id)}
                    aria-current={s.id === current?.id ? "page" : undefined}
                    aria-label={`${s.title}, namespace ${s.namespace}: ${STATE_LABEL[s.state]}`}
                    title={STATE_LABEL[s.state]}
                  >
                    <StateMark state={s.state} />
                    <span className="scenario-title">{s.title}</span>
                    <span className="scenario-ns">{s.namespace}</span>
                  </a>
                </li>
              ))}
              {list.error && !list.data && <li className="muted nav-note">{list.error}</li>}
            </ul>
          )}

          {phoenix.data?.url && (
            <a className="nav-row nav-page nav-external" href={phoenix.data.url} target="_blank" rel="noreferrer">
              <Activity className="nav-icon" size={16} strokeWidth={1.5} aria-hidden="true" />
              Traces and evals
              <ArrowUpRight className="nav-icon nav-trailing" size={16} strokeWidth={1.5} aria-label="opens Phoenix" />
            </a>
          )}
        </nav>

        <main className="main">
          {route.page === "agent" ? (
            <AgentChat key={route.question} question={route.question} examples={scenarios.map((s) => s.question)} />
          ) : route.page === "scenarios" ? (
            list.data ? (
              <OverviewView scenarios={scenarios} phoenix={phoenix.data} onChanged={list.refresh} />
            ) : (
              <p className="muted">{list.error ?? "Loading scenarios…"}</p>
            )
          ) : current ? (
            <ScenarioView key={current.id} summary={current} onChanged={list.refresh} />
          ) : (
            <p className="muted">{list.data ? `No scenario called ${route.id}` : (list.error ?? "Loading scenarios…")}</p>
          )}
        </main>
      </div>
    </div>
  );
}

function StatusDot({ ok, pending, up, down }: { ok?: boolean; pending: string; up: string; down: string }) {
  if (ok === undefined) return <span className="status">{pending}</span>;
  return (
    <span className={`status ${ok ? "is-up" : "is-down"}`}>
      <span className="status-dot" aria-hidden="true" />
      {ok ? up : down}
    </span>
  );
}

function StateMark({ state }: { state: ScenarioState }) {
  return <span className={`state-mark state-${state}`} aria-hidden="true" />;
}

function StateTag({ state }: { state: ScenarioState }) {
  return <span className={`fault-tag tag-${state}`}>{STATE_LABEL[state]}</span>;
}

// ---------------------------------------------------------------- deploy / reset / remove

type Action = "deploy" | "reset" | "remove";
const ACTION_LABEL: Record<Action, [idle: string, busy: string]> = {
  deploy: ["Deploy", "Deploying…"],
  reset: ["Reset to broken state", "Resetting…"],
  remove: ["Remove", "Removing…"],
};

function ScenarioActions({
  id,
  state,
  compact = false,
  onDone,
}: {
  id: string;
  state: ScenarioState;
  compact?: boolean; // short labels, for table rows
  onDone: (status: ScenarioStatus) => void;
}) {
  const [busy, setBusy] = useState<Action>();
  const [error, setError] = useState<string>();

  const run = async (action: Action) => {
    setBusy(action);
    setError(undefined);
    try {
      onDone(await api[action](id));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(undefined);
    }
  };

  const button = (action: Action, kind: "primary" | "quiet") => (
    <button className={`btn btn-${kind}`} disabled={!!busy} onClick={() => run(action)}>
      {busy === action ? ACTION_LABEL[action][1] : compact && action === "reset" ? "Reset" : ACTION_LABEL[action][0]}
    </button>
  );

  return (
    <div className="actions">
      {error && (
        <span className="action-error" role="alert">
          {error}
        </span>
      )}
      {state === "absent" ? (
        button("deploy", "primary")
      ) : (
        <>
          {button("remove", "quiet")}
          {button("reset", "quiet")}
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- overview

// Every scenario on one screen: what is broken, and whether it is ready to investigate.
function OverviewView({
  scenarios,
  phoenix,
  onChanged,
}: {
  scenarios: ScenarioSummary[];
  phoenix?: PhoenixStatus;
  onChanged: () => void;
}) {
  return (
    <div className="overview">
      <h1 className="page-title">Scenarios</h1>
      <p className="page-note">
        Deploy a scenario, then ask the agent about it and compare its answer with the scenario's answer key.
      </p>
      <table className="grid-table overview-table">
        <thead>
          <tr>
            <th scope="col">Scenario</th>
            <th scope="col">Injected fault</th>
            <th scope="col">Category</th>
            <th scope="col">State</th>
            <th scope="col">
              <span className="sr-only">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {scenarios.map((s) => (
            <tr key={s.id}>
              <td>
                <a className="row-link" href={scenarioHref(s.id)}>
                  {s.title}
                </a>
                <code className="cell-sub">{s.namespace}</code>
              </td>
              <td className="cell-fault">{s.fault}</td>
              <td>
                <code>{s.category}</code>
              </td>
              <td>
                <StateTag state={s.state} />
              </td>
              <td className="cell-actions">
                <ScenarioActions id={s.id} state={s.state} compact onDone={onChanged} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <EvalsSection phoenix={phoenix} />
    </div>
  );
}

// Evals run inside Phoenix: its Playground sends every dataset question to the
// agent and scores each answer with the three evaluators.
function EvalsSection({ phoenix }: { phoenix?: PhoenixStatus }) {
  return (
    <section className="evals" aria-labelledby="evals-title">
      <h2 className="section-title" id="evals-title">
        Evals
      </h2>
      {phoenix?.evalsUrl ? (
        <>
          <p className="page-note">
            Opens Phoenix's Playground with the agent's prompt and the eval dataset. Press Run there. The model is{" "}
            <code>diagnoster-recorded</code>, which answers from a saved copy of the cluster; switch to{" "}
            <code>diagnoster-live</code> to use the real one, with all four scenarios deployed.
          </p>
          <a className="btn btn-quiet evals-link" href={phoenix.evalsUrl} target="_blank" rel="noreferrer">
            Run evals in Phoenix
            <ArrowUpRight size={16} strokeWidth={1.5} aria-label="opens Phoenix" />
          </a>
        </>
      ) : phoenix?.reachable ? (
        <p className="page-note">
          Phoenix has no evals yet. Set them up with <code>docker compose up phoenix-setup</code>.
        </p>
      ) : (
        <p className="page-note">Start Phoenix to run evals.</p>
      )}
    </section>
  );
}

// ---------------------------------------------------------------- scenario

// The reviewer's view of one scenario: what is broken, whether it is ready,
// and what a correct answer looks like. Pods and events are left out on
// purpose: they are what the agent should find for itself.
function ScenarioView({ summary, onChanged }: { summary: ScenarioSummary; onChanged: () => void }) {
  const status = usePoll(() => api.status(summary.id), 3_000, summary.id);
  const spec = usePoll(() => api.spec(summary.id), 600_000, summary.id);
  const state = status.data?.state ?? summary.state;

  return (
    <article className="scenario">
      <header className="scenario-bar">
        <div className="scenario-id">
          <h1 className="scenario-name">{summary.title}</h1>
          <code className="ns">{summary.namespace}</code>
          <StateTag state={state} />
        </div>
        <ScenarioActions
          id={summary.id}
          state={state}
          onDone={(s) => {
            status.setData(s);
            onChanged();
          }}
        />
      </header>

      <p className="fault">
        <span className="fault-label">Injected fault</span>
        {summary.fault}
      </p>
      <Readiness status={status.data} error={status.error} />

      {state === "reproduced" && (
        <div className="actions ask-row">
          <a className="btn btn-primary" href={agentHref(summary.question)}>
            Ask the agent about it
          </a>
        </div>
      )}

      <AnswerKey spec={spec.data} error={spec.error} />
    </article>
  );
}

// Is the scenario broken the way it was designed? One line when it is; the
// checks still waiting when it is not.
function Readiness({ status, error }: { status?: ScenarioStatus; error?: string }) {
  if (!status) return <p className="readiness muted">{error ?? "Checking the namespace…"}</p>;
  if (status.state === "absent") return <p className="readiness muted">Not deployed. Deploy it to investigate.</p>;
  if (status.state === "changed") {
    return (
      <div className="readiness">
        <p className="muted">Changed since deploy, so it is no longer the designed scenario. Reset to restore it.</p>
        <ul className="checks changes">
          {status.changes.map((c) => (
            <li key={`${c.resource}${c.field}`}>
              <PenLine className="check-icon" size={16} strokeWidth={1.5} aria-hidden="true" />
              <span>
                <code>{c.resource}</code>, changed by <code>{c.by}</code>
                <code className="changed-field">{c.field}</code>
              </span>
            </li>
          ))}
        </ul>
      </div>
    );
  }

  const waiting = status.checks.filter((c) => !c.pass);
  if (waiting.length === 0) {
    return (
      <p className="readiness is-ready">
        <Check className="check-icon" size={16} strokeWidth={1.5} aria-hidden="true" />
        Ready to investigate: all {status.checks.length} designed-state checks pass
      </p>
    );
  }
  return (
    <div className="readiness">
      <p className="muted">
        Waiting for the designed state: {status.checks.length - waiting.length} of {status.checks.length} checks pass
      </p>
      <ul className="checks">
        {waiting.map((c) => (
          <li key={c.label} className="wait">
            <Circle className="check-icon" size={16} strokeWidth={1.5} aria-hidden="true" />
            {c.label}
          </li>
        ))}
      </ul>
    </div>
  );
}

function Remediation({ remediation }: { remediation: NonNullable<ScenarioSpec["remediation"]> }) {
  return (
    <>
      <h3 className="key-title">The one fix the agent may apply, after approval</h3>
      <pre className="diff">
        <span className="diff-meta">
          {remediation.target} {remediation.change.field}
        </span>
        {"\n"}
        <span className="diff-del">- {remediation.change.from}</span>
        {"\n"}
        <span className="diff-add">+ {remediation.change.to}</span>
      </pre>
    </>
  );
}

// ---------------------------------------------------------------- answer key

const source = (s: EvidenceSource) => `${s.kind} ${s.name}, ${s.aspect.replace("_", " ")}`;

function Disclosure({ title, children }: { title: string; children: ReactNode }) {
  return (
    <details className="disclosure-block">
      <summary>
        <ChevronRight className="nav-icon disclosure" size={16} strokeWidth={1.5} aria-hidden="true" />
        {title}
      </summary>
      {children}
    </details>
  );
}

// The white box: what a correct answer contains. The essentials are always
// visible; how each part is graded sits behind a toggle. The agent never sees
// any of this; it only gets the question and the list of categories.
function AnswerKey({ spec, error }: { spec?: ScenarioSpec; error?: string }) {
  return (
    <aside className="answer-key" aria-label="Answer key">
      <h2 className="section-title">Answer key</h2>
      {error && !spec && <p className="muted">{error}</p>}
      {spec && (
        <>
          <dl className="key-grid">
            <dt>Category</dt>
            <dd>
              <code title={spec.categoryDescription}>{spec.category}</code>
            </dd>
            <dt>Resource</dt>
            <dd>
              {spec.resources.length === 0
                ? "None"
                : spec.resources.map((r, i) => (
                    <span key={r}>
                      {i > 0 && <span className="muted"> or </span>}
                      <code>{r}</code>
                    </span>
                  ))}
            </dd>
          </dl>

          <h3 className="key-title">Must find</h3>
          <ul className="key-list">
            {spec.evidence.required.map((e) => (
              <li key={e.fact}>{e.fact}</li>
            ))}
          </ul>

          <h3 className="key-title">{spec.safeActions.length > 1 ? "Correct fix, any of" : "Correct fix"}</h3>
          <ul className="key-list">
            {spec.safeActions.map((a) => (
              <li key={a}>{a}</li>
            ))}
          </ul>

          {spec.remediation && <Remediation remediation={spec.remediation} />}

          <Disclosure title="Grading details">
            <h3 className="key-title">Where each fact can be found</h3>
            <Facts facts={spec.evidence.required} />
            {spec.evidence.supporting.length > 0 && (
              <>
                <h3 className="key-title">Also worth checking, not graded</h3>
                <Facts facts={spec.evidence.supporting} />
              </>
            )}
            <h3 className="key-title">Must not recommend</h3>
            <ul className="key-list">
              {spec.unsafeActions.map((u) => (
                <li key={u.action}>
                  {u.action}
                  <span className="key-why">{u.why}</span>
                </li>
              ))}
            </ul>
          </Disclosure>
        </>
      )}
    </aside>
  );
}

function Facts({ facts }: { facts: EvidenceFact[] }) {
  return (
    <ul className="key-list">
      {facts.map((e) => (
        <li key={e.fact}>
          {e.fact}
          <span className="key-source">{e.anyOf.map(source).join(" or ")}</span>
        </li>
      ))}
    </ul>
  );
}
