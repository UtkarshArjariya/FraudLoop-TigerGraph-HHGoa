import { useRef, useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Check,
  CheckCheck,
  Clock3,
  Info,
  LockKeyhole,
  RotateCw,
  ShieldCheck,
  X,
} from "lucide-react";
import { useRole } from "../App";
import { api } from "../api";
import {
  EmptyState,
  ErrorState,
  PageHeading,
  RouteBadge,
  Skeleton,
  human,
} from "../components";
import type { Approval, Role } from "../types";
import "./secondary.css";

interface AuditEntry extends Approval {
  audit_id: string;
  executed: boolean;
  graph?: { written_to_graph: boolean; reason?: string };
}
interface ApprovalResponse {
  pending: Approval[];
  audit: AuditEntry[];
  disclosure: string;
}
interface DecisionResponse {
  approval: AuditEntry;
  written_to_graph: boolean;
  executed: boolean;
}
type Decision = "approve" | "reject";
const roleNames: Record<Role, string> = {
  viewer: "Viewer",
  analyst_l1: "Analyst · L1",
  supervisor_l2: "Supervisor · L2",
};
const permission = (role: Role, route: string) =>
  role === "supervisor_l2" || (role === "analyst_l1" && route === "L1");
const actionName = (action: string) =>
  human(action.toLowerCase()).replace(/^./, (value) => value.toUpperCase());
const decisionTime = (value?: string) => {
  if (!value) return "Time unavailable";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleString(undefined, {
        dateStyle: "medium",
        timeStyle: "short",
      });
};

export default function Approvals() {
  const { role } = useRole();
  const client = useQueryClient();
  const query = useQuery({
    queryKey: ["approvals"],
    queryFn: () => api<ApprovalResponse>("/approvals"),
  });
  const [params, setParams] = useSearchParams();
  const route = ["L1", "L2"].includes(params.get("route") || "")
    ? params.get("route")!
    : "";
  const dialog = useRef<HTMLDialogElement>(null);
  const lastTrigger = useRef<HTMLButtonElement | null>(null);
  const pendingHeading = useRef<HTMLHeadingElement>(null);
  const [selected, setSelected] = useState<{
    approval: Approval;
    decision: Decision;
  } | null>(null);
  const [note, setNote] = useState("");
  const [notice, setNotice] = useState("");
  const pending = query.data?.pending || [];
  const audit = query.data?.audit || [];
  const visible = pending.filter((item) => !route || item.route === route);

  const mutation = useMutation({
    mutationFn: ({
      approval,
      decision,
      note: auditNote,
    }: {
      approval: Approval;
      decision: Decision;
      note: string;
    }) =>
      api<DecisionResponse>(`/approvals/${encodeURIComponent(approval.id)}`, {
        method: "POST",
        body: JSON.stringify({ decision, note: auditNote }),
      }),
    onSuccess: (result) => {
      client.setQueryData<ApprovalResponse>(["approvals"], (current) =>
        current
          ? {
              ...current,
              pending: current.pending.filter(
                (item) => item.id !== result.approval.id,
              ),
              audit: [
                ...current.audit.filter(
                  (item) => item.audit_id !== result.approval.audit_id,
                ),
                result.approval,
              ],
            }
          : current,
      );
      setNotice(
        `${result.approval.case_id}: ${actionName(result.approval.action)} ${result.approval.decision === "approve" ? "approved" : "rejected"} in the demo audit. No bank action was executed. ${result.written_to_graph ? "The graph audit was updated." : "The decision is saved locally; graph audit is unavailable."}`,
      );
      dialog.current?.close();
      void client.invalidateQueries({ queryKey: ["approvals"] });
      void client.invalidateQueries({ queryKey: ["cases"] });
      void client.invalidateQueries({ queryKey: ["case"] });
    },
  });

  function open(
    approval: Approval,
    decision: Decision,
    trigger: HTMLButtonElement,
  ) {
    lastTrigger.current = trigger;
    setSelected({ approval, decision });
    setNote("");
    mutation.reset();
    dialog.current?.showModal();
  }
  function submit(event: FormEvent) {
    event.preventDefault();
    if (
      !selected ||
      mutation.isPending ||
      !permission(role, selected.approval.route)
    )
      return;
    mutation.mutate({ ...selected, note: note.trim() });
  }
  const roleDescription =
    role === "viewer"
      ? "Read-only access. Select Analyst · L1 or Supervisor · L2 in the role switcher to record a demo decision."
      : role === "analyst_l1"
        ? "You can decide L1 recommendations. L2 recommendations require the supervisor role."
        : "You can decide L1 and L2 recommendations. Every decision is kept in the audit trail.";

  return (
    <div className="secondary-page approval-page">
      <PageHeading
        title="Approvals"
        description="Review policy-routed recommendations and record a decision."
      >
        <button
          className="button"
          disabled={query.isFetching}
          onClick={() => void query.refetch()}
          aria-busy={query.isFetching}
        >
          <RotateCw size={16} aria-hidden />
          Refresh
        </button>
      </PageHeading>
      <div className="secondary-notice">
        <ShieldCheck size={20} aria-hidden />
        <div>
          <strong>{roleNames[role]}</strong>
          <p>{roleDescription}</p>
        </div>
      </div>
      <p className="secondary-disclosure">
        <Info size={15} aria-hidden />
        {query.data?.disclosure ||
          "Approvals are local demo audit decisions. No card block, customer contact, or report filing is executed. The role switcher is not authentication."}
      </p>
      {notice && (
        <div className="secondary-success" role="status">
          <CheckCheck size={20} aria-hidden />
          <p>{notice}</p>
          <button
            className="icon-button"
            onClick={() => setNotice("")}
            aria-label="Dismiss confirmation"
          >
            <X size={16} />
          </button>
        </div>
      )}
      {query.isPending ? (
        <Skeleton rows={6} />
      ) : query.isError ? (
        <ErrorState error={query.error} retry={() => void query.refetch()} />
      ) : (
        <>
          <div className="secondary-stats" aria-label="Approval counts">
            <div>
              <span>Awaiting decision</span>
              <strong className="mono">{pending.length}</strong>
            </div>
            <div>
              <span>L1 review</span>
              <strong className="mono">
                {pending.filter((item) => item.route === "L1").length}
              </strong>
            </div>
            <div>
              <span>L2 review</span>
              <strong className="mono">
                {pending.filter((item) => item.route === "L2").length}
              </strong>
            </div>
            <div>
              <span>Decisions recorded</span>
              <strong className="mono">{audit.length}</strong>
            </div>
          </div>
          <section
            className="secondary-panel"
            aria-labelledby="pending-approvals-title"
          >
            <div className="secondary-panel-heading">
              <div>
                <h2
                  id="pending-approvals-title"
                  ref={pendingHeading}
                  tabIndex={-1}
                >
                  Pending recommendations
                </h2>
                <p>Review the case evidence before deciding.</p>
              </div>
              <label className="secondary-filter">
                Route
                <select
                  aria-label="Filter approval route"
                  value={route}
                  onChange={(event) => {
                    const next = new URLSearchParams(params);
                    if (event.target.value)
                      next.set("route", event.target.value);
                    else next.delete("route");
                    setParams(next, { replace: true });
                  }}
                >
                  <option value="">All routes</option>
                  <option value="L1">L1 only</option>
                  <option value="L2">L2 only</option>
                </select>
              </label>
            </div>
            {!visible.length ? (
              <EmptyState
                title={
                  pending.length
                    ? "No recommendations on this route"
                    : "No approvals waiting"
                }
              >
                {pending.length ? (
                  "Choose another route to review the remaining recommendations."
                ) : (
                  <>
                    Actions requiring L1 or L2 review will appear here.{" "}
                    <Link to="/">Open the case queue</Link>.
                  </>
                )}
              </EmptyState>
            ) : (
              <ul className="approval-list">
                {visible.map((approval) => {
                  const allowed = permission(role, approval.route);
                  return (
                    <li className="approval-row" key={approval.id}>
                      <div className="approval-case">
                        <Link
                          className="case-link mono"
                          to={`/cases/${encodeURIComponent(approval.case_id)}`}
                        >
                          {approval.case_id}
                        </Link>
                        <RouteBadge route={approval.route} />
                        <span>
                          <Clock3 size={13} aria-hidden />
                          Awaiting review
                        </span>
                      </div>
                      <div className="approval-recommendation">
                        <h3>{actionName(approval.action)}</h3>
                        <p>{approval.reason}</p>
                      </div>
                      <div className="approval-controls">
                        <div>
                          <button
                            className="button"
                            disabled={!allowed || mutation.isPending}
                            onClick={(event) =>
                              open(approval, "reject", event.currentTarget)
                            }
                            aria-label={`Reject ${actionName(approval.action)} for ${approval.case_id}`}
                          >
                            <X size={15} aria-hidden />
                            Reject
                          </button>
                          <button
                            className="button primary"
                            disabled={!allowed || mutation.isPending}
                            onClick={(event) =>
                              open(approval, "approve", event.currentTarget)
                            }
                            aria-label={`Approve ${actionName(approval.action)} for ${approval.case_id}`}
                          >
                            <Check size={15} aria-hidden />
                            Approve
                          </button>
                        </div>
                        {!allowed && (
                          <span className="helper">
                            <LockKeyhole size={13} aria-hidden />
                            {role === "viewer"
                              ? "A review role is required"
                              : "Supervisor · L2 required"}
                          </span>
                        )}
                      </div>
                    </li>
                  );
                })}
              </ul>
            )}
          </section>
          <section
            className="secondary-panel approval-audit"
            aria-labelledby="audit-title"
          >
            <div className="secondary-panel-heading">
              <div>
                <h2 id="audit-title">Decision audit trail</h2>
                <p>
                  Persistent review history. Approval does not execute the
                  recommendation.
                </p>
              </div>
              <span className="secondary-count mono">
                {audit.length} decisions
              </span>
            </div>
            {!audit.length ? (
              <EmptyState title="No decisions recorded">
                Approved and rejected recommendations will appear here with the
                review role, note, and graph audit status.
              </EmptyState>
            ) : (
              <ol className="audit-list">
                {[...audit].reverse().map((entry) => (
                  <li key={entry.audit_id || entry.id}>
                    <div className="audit-heading">
                      <Link
                        className="case-link mono"
                        to={`/cases/${encodeURIComponent(entry.case_id)}`}
                      >
                        {entry.case_id}
                      </Link>
                      <strong>{actionName(entry.action)}</strong>
                      <RouteBadge route={entry.route} />
                      <span
                        className={`chip ${entry.decision === "approve" ? "legitimate" : "fraud"}`}
                      >
                        {entry.decision === "approve" ? "Approved" : "Rejected"}
                      </span>
                      <time dateTime={entry.decided_at || entry.timestamp}>
                        {decisionTime(entry.decided_at || entry.timestamp)}
                      </time>
                    </div>
                    <p className="audit-note">
                      {entry.note || (
                        <span className="muted">No review note provided.</span>
                      )}
                    </p>
                    <div className="audit-meta">
                      <span>
                        {entry.role
                          ? roleNames[entry.role]
                          : "Review role unavailable"}
                      </span>
                      <span>Execution: none</span>
                      <span title={entry.graph?.reason}>
                        {entry.graph?.written_to_graph
                          ? "Graph audit saved"
                          : "Local audit saved"}
                      </span>
                    </div>
                  </li>
                ))}
              </ol>
            )}
          </section>
        </>
      )}
      <dialog
        ref={dialog}
        className="modal approval-dialog"
        aria-labelledby="decision-title"
        aria-describedby="decision-description"
        onCancel={(event) => {
          if (mutation.isPending) event.preventDefault();
        }}
        onClose={() => {
          if (lastTrigger.current?.isConnected) lastTrigger.current.focus();
          else pendingHeading.current?.focus();
        }}
      >
        <form onSubmit={submit}>
          <div className="modal-heading">
            <h2 id="decision-title">
              {selected?.decision === "reject" ? "Reject" : "Approve"}{" "}
              recommendation
            </h2>
            <button
              className="icon-button"
              type="button"
              disabled={mutation.isPending}
              onClick={() => dialog.current?.close()}
              aria-label="Close review dialog"
            >
              <X size={20} />
            </button>
          </div>
          <p id="decision-description" className="muted">
            Record a demo audit decision. This does not execute a bank action or
            file a report.
          </p>
          {selected && (
            <div className="approval-dialog-subject">
              <div>
                <span className="mono">{selected.approval.case_id}</span>
                <RouteBadge route={selected.approval.route} />
              </div>
              <strong>{actionName(selected.approval.action)}</strong>
              <p>{selected.approval.reason}</p>
            </div>
          )}
          <label htmlFor="review-note">
            Review note <span className="muted">(optional)</span>
          </label>
          <textarea
            id="review-note"
            rows={4}
            maxLength={2000}
            value={note}
            onChange={(event) => setNote(event.target.value)}
            disabled={mutation.isPending}
            placeholder="Record the evidence behind your decision."
            aria-describedby="review-note-help"
          />
          <p id="review-note-help" className="helper">
            Saved with your selected role: {roleNames[role]}.
          </p>
          {selected && !permission(role, selected.approval.route) && (
            <p className="form-error" role="alert">
              Your current role cannot decide this recommendation.
            </p>
          )}
          {mutation.isError && (
            <p className="form-error" role="alert">
              {mutation.error.message} Your note is retained.
            </p>
          )}
          <div className="modal-actions">
            <button
              className="button"
              type="button"
              disabled={mutation.isPending}
              onClick={() => dialog.current?.close()}
            >
              Cancel
            </button>
            <button
              className={`button ${selected?.decision === "reject" ? "danger" : "primary"}`}
              type="submit"
              disabled={
                mutation.isPending ||
                !selected ||
                !permission(role, selected.approval.route)
              }
              aria-busy={mutation.isPending}
            >
              {mutation.isPending
                ? "Saving decision…"
                : selected?.decision === "reject"
                  ? "Record rejection"
                  : "Record approval"}
            </button>
          </div>
        </form>
      </dialog>
    </div>
  );
}
