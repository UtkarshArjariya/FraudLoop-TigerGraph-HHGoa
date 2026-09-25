import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  AlertTriangle,
  ArrowUpRight,
  Check,
  Pause,
  Play,
  Radio,
  Save,
  Settings2,
  Square,
} from "lucide-react";
import { api } from "../api";
import { useRole } from "../App";
import {
  EmptyState,
  ErrorState,
  PageHeading,
  Skeleton,
  VerdictChip,
  human,
  money,
  percent,
} from "../components";
import type { EvidenceMode, TimelineEvent, Verdict } from "../types";
import "./secondary.css";
import "./live.css";

interface StreamTransaction {
  txn_id: string;
  amount?: number;
  card_id?: string;
  device_profile?: string;
  risk_band?: string;
  risk_score?: number;
  alert_score?: number;
  ts?: string | number;
  epoch?: number;
  timestamp?: string;
  graph?: { source?: string; written_to_graph?: boolean; warning?: string };
}
interface StreamAlert extends StreamTransaction {
  reasons: string[];
  case_id?: string;
  queue_status?: string;
}
interface StreamCase {
  case_id: string;
  txn_id?: string;
  status?: string;
  verdict?: Verdict | null;
  mode_evidence?: EvidenceMode;
  error?: string;
}
interface StreamStatus {
  state: "stopped" | "starting" | "running" | "paused" | "completed" | "error";
  running: boolean;
  paused: boolean;
  speed_multiplier: number;
  rate_limit_tps: number;
  target_tps: number;
  mode_evidence: EvidenceMode;
  start_date?: string;
  range?: { start: string; end: string } | null;
  cursor: number;
  total_transactions: number;
  transactions_ingested: number;
  alerts_raised: number;
  cases_opened: number;
  investigations_done: number;
  queue_depth: number;
  max_pending: number;
  active_case_id?: string | null;
  throughput_tps: number;
  alert_rate: number;
  graph: { source: string; written_to_graph: boolean; warning?: string };
  feed: StreamTransaction[];
  alerts: StreamAlert[];
  cases: StreamCase[];
  error?: string;
}
interface DetectorConfig {
  threshold: number;
  [key: string]: number;
}
interface DetectorResponse {
  config: DetectorConfig;
  defaults: DetectorConfig;
}
const eventTypes = [
  "txn_ingested",
  "alert_raised",
  "case_opened",
  "investigation_done",
  "stream_status",
];
const timeValue = (value: string | number | undefined) => {
  if (typeof value === "number")
    return value < 100000000000 ? value * 1000 : value;
  return value ? Date.parse(value) : 0;
};
function timeLabel(value: string | number | undefined) {
  const time = timeValue(value);
  return time && Number.isFinite(time)
    ? new Date(time).toISOString().slice(11, 19)
    : "—";
}
function uniqueRecent<T>(
  items: T[],
  key: (item: T) => string,
  limit: number,
): T[] {
  const seen = new Set<string>();
  return items
    .filter((item) => {
      const id = key(item);
      if (!id || seen.has(id)) return false;
      seen.add(id);
      return true;
    })
    .slice(0, limit);
}

export default function LiveMonitor() {
  const { role } = useRole();
  const canControl = role === "supervisor_l2";
  const client = useQueryClient();
  const statusQuery = useQuery({
    queryKey: ["stream-status"],
    queryFn: () => api<StreamStatus>("/stream/status"),
    refetchInterval: 2000,
  });
  const detector = useQuery({
    queryKey: ["detector-config"],
    queryFn: () => api<DetectorResponse>("/detector/config"),
  });
  const [connected, setConnected] = useState(false);
  const [recentEvents, setRecentEvents] = useState<TimelineEvent[]>([]);
  const [speed, setSpeed] = useState(1);
  const [startDate, setStartDate] = useState("");
  const [evidenceMode, setEvidenceMode] = useState<EvidenceMode>("simulated");
  const [threshold, setThreshold] = useState(0.7);
  const [follow, setFollow] = useState(true);
  const feedScroll = useRef<HTMLDivElement>(null);
  const initialized = useRef(false);
  const previousCount = useRef(0);
  const data = statusQuery.data;
  useEffect(() => {
    if (data && !initialized.current) {
      setSpeed(data.speed_multiplier);
      setEvidenceMode(data.mode_evidence);
      setStartDate(
        (data.start_date || data.range?.start || "")
          .replace(" ", "T")
          .slice(0, 19),
      );
      initialized.current = true;
    }
    if (data && data.transactions_ingested < previousCount.current)
      setRecentEvents([]);
    if (data) previousCount.current = data.transactions_ingested;
  }, [data]);
  useEffect(() => {
    if (detector.data) setThreshold(detector.data.config.threshold);
  }, [detector.data]);
  useEffect(() => {
    const source = new EventSource("/api/stream/events");
    source.onopen = () => setConnected(true);
    source.onerror = () => setConnected(false);
    const receive = (message: MessageEvent) => {
      try {
        const value = JSON.parse(message.data);
        const event: TimelineEvent = value.payload
          ? value
          : { event_type: message.type, payload: value };
        if (event.event_type === "stream_status") {
          client.setQueryData(
            ["stream-status"],
            event.payload as unknown as StreamStatus,
          );
        } else {
          setRecentEvents((previous) => [...previous, event].slice(-200));
          if (
            event.event_type === "case_opened" ||
            event.event_type === "investigation_done"
          ) {
            void client.invalidateQueries({ queryKey: ["cases"] });
          }
        }
      } catch {
        /* Polling retains the last valid status when an event is incomplete. */
      }
    };
    eventTypes.forEach((type) => source.addEventListener(type, receive));
    return () => {
      source.close();
    };
  }, [client]);
  const control = useMutation({
    mutationFn: ({
      action,
      body,
    }: {
      action: "start" | "pause" | "stop";
      body?: object;
    }) =>
      api<StreamStatus>(`/stream/${action}`, {
        method: "POST",
        ...(body ? { body: JSON.stringify(body) } : {}),
      }),
    onSuccess: (result) => {
      client.setQueryData(["stream-status"], result);
      void statusQuery.refetch();
    },
  });
  const saveDetector = useMutation({
    mutationFn: (config: DetectorConfig) =>
      api<DetectorResponse>("/detector/config", {
        method: "PUT",
        body: JSON.stringify(config),
      }),
    onSuccess: (result) => {
      client.setQueryData(["detector-config"], result);
    },
  });
  const feed = useMemo(
    () =>
      uniqueRecent(
        [
          ...recentEvents
            .filter((event) => event.event_type === "txn_ingested")
            .reverse()
            .map(
              (event) =>
                ({
                  ...event.payload,
                  timestamp: event.timestamp,
                }) as unknown as StreamTransaction,
            ),
          ...(data?.feed || []),
        ],
        (item) => item.txn_id,
        50,
      ).sort(
        (first, second) =>
          timeValue(second.epoch || second.ts || second.timestamp) -
          timeValue(first.epoch || first.ts || first.timestamp),
      ),
    [recentEvents, data?.feed],
  );
  const alerts = useMemo(
    () =>
      uniqueRecent(
        [
          ...(data?.alerts || []),
          ...recentEvents
            .filter((event) => event.event_type === "alert_raised")
            .reverse()
            .map((event) => event.payload as unknown as StreamAlert),
        ],
        (item) => item.txn_id,
        40,
      ),
    [recentEvents, data?.alerts],
  );
  const streamCases = useMemo(
    () =>
      uniqueRecent(
        [
          ...(data?.cases || []),
          ...recentEvents
            .filter(
              (event) =>
                event.event_type === "case_opened" ||
                event.event_type === "investigation_done",
            )
            .reverse()
            .map((event) => event.payload as unknown as StreamCase),
        ],
        (item) => item.case_id,
        30,
      ),
    [recentEvents, data?.cases],
  );
  useEffect(() => {
    if (follow && feedScroll.current) feedScroll.current.scrollTop = 0;
  }, [feed, follow]);
  function start(event: FormEvent) {
    event.preventDefault();
    const selectedStart =
      startDate && data?.range?.start.startsWith(startDate)
        ? data.range.start
        : `${startDate}Z`;
    control.mutate({
      action: "start",
      body: {
        speed_multiplier: speed,
        mode_evidence: evidenceMode,
        ...(!active && startDate ? { start_date: selectedStart } : {}),
      },
    });
  }
  const running = data?.state === "running" || data?.state === "starting";
  const active = running || data?.state === "paused";
  const blocked = !canControl || control.isPending;
  const graphWarning =
    data?.graph?.warning ||
    (data?.graph?.source === "local"
      ? "Graph updates are unavailable. The simulator is using local prepared data; no transaction graph write is claimed."
      : "");
  return (
    <div className="secondary-page live-page">
      <PageHeading
        title="Live monitor"
        description="Replay the final two months of transactions, inspect alerts, and follow the investigations they open."
      >
        <span className={`live-connection ${connected ? "connected" : ""}`}>
          <span className="dot" />
          {connected
            ? "Event stream connected"
            : "Polling · event stream reconnecting"}
        </span>
      </PageHeading>
      {statusQuery.isPending ? (
        <Skeleton rows={8} />
      ) : statusQuery.isError && !data ? (
        <ErrorState
          error={statusQuery.error}
          retry={() => void statusQuery.refetch()}
        />
      ) : (
        data && (
          <>
            {graphWarning && (
              <div className="live-graph-notice" role="status">
                <AlertTriangle size={19} aria-hidden />
                <div>
                  <strong>
                    {data.graph?.source === "local"
                      ? "Local graph fallback"
                      : "Stream notice"}
                  </strong>
                  <p>{graphWarning}</p>
                </div>
              </div>
            )}
            {data.error && (
              <div className="live-graph-notice error" role="alert">
                <AlertTriangle size={19} aria-hidden />
                <p>{data.error}</p>
              </div>
            )}
            <section
              className="secondary-panel live-controls"
              aria-labelledby="stream-controls-title"
            >
              <div className="secondary-panel-heading">
                <div>
                  <h2 id="stream-controls-title">
                    <Radio size={17} aria-hidden />
                    Stream controls
                  </h2>
                  <p>
                    Transactions are simulated from the supplied dataset. Bank
                    actions remain recommendations.
                  </p>
                </div>
                <span className={`live-state state-${data.state}`}>
                  <span className="dot" />
                  {human(data.state)}
                </span>
              </div>
              <form onSubmit={start}>
                <div className="live-control-fields">
                  <label>
                    Start time <span className="helper">UTC</span>
                    <input
                      type="datetime-local"
                      step={1}
                      aria-label="Start time (UTC)"
                      value={startDate}
                      min={data.range?.start?.replace(" ", "T").slice(0, 19)}
                      max={data.range?.end?.replace(" ", "T").slice(0, 19)}
                      disabled={blocked || active}
                      onChange={(event) => setStartDate(event.target.value)}
                    />
                  </label>
                  <label>
                    Speed multiplier
                    <div className="live-speed">
                      <input
                        type="number"
                        min={0.1}
                        max={10}
                        step={0.1}
                        required
                        value={speed}
                        disabled={blocked}
                        onChange={(event) =>
                          setSpeed(Number(event.target.value))
                        }
                      />
                      <span>×</span>
                    </div>
                  </label>
                  <label>
                    Evidence responses
                    <select
                      value={evidenceMode}
                      disabled={blocked || active}
                      onChange={(event) =>
                        setEvidenceMode(event.target.value as EvidenceMode)
                      }
                    >
                      <option value="simulated">Simulated</option>
                      <option value="human">Human</option>
                    </select>
                  </label>
                  <div className="live-control-buttons">
                    <button
                      type="submit"
                      className="button primary"
                      disabled={blocked}
                      aria-busy={control.isPending}
                    >
                      <Play size={14} aria-hidden />
                      {running
                        ? "Apply speed"
                        : data.state === "paused"
                          ? "Resume stream"
                          : "Start stream"}
                    </button>
                    <button
                      type="button"
                      className="button"
                      disabled={blocked || !running}
                      onClick={() => control.mutate({ action: "pause" })}
                    >
                      <Pause size={14} aria-hidden />
                      Pause
                    </button>
                    <button
                      type="button"
                      className="button"
                      disabled={blocked || !active}
                      onClick={() => control.mutate({ action: "stop" })}
                    >
                      <Square size={13} aria-hidden />
                      Stop
                    </button>
                  </div>
                </div>
                <div className="live-control-note">
                  <p>
                    {canControl
                      ? `Capped at ${data.rate_limit_tps} transactions/second. One investigation runs at a time; up to ${data.max_pending} wait in the queue. Pause and stop affect the feed; existing investigations continue.`
                      : "Select Supervisor · L2 to control the stream and detector. All roles can monitor activity."}
                  </p>
                  <span className="mono">
                    Target {data.target_tps?.toFixed(1) || "0.0"} tx/s
                  </span>
                </div>
                {control.isError && (
                  <p className="form-error" role="alert">
                    {control.error.message}
                  </p>
                )}
              </form>
            </section>
            <div
              className="secondary-stats live-stats"
              aria-label="Live stream counters"
            >
              <div>
                <span>Transactions ingested</span>
                <strong className="mono">
                  {data.transactions_ingested.toLocaleString()}
                </strong>
                <small>
                  of {data.total_transactions.toLocaleString()} available
                </small>
              </div>
              <div>
                <span>Throughput</span>
                <strong className="mono">
                  {data.throughput_tps.toFixed(1)}
                  <small> tx/s</small>
                </strong>
                <small>Observed rate</small>
              </div>
              <div>
                <span>Alerts raised</span>
                <strong className="mono">
                  {data.alerts_raised.toLocaleString()}
                </strong>
                <small>
                  {percent(data.alert_rate)} of ingested transactions
                </small>
              </div>
              <div>
                <span>Investigations</span>
                <strong className="mono">
                  {data.cases_opened.toLocaleString()}
                </strong>
                <small>
                  {data.investigations_done} complete · {data.queue_depth}/
                  {data.max_pending} queued
                </small>
              </div>
            </div>
            <div className="live-workspace">
              <section
                className="secondary-panel live-feed-panel"
                aria-labelledby="transaction-feed-title"
              >
                <div className="secondary-panel-heading">
                  <div>
                    <h2 id="transaction-feed-title">
                      <Activity size={17} aria-hidden />
                      Transaction feed
                    </h2>
                    <p>
                      Latest {feed.length} transactions. Newest appears first.
                    </p>
                  </div>
                  <label className="live-follow">
                    <input
                      type="checkbox"
                      checked={follow}
                      onChange={(event) => setFollow(event.target.checked)}
                    />
                    Follow
                  </label>
                </div>
                <div
                  className="live-feed-scroll"
                  ref={feedScroll}
                  tabIndex={0}
                  aria-label="Recent transaction feed"
                >
                  {feed.length ? (
                    <table className="live-feed-table">
                      <thead>
                        <tr>
                          <th>Time · UTC</th>
                          <th>Transaction / card</th>
                          <th>Amount</th>
                          <th>Device profile</th>
                          <th>Risk band</th>
                        </tr>
                      </thead>
                      <tbody>
                        {feed.map((item) => (
                          <tr key={item.txn_id}>
                            <td className="mono">
                              {timeLabel(
                                item.epoch || item.ts || item.timestamp,
                              )}
                            </td>
                            <td>
                              <span className="mono">{item.txn_id}</span>
                              <small className="mono">
                                {item.card_id || "Unresolved card"}
                              </small>
                            </td>
                            <td className="mono">
                              {typeof item.amount === "number"
                                ? money(item.amount)
                                : "—"}
                            </td>
                            <td
                              className="live-device"
                              title={item.device_profile}
                            >
                              {item.device_profile || "No device profile"}
                            </td>
                            <td>
                              <span
                                className={`live-risk risk-${item.risk_band || "unknown"}`}
                              >
                                {human(item.risk_band || "unknown")}
                              </span>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  ) : (
                    <EmptyState title="No transactions ingested yet">
                      Start the stream as a supervisor to replay transactions
                      and watch detection results appear here.
                    </EmptyState>
                  )}
                </div>
              </section>
              <aside className="live-right-column">
                <section
                  className="secondary-panel live-alert-panel"
                  aria-labelledby="live-alerts-title"
                >
                  <div className="secondary-panel-heading">
                    <div>
                      <h2 id="live-alerts-title">Detection alerts</h2>
                      <p>Score and reasons from the detector.</p>
                    </div>
                    <span className="secondary-count mono">
                      {alerts.length} recent
                    </span>
                  </div>
                  {alerts.length ? (
                    <ol className="live-alert-list">
                      {alerts.map((alert) => (
                        <li key={alert.txn_id}>
                          <div className="live-alert-heading">
                            <strong className="mono">{alert.txn_id}</strong>
                            <span>
                              {percent(alert.alert_score)} alert score
                            </span>
                          </div>
                          <ul>
                            {(alert.reasons || []).map((reason, i) => (
                              <li key={i}>{reason}</li>
                            ))}
                          </ul>
                          {alert.queue_status && (
                            <p className="helper">
                              {human(alert.queue_status)}
                            </p>
                          )}
                          {alert.case_id && (
                            <Link
                              to={`/cases/${encodeURIComponent(alert.case_id)}`}
                              className="case-link mono"
                            >
                              {alert.case_id}
                              <ArrowUpRight size={12} aria-hidden />
                            </Link>
                          )}
                        </li>
                      ))}
                    </ol>
                  ) : (
                    <EmptyState title="No alerts raised">
                      Transactions that cross the saved threshold will appear
                      with the detector’s reasons.
                    </EmptyState>
                  )}
                </section>
                <section
                  className="secondary-panel live-case-panel"
                  aria-labelledby="live-cases-title"
                >
                  <div className="secondary-panel-heading">
                    <div>
                      <h2 id="live-cases-title">Auto-opened cases</h2>
                      <p>
                        {data.active_case_id
                          ? `Active: ${data.active_case_id}`
                          : "No investigation is active."}
                      </p>
                    </div>
                  </div>
                  {streamCases.length ? (
                    <ul className="live-case-list">
                      {streamCases.map((item) => (
                        <li key={item.case_id}>
                          <Link
                            className="case-link mono"
                            to={`/cases/${encodeURIComponent(item.case_id)}`}
                          >
                            {item.case_id}
                            <ArrowUpRight size={14} aria-hidden />
                          </Link>
                          <div>
                            {item.verdict ? (
                              <VerdictChip verdict={item.verdict} />
                            ) : (
                              <span className="helper">
                                {human(item.status || "queued")}
                              </span>
                            )}
                            {item.mode_evidence === "human" && (
                              <span className="live-human-label">
                                Human evidence
                              </span>
                            )}
                          </div>
                          {item.error && (
                            <p className="form-error">{item.error}</p>
                          )}
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <EmptyState title="No cases opened by this stream">
                      Alerts accepted into the investigation queue create cases
                      here.
                    </EmptyState>
                  )}
                </section>
              </aside>
            </div>
            <section
              className="secondary-panel live-detector"
              aria-labelledby="detector-title"
            >
              <div className="secondary-panel-heading">
                <div>
                  <h2 id="detector-title">
                    <Settings2 size={17} aria-hidden />
                    Detector threshold
                  </h2>
                  <p>
                    Control which scored transactions enter the investigation
                    queue.
                  </p>
                </div>
              </div>
              {detector.isPending ? (
                <Skeleton rows={2} />
              ) : detector.isError ? (
                <ErrorState
                  error={detector.error}
                  retry={() => void detector.refetch()}
                />
              ) : (
                detector.data && (
                  <form
                    onSubmit={(event) => {
                      event.preventDefault();
                      saveDetector.mutate({
                        ...detector.data.config,
                        threshold,
                      });
                    }}
                  >
                    <label htmlFor="detector-threshold">
                      Alert score threshold
                    </label>
                    <div className="detector-threshold-control">
                      <input
                        id="detector-threshold"
                        type="range"
                        min={0}
                        max={1}
                        step={0.01}
                        value={threshold}
                        disabled={!canControl || saveDetector.isPending}
                        onChange={(event) => {
                          setThreshold(Number(event.target.value));
                          saveDetector.reset();
                        }}
                      />
                      <input
                        aria-label="Alert score threshold value"
                        type="number"
                        min={0}
                        max={1}
                        step={0.01}
                        required
                        value={threshold}
                        disabled={!canControl || saveDetector.isPending}
                        onChange={(event) => {
                          setThreshold(Number(event.target.value));
                          saveDetector.reset();
                        }}
                      />
                      <button
                        type="submit"
                        className="button"
                        disabled={
                          !canControl ||
                          saveDetector.isPending ||
                          threshold === detector.data.config.threshold
                        }
                      >
                        <Save size={14} aria-hidden />
                        {saveDetector.isPending ? "Saving…" : "Save threshold"}
                      </button>
                      <button
                        className="button subtle"
                        type="button"
                        disabled={!canControl || saveDetector.isPending}
                        onClick={() => {
                          setThreshold(detector.data.defaults.threshold);
                          saveDetector.reset();
                        }}
                      >
                        Reset to default
                      </button>
                    </div>
                    <p className="helper">
                      Lower thresholds admit more alerts. The detector’s alert
                      score is separate from the agent’s fraud probability and
                      policy decision.
                    </p>
                    {saveDetector.isError && (
                      <p className="form-error" role="alert">
                        {saveDetector.error.message}
                      </p>
                    )}
                    {saveDetector.isSuccess && (
                      <p className="live-save-success" role="status">
                        <Check size={14} aria-hidden />
                        Threshold saved for subsequent transactions.
                      </p>
                    )}
                  </form>
                )
              )}
            </section>
          </>
        )
      )}
    </div>
  );
}
