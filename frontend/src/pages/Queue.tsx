import { useMemo, useRef, useState, type FormEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowDown,
  ArrowUp,
  ArrowUpRight,
  Plus,
  Search,
  SlidersHorizontal,
  X,
} from "lucide-react";
import { api } from "../api";
import {
  EmptyState,
  ErrorState,
  PageHeading,
  Skeleton,
  VerdictChip,
  human,
  percent,
} from "../components";
import type { QueueCase } from "../types";
function NewInvestigation() {
  const ref = useRef<HTMLDialogElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const navigate = useNavigate();
  const client = useQueryClient();
  const [type, setType] = useState("risk_signal");
  const [id, setId] = useState("");
  const [text, setText] = useState("");
  const mutation = useMutation({
    mutationFn: (body: object) =>
      api<{ case_id: string; stream_url: string }>("/investigations", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    onSuccess: (result) => {
      ref.current?.close();
      void client.invalidateQueries({ queryKey: ["cases"] });
      navigate(`/cases/${result.case_id}`);
    },
  });
  function submit(event: FormEvent) {
    event.preventDefault();
    mutation.mutate(
      type === "risk_signal"
        ? { type, txn_id: id.trim() }
        : {
            type,
            card_id: id.trim(),
            [type === "customer_report" ? "text" : "note"]: text.trim(),
          },
    );
  }
  return (
    <>
      <button
        className="button primary"
        ref={trigger}
        onClick={() => {
          mutation.reset();
          ref.current?.showModal();
        }}
      >
        <Plus size={17} aria-hidden />
        New investigation
      </button>
      <dialog
        ref={ref}
        className="modal"
        onClose={() => trigger.current?.focus()}
      >
        <form onSubmit={submit}>
          <div className="modal-heading">
            <h2>New investigation</h2>
            <button
              className="icon-button"
              type="button"
              aria-label="Close dialog"
              onClick={() => ref.current?.close()}
            >
              <X size={20} />
            </button>
          </div>
          <p className="muted">
            Create a case from a supplied transaction or card. Starting a live
            run is a separate step.
          </p>
          <label htmlFor="trigger-type">Trigger type</label>
          <select
            id="trigger-type"
            value={type}
            onChange={(e) => {
              setType(e.target.value);
              setId("");
              mutation.reset();
            }}
          >
            <option value="risk_signal">Risk signal</option>
            <option value="customer_report">Customer report</option>
            <option value="analyst_request">Analyst request</option>
          </select>
          <label htmlFor="trigger-id">
            {type === "risk_signal" ? "Transaction ID" : "Card ID"}{" "}
            <span className="muted">(required)</span>
          </label>
          <input
            id="trigger-id"
            autoComplete="off"
            spellCheck={false}
            value={id}
            onChange={(e) => setId(e.target.value)}
            placeholder={type === "risk_signal" ? "3478561" : "C13487-K1"}
            required
            maxLength={80}
          />
          {type !== "risk_signal" && (
            <>
              <label htmlFor="trigger-text">
                {type === "customer_report"
                  ? "Customer message"
                  : "Analyst note"}{" "}
                <span className="muted">(required)</span>
              </label>
              <textarea
                id="trigger-text"
                value={text}
                onChange={(e) => setText(e.target.value)}
                required
                maxLength={3000}
                rows={4}
              />
            </>
          )}
          <p className="helper">
            Use IDs from the prepared dataset. The case is saved separately from
            graded answers.
          </p>
          {mutation.isError && (
            <p className="form-error" role="alert">
              {mutation.error.message}
            </p>
          )}
          <div className="modal-actions">
            <button
              className="button"
              type="button"
              onClick={() => ref.current?.close()}
            >
              Cancel
            </button>
            <button
              className="button primary"
              type="submit"
              disabled={mutation.isPending}
              aria-busy={mutation.isPending}
            >
              {mutation.isPending ? "Creating case…" : "Create investigation"}
            </button>
          </div>
        </form>
      </dialog>
    </>
  );
}
export default function Queue() {
  const query = useQuery({
    queryKey: ["cases"],
    queryFn: () => api<{ cases: QueueCase[] }>("/cases"),
  });
  const [params, setParams] = useSearchParams();
  const [sort, setSort] = useState<{ key: keyof QueueCase; asc: boolean }>({
    key: "opened_at",
    asc: true,
  });
  const change = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    setParams(next, { replace: true });
  };
  const rows = query.data?.cases || [];
  const filtered = useMemo(
    () =>
      rows
        .filter((row) => {
          const search = params.get("q")?.toLowerCase() || "";
          return (
            (!search ||
              `${row.case_id} ${row.pattern} ${row.trigger_type}`
                .toLowerCase()
                .includes(search)) &&
            ["verdict", "status", "pattern", "trigger_type"].every(
              (key) =>
                !params.get(key) ||
                String(row[key as keyof QueueCase]) === params.get(key),
            ) &&
            (!params.get("sar") ||
              Boolean(row.sar ?? row.sar_flag) ===
                (params.get("sar") === "yes"))
          );
        })
        .sort((a, b) => {
          const first = a[sort.key] ?? "";
          const second = b[sort.key] ?? "";
          return (
            (typeof first === "number" && typeof second === "number"
              ? first - second
              : String(first).localeCompare(String(second))) *
            (sort.asc ? 1 : -1)
          );
        }),
    [rows, params, sort],
  );
  const sortButton = (key: keyof QueueCase, label: string) => (
    <button
      className="sort-button"
      onClick={() => setSort({ key, asc: sort.key === key ? !sort.asc : true })}
    >
      {label}
      {sort.key === key ? (
        sort.asc ? (
          <ArrowUp size={12} />
        ) : (
          <ArrowDown size={12} />
        )
      ) : null}
    </button>
  );
  return (
    <>
      <PageHeading
        title="Case queue"
        description="Review the evidence. Follow the policy. Decide what happens next."
      >
        <NewInvestigation />
      </PageHeading>
      <section className="queue-summary" aria-label="Queue overview">
        <div>
          <span>Investigations</span>
          <strong>{rows.length || "—"}</strong>
        </div>
        <div>
          <span className="fraud-text">Fraud</span>
          <strong>{rows.filter((x) => x.verdict === "fraud").length}</strong>
        </div>
        <div>
          <span className="legitimate-text">Legitimate</span>
          <strong>
            {rows.filter((x) => x.verdict === "legitimate").length}
          </strong>
        </div>
        <div>
          <span className="uncertain-text">Uncertain</span>
          <strong>
            {rows.filter((x) => x.verdict === "uncertain").length}
          </strong>
        </div>
        <div>
          <span>SAR recommended</span>
          <strong>{rows.filter((x) => x.sar ?? x.sar_flag).length}</strong>
        </div>
        <div>
          <span>Pending approvals</span>
          <strong>
            {rows.reduce((n, x) => n + (x.pending_approval_count || 0), 0)}
          </strong>
        </div>
      </section>
      <section className="queue-panel">
        <div className="queue-toolbar">
          <div className="search-field">
            <Search size={16} aria-hidden />
            <input
              aria-label="Search cases"
              type="search"
              placeholder="Search case or pattern…"
              value={params.get("q") || ""}
              onChange={(e) => change("q", e.target.value)}
            />
          </div>
          <div className="filter-label">
            <SlidersHorizontal size={15} aria-hidden />
            Filters
          </div>
          {(["verdict", "status", "pattern", "trigger_type"] as const).map(
            (key) => (
              <select
                key={key}
                aria-label={`Filter ${human(key)}`}
                value={params.get(key) || ""}
                onChange={(e) => change(key, e.target.value)}
              >
                <option value="">
                  All{" "}
                  {key === "trigger_type"
                    ? "triggers"
                    : key === "status"
                      ? "statuses"
                      : `${key}s`}
                </option>
                {[
                  ...new Set(
                    rows.map((row) => row[key]).filter((v): v is string => !!v),
                  ),
                ]
                  .sort()
                  .map((value) => (
                    <option key={value} value={value}>
                      {human(value)}
                    </option>
                  ))}
              </select>
            ),
          )}
          <select
            aria-label="Filter SAR"
            value={params.get("sar") || ""}
            onChange={(e) => change("sar", e.target.value)}
          >
            <option value="">Any SAR</option>
            <option value="yes">SAR recommended</option>
            <option value="no">No SAR</option>
          </select>
          {params.size > 0 && (
            <button className="button subtle" onClick={() => setParams({})}>
              Reset
            </button>
          )}
        </div>
        {query.isPending ? (
          <Skeleton rows={8} />
        ) : query.isError ? (
          <ErrorState error={query.error} retry={() => void query.refetch()} />
        ) : !filtered.length ? (
          <EmptyState title="No cases match these filters">
            Clear a filter or search by case ID.
          </EmptyState>
        ) : (
          <div className="table-scroll">
            <table className="case-table">
              <thead>
                <tr>
                  <th
                    aria-sort={
                      sort.key === "case_id"
                        ? sort.asc
                          ? "ascending"
                          : "descending"
                        : "none"
                    }
                  >
                    {sortButton("case_id", "Case")}
                  </th>
                  <th>Trigger</th>
                  <th
                    aria-sort={
                      sort.key === "opened_at"
                        ? sort.asc
                          ? "ascending"
                          : "descending"
                        : "none"
                    }
                  >
                    {sortButton("opened_at", "Opened (UTC)")}
                  </th>
                  <th>Verdict</th>
                  <th
                    aria-sort={
                      sort.key === "probability"
                        ? sort.asc
                          ? "ascending"
                          : "descending"
                        : "none"
                    }
                  >
                    {sortButton("probability", "Probability")}
                  </th>
                  <th>Pattern</th>
                  <th>Status</th>
                  <th>SAR</th>
                  <th>Approvals</th>
                  <th>
                    <span className="sr-only">Open case</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((row) => (
                  <tr key={row.case_id}>
                    <td>
                      <Link
                        className="case-link mono"
                        to={`/cases/${row.case_id}`}
                      >
                        {row.case_id}
                      </Link>
                    </td>
                    <td>
                      {human(
                        row.trigger_type === "risk_score"
                          ? "risk_signal"
                          : row.trigger_type,
                      )}
                    </td>
                    <td className="date-cell">{row.opened_at?.slice(0, 16)}</td>
                    <td>
                      <VerdictChip verdict={row.verdict} />
                    </td>
                    <td>
                      <div className="table-probability">
                        <span className="mono">{percent(row.probability)}</span>
                        <span className="mini-track">
                          <span
                            className={row.verdict || ""}
                            style={{
                              width: `${(row.probability || 0) * 100}%`,
                            }}
                          />
                        </span>
                      </div>
                    </td>
                    <td className="pattern-cell">{human(row.pattern)}</td>
                    <td>
                      <span className="status-text">{human(row.status)}</span>
                    </td>
                    <td>
                      {(row.sar ?? row.sar_flag) ? (
                        <span className="sar-chip">L2 review</span>
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </td>
                    <td>
                      <span className="status-text">
                        {row.pending_approval_count > 0
                          ? `${row.pending_approval_count} pending`
                          : "—"}
                      </span>
                    </td>
                    <td>
                      <Link
                        className="icon-button row-open"
                        to={`/cases/${row.case_id}`}
                        aria-label={`Open ${row.case_id}`}
                      >
                        <ArrowUpRight size={16} />
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <div className="table-footer">
          <span>
            {filtered.length} of {rows.length} investigations
          </span>
          <span>
            Probability is an evidence heuristic, not a calibrated model score.
          </span>
        </div>
      </section>
    </>
  );
}
