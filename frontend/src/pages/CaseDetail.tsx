import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  ArrowRight,
  Check,
  ChevronDown,
  Database,
  FileText,
  GitBranch,
  Play,
  Radio,
  ShieldCheck,
  Square,
  Timer,
} from "lucide-react";
import { api, streamCase } from "../api";
import {
  EmptyState,
  ErrorState,
  RouteBadge,
  Skeleton,
  VerdictChip,
  human,
  money,
  percent,
} from "../components";
import type {
  Action,
  Approval,
  Answer,
  CaseDetail as Detail,
  Precedent,
  TimelineEvent,
  Verdict,
} from "../types";
import "./detail.css";
const GraphPanel = lazy(() => import("./GraphPanel"));
const eventLabels: Record<string, string> = {
  TRIGGER: "Trigger received",
  GATHER: "Gather graph evidence",
  ASSESS: "Assess independent evidence",
  INITIAL_NBA: "Initial recommendation",
  REQUEST_EVIDENCE: "Request verification",
  REASSESS: "Reassess with new evidence",
  FINAL_NBA: "Final recommendation",
  SAR: "Review report requirement",
  WRITE_BACK: "Write case memory",
  ANSWER: "Investigation complete",
};
const asText = (value: unknown, fallback = "") =>
  typeof value === "string" ? value : fallback;
function Probability({
  probability,
  verdict,
  initial,
}: {
  probability: number;
  verdict: Verdict;
  initial?: number;
}) {
  return (
    <div className="probability-panel">
      <div className="section-label">Fraud probability</div>
      <div className="probability-value">
        <strong>{percent(probability)}</strong>
        <VerdictChip verdict={verdict} />
      </div>
      <div
        className="probability-track"
        role="meter"
        aria-label="Fraud probability"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={probability * 100}
      >
        <span className={verdict} style={{ width: `${probability * 100}%` }} />
        {[15, 30, 70, 85].map((value) => (
          <i key={value} style={{ left: `${value}%` }}>
            <b>{value / 100}</b>
          </i>
        ))}
      </div>
      <p className="helper">
        Policy thresholds. Evidence heuristic; not a calibrated probability.
      </p>
      {initial != null && Math.abs(initial - probability) > 0.0001 && (
        <div className="probability-change">
          {percent(initial)} <ArrowRight size={13} aria-hidden />{" "}
          {percent(probability)} <span>after evidence</span>
        </div>
      )}
    </div>
  );
}
function Families({ families }: { families: Record<string, number> }) {
  const maximum = Math.max(2.2, ...Object.values(families).map(Math.abs));
  return (
    <div className="family-section">
      <h3>Evidence contributions</h3>
      <p className="helper">Capped log-odds by independent family.</p>
      {Object.keys(families).length ? (
        Object.entries(families).map(([family, weight]) => (
          <div className="family" key={family}>
            <div>
              <span>{human(family)}</span>
              <span className="mono">
                {weight > 0 ? "+" : ""}
                {weight.toFixed(2)}
              </span>
            </div>
            <div className="family-track">
              <i />
              <span
                className={weight < 0 ? "negative" : "positive"}
                style={{
                  left:
                    weight < 0
                      ? `${50 - (Math.abs(weight) / maximum) * 50}%`
                      : "50%",
                  width: `${(Math.abs(weight) / maximum) * 50}%`,
                }}
              />
            </div>
          </div>
        ))
      ) : (
        <p className="muted">No family breakdown was saved for this run.</p>
      )}
      <div className="family-key">
        <span>← supports legitimate</span>
        <span>supports fraud →</span>
      </div>
    </div>
  );
}
function ActionList({ title, actions }: { title: string; actions: Action[] }) {
  return (
    <section className="nba-column">
      <h3>
        {title}
        <span>{actions.length} actions</span>
      </h3>
      {actions.map((action) => (
        <details key={action.action} className="action-item">
          <summary>
            <span>{human(action.action)}</span>
            <RouteBadge route={action.route} />
          </summary>
          <p>{action.reason}</p>
        </details>
      ))}
    </section>
  );
}
function TimelineRow({ event }: { event: TimelineEvent }) {
  const payload = event.payload;
  const type = event.event_type;
  if (type === "step_started") {
    const step = asText(payload.step) || asText(payload.state);
    return (
      <li className="timeline-step">
        <span className="step-icon">
          <Check size={11} />
        </span>
        <strong>{eventLabels[step] || human(step)}</strong>
        {typeof payload.elapsed_s === "number" && (
          <span className="event-time mono">
            {payload.elapsed_s.toFixed(2)}s
          </span>
        )}
      </li>
    );
  }
  if (type === "tool_call")
    return (
      <li className="timeline-tool">
        <Database size={14} aria-hidden />
        <div>
          <code>{asText(payload.tool)}</code>
          <small>
            {payload.execution_observed === false
              ? "Cited query · historical timing unavailable"
              : `${typeof payload.duration_ms === "number" ? Math.round(payload.duration_ms) + " ms" : "Timing unavailable"} · ${payload.rows == null ? "no row count" : String(payload.rows) + " rows"}`}
          </small>
          {payload.success === false && (
            <small className="fraud-text">
              Call failed: {asText(payload.error_type)}
            </small>
          )}
        </div>
      </li>
    );
  if (type === "assessment")
    return (
      <li className="timeline-assessment">
        <span>{human(asText(payload.phase, "current"))} assessment</span>
        <strong>{percent(payload.probability as number)}</strong>
        <VerdictChip verdict={payload.verdict as Verdict} />
      </li>
    );
  if (type === "evidence_added")
    return (
      <li className="timeline-evidence">
        <details>
          <summary>
            Evidence from {asText(payload.source, "record")}{" "}
            <ChevronDown size={12} aria-hidden />
          </summary>
          <p>{asText(payload.claim)}</p>
          <small>{asText(payload.ref)}</small>
        </details>
      </li>
    );
  if (type === "evidence_requested")
    return (
      <li className="timeline-request">
        <strong>{human(asText(payload.type))} requested</strong>
        <p>{asText(payload.reason)}</p>
        <small>
          Precedent basis:{" "}
          {Array.isArray(payload.precedent_ids)
            ? payload.precedent_ids.join(", ")
            : "See saved evidence"}
        </small>
      </li>
    );
  if (type === "evidence_received")
    return (
      <li className="timeline-request received">
        <span className="source-chip">Simulated response</span>
        <p>{asText(payload.assumed_response)}</p>
      </li>
    );
  if (type === "nba_initial" || type === "nba_final")
    return (
      <li className="timeline-actions">
        <span>{type === "nba_initial" ? "Initial NBA" : "Final NBA"}</span>
        <p>
          {Array.isArray(payload.actions)
            ? (payload.actions as Action[])
                .map((x) => human(x.action))
                .join(" · ")
            : "Recommendation recorded"}
        </p>
      </li>
    );
  if (type === "sar_drafted")
    return (
      <li className="timeline-note">
        <FileText size={14} aria-hidden />
        {payload.file
          ? "SAR drafted · L2 approval required"
          : "SAR threshold not met"}
      </li>
    );
  if (type === "case_written")
    return (
      <li className="timeline-note">
        <Database size={14} aria-hidden />
        {payload.historical_writeback
          ? "Saved answer records historical graph write"
          : payload.written_to_graph
            ? "Case stored in graph"
            : "Case retained locally"}
      </li>
    );
  if (type === "error")
    return (
      <li className="timeline-request error-state" role="alert">
        {asText(payload.message) ||
          asText(payload.error, "Investigation failed. See service logs.")}
      </li>
    );
  if (type === "done")
    return (
      <li className="timeline-done">
        <ShieldCheck size={16} aria-hidden />
        Record ready for review
      </li>
    );
  return null;
}
function Precedents({ caseId }: { caseId: string }) {
  const query = useQuery({
    queryKey: ["precedents", caseId],
    queryFn: () =>
      api<{ precedents: Precedent[] }>(`/cases/${caseId}/precedents`),
  });
  if (query.isPending) return <Skeleton />;
  if (query.isError)
    return (
      <ErrorState error={query.error} retry={() => void query.refetch()} />
    );
  if (!query.data.precedents.length)
    return (
      <EmptyState title="No prior cases cited">
        This answer does not cite historical cases.
      </EmptyState>
    );
  return (
    <div className="precedent-grid">
      {query.data.precedents.map((prior) => (
        <article className="precedent-card" key={prior.case_id}>
          <header>
            <strong className="mono">{prior.case_id}</strong>
            <span
              className={`chip ${prior.outcome === "confirmed_fraud" ? "fraud" : prior.outcome === "cleared" ? "legitimate" : "pending"}`}
            >
              {human(prior.outcome)}
            </span>
          </header>
          <div className="precedent-meta">
            <span>{human(prior.pattern)}</span>
            <span>
              {prior.similarity == null
                ? "Similarity not recorded"
                : `Similarity ${prior.similarity.toFixed(3)}`}
            </span>
          </div>
          <p>
            {prior.explanation ||
              prior.analyst_notes ||
              "No analyst narrative available."}
          </p>
          <div className="provenance-chips">
            {prior.provenance.length ? (
              prior.provenance.map((item, i) => (
                <span key={i} className="source-chip">
                  {typeof item === "string"
                    ? item
                    : [item.source, ...(item.paths || [])]
                        .map(human)
                        .join(" / ")}
                </span>
              ))
            ) : (
              <span className="helper">
                Legacy answer: detailed ranking provenance was not recorded.
              </span>
            )}
          </div>
        </article>
      ))}
    </div>
  );
}
export default function CaseDetail() {
  const { id = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") || "evidence";
  const client = useQueryClient();
  const query = useQuery({
    queryKey: ["case", id],
    queryFn: () => api<Detail>(`/cases/${id}`),
  });
  const approvals = useQuery({
    queryKey: ["approvals"],
    queryFn: () =>
      api<{ pending: Approval[]; audit: Approval[] }>("/approvals"),
    enabled: Boolean(query.data?.answer?.sar.file),
  });
  const [streamEvents, setStreamEvents] = useState<TimelineEvent[] | null>(
    null,
  );
  const [mode, setMode] = useState<"live" | "replay" | null>(null);
  const [streamError, setStreamError] = useState("");
  const [runAnswer, setRunAnswer] = useState<Answer | null>(null);
  const [follow, setFollow] = useState(true);
  const controller = useRef<AbortController | null>(null);
  const scroll = useRef<HTMLDivElement>(null);
  useEffect(() => {
    setStreamEvents(null);
    setRunAnswer(null);
    setMode(null);
    setStreamError("");
    return () => controller.current?.abort();
  }, [id]);
  useEffect(() => {
    if (follow && mode && scroll.current)
      scroll.current.scrollTop = scroll.current.scrollHeight;
  }, [streamEvents, follow, mode]);
  async function start(selected: "live" | "replay") {
    controller.current?.abort();
    const abort = new AbortController();
    controller.current = abort;
    setStreamEvents([]);
    setRunAnswer(null);
    setMode(selected);
    setStreamError("");
    try {
      await streamCase(id, selected, abort.signal, (event) => {
        setStreamEvents((previous) => [...(previous || []), event]);
        if (event.event_type === "error")
          setStreamError(
            asText(event.payload.message) ||
              asText(
                event.payload.error,
                "The investigation could not finish.",
              ),
          );
        if (event.event_type === "done" && event.payload.answer)
          setRunAnswer(event.payload.answer as Answer);
      });
      if (selected === "live") {
        void client.invalidateQueries({ queryKey: ["case", id] });
        void client.invalidateQueries({ queryKey: ["cases"] });
        void client.invalidateQueries({ queryKey: ["approvals"] });
      }
    } catch (error) {
      if (!abort.signal.aborted)
        setStreamError(
          error instanceof Error ? error.message : "Stream failed.",
        );
    } finally {
      if (controller.current === abort) setMode(null);
    }
  }
  if (query.isPending) return <Skeleton rows={10} />;
  if (query.isError)
    return (
      <ErrorState error={query.error} retry={() => void query.refetch()} />
    );
  const data = query.data;
  const answer = runAnswer || data.answer;
  const trigger = data.trigger;
  const events = streamEvents || data.timeline;
  const assessments = events.filter(
    (event) => event.event_type === "assessment",
  );
  const last = assessments.at(-1)?.payload;
  const caseData = answer?.case;
  const pendingReport = approvals.data?.pending.some(
    (item) => item.case_id === id && item.action === "FILE_REPORT",
  );
  const reportDecision = approvals.data?.audit
    .filter((item) => item.case_id === id && item.action === "FILE_REPORT")
    .at(-1);
  const reportStatus = !approvals.data
    ? "Draft prepared · approval status unavailable"
    : pendingReport || !reportDecision
      ? "Draft prepared · awaiting L2 approval"
      : reportDecision.decision === "approve"
        ? "Approved internally · external filing not executed"
        : "Filing recommendation rejected · not filed";
  const probability =
    mode && last
      ? Number(last.probability)
      : (caseData?.fraud_probability ?? 0);
  const verdict =
    mode && last ? (last.verdict as Verdict) : caseData?.verdict || "uncertain";
  const families = (
    mode && last?.family_contributions
      ? last.family_contributions
      : data.family_contributions ||
        assessments.at(-1)?.payload.family_contributions ||
        {}
  ) as Record<string, number>;
  const firstAssessment = data.timeline.find(
    (event) => event.event_type === "assessment",
  )?.payload;
  const initial =
    data.initial_probability ??
    (firstAssessment?.probability as number | undefined);
  const source = events.some((event) => event.source === "reconstructed")
    ? "Reconstructed from saved records"
    : streamEvents
      ? "Recorded event stream"
      : "Saved event stream";
  const tabs = [
    ["evidence", "Evidence"],
    ["graph", "Graph"],
    ["precedents", "Precedents"],
    ["json", "Raw JSON"],
  ];
  const setTab = (value: string) => {
    const next = new URLSearchParams(params);
    next.set("tab", value);
    setParams(next, { replace: true });
  };
  return (
    <>
      <Link className="back-link" to="/">
        <ArrowLeft size={14} aria-hidden />
        Case queue
      </Link>
      <div className="detail-heading">
        <div>
          <div className="case-title">
            <h1 className="mono">{id}</h1>
            {caseData && <VerdictChip verdict={caseData.verdict} />}
            <span className="status-text">{human(data.status)}</span>
          </div>
          <p>
            {human(trigger.trigger_type)} · Opened {trigger.opened_at} UTC
          </p>
        </div>
        <div className="run-controls">
          <button
            className="button"
            disabled={!!mode || !data.timeline.length}
            onClick={() => void start("replay")}
          >
            <Play size={15} aria-hidden />
            {mode === "replay" ? "Replaying…" : "Replay"}
          </button>
          <button
            className="button primary"
            disabled={!!mode}
            onClick={() => void start("live")}
          >
            <Radio size={15} aria-hidden />
            {mode === "live" ? "Investigating…" : "Run live"}
          </button>
          {mode && (
            <button
              className="icon-button"
              title="Disconnect this view; a live investigation continues in the background"
              aria-label="Stop viewing stream"
              onClick={() => {
                controller.current?.abort();
                setMode(null);
              }}
            >
              <Square size={15} />
            </button>
          )}
        </div>
      </div>
      <div className="case-source">
        <span className="dot" />
        {data.answer_source === "runtime"
          ? "Runtime investigation · graded answers preserved"
          : "Saved benchmark answer"}
        <span>
          {answer?.metadata?.llm_model_used
            ? `Narration: ${answer.metadata.llm_model_used}`
            : "Saved narration: deterministic template"}
        </span>
      </div>
      {streamError && <ErrorState error={new Error(streamError)} />}
      <div className="detail-grid">
        <aside className="facts-column">
          <section className="panel">
            <div className="panel-heading">
              <h2>Case facts</h2>
              <GitBranch size={16} aria-hidden />
            </div>
            <dl className="facts-list">
              <div>
                <dt>Card</dt>
                <dd className="mono">{trigger.card_id}</dd>
              </div>
              <div>
                <dt>Customer</dt>
                <dd className="mono">{trigger.customer_id}</dd>
              </div>
              <div>
                <dt>Flagged transaction</dt>
                <dd className="mono">{trigger.flagged_txn_id}</dd>
              </div>
              <div>
                <dt>Identified exposure</dt>
                <dd className="exposure-value">
                  {caseData
                    ? money(caseData.exposure_usd)
                    : "Awaiting assessment"}
                </dd>
              </div>
              <div>
                <dt>Pattern</dt>
                <dd>{human(caseData?.pattern)}</dd>
              </div>
              <div>
                <dt>Affected transactions</dt>
                <dd>{caseData?.affected_txn_ids.length ?? "—"}</dd>
              </div>
              <div>
                <dt>Connected known cards</dt>
                <dd>{caseData?.connected_card_ids.length ?? "—"}</dd>
              </div>
            </dl>
            <div className="trigger-text">
              <h3>Trigger</h3>
              <p>{trigger.trigger_text}</p>
            </div>
          </section>
          {caseData && (
            <section className="panel">
              <Probability
                probability={probability}
                verdict={verdict}
                initial={initial}
              />
              <Families families={families} />
            </section>
          )}
        </aside>
        <section className="panel timeline-panel">
          <div className="panel-heading">
            <h2>Investigation timeline</h2>
            <span className={`stream-status ${mode ? "streaming" : ""}`}>
              {mode ? "Streaming" : "Stored"}
            </span>
          </div>
          <div className="timeline-caption">
            <span>{source}</span>
            <label>
              <input
                type="checkbox"
                checked={follow}
                onChange={(e) => setFollow(e.target.checked)}
              />
              Follow
            </label>
          </div>
          <div
            className="timeline-scroll"
            ref={scroll}
            aria-label="Investigation events"
          >
            {events.length ? (
              <ol className="timeline">
                {events.map((event, index) => (
                  <TimelineRow
                    key={`${index}-${event.event_type}`}
                    event={event}
                  />
                ))}
              </ol>
            ) : (
              <EmptyState
                title={
                  mode ? "Waiting for first event…" : "Ready to investigate"
                }
              >
                {mode
                  ? "The run will appear here as evidence arrives."
                  : "Run this case live to create its evidence record."}
              </EmptyState>
            )}
          </div>
          <footer className="timeline-footer">
            <Timer size={13} aria-hidden />
            {answer
              ? `${answer.tool_calls} recorded tool calls · ${answer.latency_s.toFixed(2)}s original run`
              : "No completed run"}
            <p>
              Replay pacing is illustrative. Replay makes no graph or LLM calls.
            </p>
          </footer>
        </section>
        <div className="decisions-column">
          {answer ? (
            <>
              <section className="panel nba-panel">
                <div className="panel-heading">
                  <h2>Next best actions</h2>
                  <ShieldCheck size={16} aria-hidden />
                </div>
                <div className="nba-grid">
                  <ActionList
                    title="Initial NBA"
                    actions={answer.next_best_actions.initial}
                  />
                  <ActionList
                    title="Final NBA"
                    actions={answer.next_best_actions.final}
                  />
                </div>
                <div className="what-changed">
                  <strong>What changed</strong>
                  <p>
                    {answer.next_best_actions.what_changed === "nothing"
                      ? "No additional evidence was needed. Initial recommendations remain supported."
                      : answer.next_best_actions.what_changed}
                  </p>
                </div>
                <p className="data-note">
                  Expand an action to see its policy citations. L1 and L2
                  actions await approval.
                </p>
              </section>
              <section className="panel requests-panel">
                <div className="panel-heading">
                  <h2>Evidence requests</h2>
                  <span className="count-label">
                    {answer.evidence_requests.length}
                  </span>
                </div>
                {answer.evidence_requests.length ? (
                  answer.evidence_requests.map((request, index) => (
                    <details key={index} open>
                      <summary>
                        {human(request.type)}
                        <span className="source-chip">Simulated</span>
                      </summary>
                      <p>{request.assumed_response}</p>
                    </details>
                  ))
                ) : (
                  <p className="data-note">
                    No additional replies were simulated for this case.
                  </p>
                )}
              </section>
              <section
                className={`panel sar-panel ${answer.sar.file ? "report-required" : ""}`}
              >
                <div className="panel-heading">
                  <h2>Suspicious activity report</h2>
                  {answer.sar.file ? (
                    <RouteBadge route="L2" />
                  ) : (
                    <span className="source-chip">Not recommended</span>
                  )}
                </div>
                <p className="sar-state">
                  {answer.sar.file ? reportStatus : "Internal case only"}
                </p>
                <p className="data-note">{answer.sar.reason}</p>
                {answer.sar.file && (
                  <details>
                    <summary>Read SAR narrative</summary>
                    <p>{answer.sar.narrative}</p>
                    <div className="sar-meta">
                      {money(answer.sar.total_amount_usd)} ·{" "}
                      {answer.sar.activity_dates.join(" to ")}
                    </div>
                  </details>
                )}
                <p className="helper sar-disclosure">
                  No external report has been filed.
                </p>
              </section>
            </>
          ) : (
            <section className="panel">
              <EmptyState title="Recommendations pending">
                Run the investigation to assess the trigger and produce actions.
              </EmptyState>
            </section>
          )}
        </div>
      </div>
      {answer && (
        <>
          <section className="case-summary">
            <div>
              <h2>Analyst summary</h2>
              <p>{caseData?.summary}</p>
              {caseData?.pattern_description && (
                <p>{caseData.pattern_description}</p>
              )}
            </div>
            <div>
              <h3>Stopping decision</h3>
              <p>{answer.stop_reason}</p>
            </div>
          </section>
          <section className="panel evidence-panel">
            <div
              className="tabs"
              role="tablist"
              aria-label="Case evidence views"
            >
              {tabs.map(([value, label], index) => (
                <button
                  key={value}
                  id={`tab-${value}`}
                  type="button"
                  role="tab"
                  aria-selected={tab === value}
                  aria-controls="case-tab-panel"
                  tabIndex={tab === value ? 0 : -1}
                  className={tab === value ? "active" : ""}
                  onClick={() => setTab(value)}
                  onKeyDown={(event) => {
                    if (
                      event.key === "ArrowRight" ||
                      event.key === "ArrowLeft"
                    ) {
                      event.preventDefault();
                      const next =
                        tabs[
                          (index +
                            (event.key === "ArrowRight"
                              ? 1
                              : tabs.length - 1)) %
                            tabs.length
                        ][0];
                      setTab(next);
                      document.getElementById(`tab-${next}`)?.focus();
                    }
                  }}
                >
                  {label}
                  {value === "evidence" && (
                    <span>{caseData?.evidence.length}</span>
                  )}
                </button>
              ))}
            </div>
            <div
              role="tabpanel"
              id="case-tab-panel"
              aria-labelledby={`tab-${tab}`}
              tabIndex={0}
            >
              {tab === "evidence" && (
                <div className="table-scroll">
                  <table className="evidence-table">
                    <thead>
                      <tr>
                        <th>Source</th>
                        <th>Evidence</th>
                        <th>References</th>
                        <th>Weight</th>
                      </tr>
                    </thead>
                    <tbody>
                      {caseData?.evidence.map((entry, index) => (
                        <tr key={index}>
                          <td>
                            <span className="source-chip">{entry.source}</span>
                          </td>
                          <td>
                            <p>{entry.claim}</p>
                            <small>{entry.ref}</small>
                          </td>
                          <td>
                            <div className="evidence-ids">
                              {entry.entity_ids.length ? (
                                entry.entity_ids.map((entity) => (
                                  <span className="mono" key={entity}>
                                    {entity}
                                  </span>
                                ))
                              ) : (
                                <span className="muted">
                                  Policy or response evidence
                                </span>
                              )}
                            </div>
                          </td>
                          <td className="muted">
                            {entry.weight ?? "By family"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              {tab === "graph" && (
                <Suspense fallback={<Skeleton rows={6} />}>
                  <GraphPanel caseId={id} />
                </Suspense>
              )}
              {tab === "precedents" && <Precedents caseId={id} />}{" "}
              {tab === "json" && (
                <pre className="raw-json">
                  {JSON.stringify(answer, null, 2)}
                </pre>
              )}
            </div>
          </section>
        </>
      )}
    </>
  );
}
