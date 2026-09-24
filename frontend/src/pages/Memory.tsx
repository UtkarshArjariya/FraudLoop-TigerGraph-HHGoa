import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Database, Info, MapPin, RotateCw, Smartphone } from "lucide-react";
import { api } from "../api";
import {
  EmptyState,
  ErrorState,
  PageHeading,
  Skeleton,
  human,
} from "../components";
import type { MemoryStats, VectorIndex } from "../types";
import "./secondary.css";

interface CheckpointIndex extends VectorIndex {
  closed_case_vectors?: number;
  document_vectors?: number;
  device?: string;
}
interface MemoryResponse extends MemoryStats {
  saved_cases?: number;
  vector_indexes: CheckpointIndex[];
  disclosure?: string;
}
interface SharedOrigin {
  id?: string;
  device?: string;
  device_profile?: string;
  region?: string;
  case_ids?: string[];
  count?: number;
}
const number = (value: number) => value.toLocaleString();
const patternName = (value: string) =>
  value === "none"
    ? "No established pattern"
    : human(value).replace(/^./, (initial) => initial.toUpperCase());
function indexName(index: VectorIndex) {
  if (index.model.includes("preview")) return "Gemini Embedding 2 Preview";
  return (
    (
      {
        gem001: "Gemini Embedding 001",
        gem2: "Gemini Embedding 2",
        gemma: "EmbeddingGemma",
        bge: "BGE base",
      } as Record<string, string>
    )[index.id] || human(index.id)
  );
}

function RecurringOrigins({
  kind,
  groups,
}: {
  kind: "device" | "region";
  groups: SharedOrigin[];
}) {
  const isDevice = kind === "device";
  const title = isDevice
    ? "Recurring device profiles"
    : "Recurring billing regions";
  const titleId = `recurring-${kind}-title`;
  return (
    <section
      className="secondary-panel memory-origins"
      aria-labelledby={titleId}
    >
      <div className="secondary-panel-heading">
        <div>
          <h2 id={titleId}>
            {isDevice ? (
              <Smartphone size={17} aria-hidden />
            ) : (
              <MapPin size={17} aria-hidden />
            )}
            {title}
          </h2>
          <p>
            {isDevice
              ? "Flagged or connected profiles shared across saved cases."
              : "Billing-region codes of flagged transactions across saved cases."}
          </p>
        </div>
        <span className="secondary-count mono">{number(groups.length)}</span>
      </div>
      {!groups.length ? (
        <EmptyState
          title={
            isDevice
              ? "No recurring device profiles"
              : "No recurring billing regions"
          }
        >
          No matching {isDevice ? "profile" : "billing-region code"} appears in
          multiple saved cases.
        </EmptyState>
      ) : (
        <ul className="memory-origin-list">
          {groups.map((group, position) => {
            const id =
              group.id ||
              group.device_profile ||
              group.device ||
              group.region ||
              "Identifier unavailable";
            const cases = group.case_ids || [];
            return (
              <li key={`${id}-${position}`}>
                <div className="origin-heading">
                  <code>{isDevice ? id : `Region ${id}`}</code>
                  <span>{number(group.count ?? cases.length)} cases</span>
                </div>
                <div
                  className="origin-case-links"
                  aria-label={`Cases sharing ${isDevice ? "this device profile" : `region ${id}`}`}
                >
                  {cases.map((caseId) => (
                    <Link
                      key={caseId}
                      className="mono"
                      to={`/cases/${encodeURIComponent(caseId)}`}
                    >
                      {caseId}
                    </Link>
                  ))}
                  {!cases.length && (
                    <span className="helper">Case references unavailable.</span>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}
      <p className="memory-panel-footnote">
        {isDevice
          ? "A shared coarse profile is context, not evidence of coordinated fraud on its own."
          : "Region codes are billing context, not precise physical locations or proof of fraud."}
      </p>
    </section>
  );
}

export default function Memory() {
  const query = useQuery({
    queryKey: ["memory"],
    queryFn: () => api<MemoryResponse>("/memory/stats"),
    staleTime: 30000,
  });
  const data = query.data;
  const indexes = data?.vector_indexes || [];
  const patterns = Object.entries(data?.pattern_counts || {}).sort(
    ([firstName, firstCount], [secondName, secondCount]) =>
      secondCount - firstCount || firstName.localeCompare(secondName),
  );
  const patternTotal = patterns.reduce((sum, [, count]) => sum + count, 0);
  const savedCases = data?.saved_cases ?? patternTotal;
  const populated = indexes.filter((index) => index.count > 0).length;

  return (
    <div className="secondary-page memory-page">
      <PageHeading
        title="Case memory"
        description="Inspect precedent coverage and context recurring across saved investigations."
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
      {query.isPending ? (
        <Skeleton rows={8} />
      ) : query.isError ? (
        <ErrorState error={query.error} retry={() => void query.refetch()} />
      ) : (
        data && (
          <>
            <div className="secondary-stats" aria-label="Memory summary">
              <div>
                <span>Historical closed cases</span>
                <strong className="mono">{number(data.closed_cases)}</strong>
              </div>
              <div>
                <span>Saved investigations</span>
                <strong className="mono">{number(savedCases)}</strong>
              </div>
              <div>
                <span>Indexes with checkpoints</span>
                <strong className="mono">
                  {populated}
                  <small> / {indexes.length}</small>
                </strong>
              </div>
              <div>
                <span>Recurring context groups</span>
                <strong className="mono">
                  {number(
                    data.recurring_devices.length +
                      data.recurring_regions.length,
                  )}
                </strong>
              </div>
            </div>
            <div className="secondary-notice">
              <Database size={20} aria-hidden />
              <div>
                <strong>
                  Bank history and investigation memory have different
                  provenance
                </strong>
                <p>
                  {data.disclosure ||
                    "Historical cases contain bank outcomes. Saved investigations may include simulated replies. Shared devices and billing regions provide context, not proof of fraud."}
                </p>
              </div>
            </div>
            <div className="memory-grid">
              <section
                className="secondary-panel"
                aria-labelledby="vector-coverage-title"
              >
                <div className="secondary-panel-heading">
                  <div>
                    <h2 id="vector-coverage-title">Vector index coverage</h2>
                    <p>
                      Historical cases and policy documents, counted separately
                      for each model.
                    </p>
                  </div>
                  <span className="secondary-count mono">
                    {indexes.length} indexes
                  </span>
                </div>
                <div className="memory-coverage-note">
                  <Info size={16} aria-hidden />
                  <p>
                    These counts come from durable upload checkpoints. They are
                    not a live graph inventory. Coverage overlaps between
                    indexes; empty indexes can still use graph and text
                    retrieval.
                  </p>
                </div>
                {!indexes.length ? (
                  <EmptyState title="No index manifest available">
                    Saved evidence remains available. Index coverage appears
                    when an embedding manifest and checkpoints are present.
                  </EmptyState>
                ) : (
                  <ul className="memory-index-list">
                    {indexes.map((index) => {
                      const total =
                        typeof index.total === "number" && index.total > 0
                          ? index.total
                          : null;
                      const coverage = total
                        ? Math.min(
                            100,
                            Math.max(0, (index.count / total) * 100),
                          )
                        : null;
                      return (
                        <li key={index.id}>
                          <div className="memory-index-heading">
                            <h3>{indexName(index)}</h3>
                            <span className="chip pending">
                              Saved: {human(index.state)}
                            </span>
                          </div>
                          <p className="memory-model mono">
                            {index.model || "Model not recorded"}
                          </p>
                          <div className="memory-coverage-label">
                            <span>
                              <strong className="mono">
                                {number(index.count)}
                              </strong>{" "}
                              checkpoint IDs{total ? ` / ${number(total)}` : ""}
                            </span>
                            <span className="mono">
                              {coverage === null
                                ? "Total unavailable"
                                : `${coverage.toFixed(1)}%`}
                            </span>
                          </div>
                          <div
                            className="memory-track"
                            role={coverage === null ? undefined : "progressbar"}
                            aria-label={`${indexName(index)} checkpoint coverage`}
                            aria-valuemin={coverage === null ? undefined : 0}
                            aria-valuemax={coverage === null ? undefined : 100}
                            aria-valuenow={coverage ?? undefined}
                            aria-valuetext={
                              total
                                ? `${number(index.count)} of ${number(total)} historical case and policy document IDs`
                                : undefined
                            }
                          >
                            <span style={{ width: `${coverage ?? 0}%` }} />
                          </div>
                          <div className="memory-index-meta">
                            {index.closed_case_vectors !== undefined && (
                              <span>
                                {number(index.closed_case_vectors)} closed cases
                              </span>
                            )}
                            {index.document_vectors !== undefined && (
                              <span>
                                {number(index.document_vectors)} documents
                              </span>
                            )}
                            <span className="mono">
                              {index.attribute || "Attribute unavailable"}
                            </span>
                          </div>
                          <span className="sr-only">
                            Source: {index.coverage_source}
                          </span>
                        </li>
                      );
                    })}
                  </ul>
                )}
              </section>
              <section
                className="secondary-panel"
                aria-labelledby="pattern-distribution-title"
              >
                <div className="secondary-panel-heading">
                  <div>
                    <h2 id="pattern-distribution-title">
                      Pattern distribution
                    </h2>
                    <p>
                      Assignments in the {number(savedCases)} saved
                      investigations.
                    </p>
                  </div>
                </div>
                {!patterns.length || !patternTotal ? (
                  <EmptyState title="No saved patterns">
                    Pattern counts appear once investigation answers are saved.
                  </EmptyState>
                ) : (
                  <div className="memory-patterns">
                    {patterns.map(([pattern, count]) => (
                      <div className="memory-pattern" key={pattern}>
                        <div>
                          <span>{patternName(pattern)}</span>
                          <strong className="mono">
                            {number(count)}
                            <small>
                              {((count / patternTotal) * 100).toFixed(0)}%
                            </small>
                          </strong>
                        </div>
                        <div
                          className={`memory-track ${pattern === "none" ? "muted-track" : ""}`}
                          aria-hidden="true"
                        >
                          <span
                            style={{
                              width: `${(count / patternTotal) * 100}%`,
                            }}
                          />
                        </div>
                      </div>
                    ))}
                  </div>
                )}
                <p className="memory-panel-footnote">
                  These are saved agent assessments, which can include simulated
                  verification replies. They are separate from historical
                  confirmed bank outcomes.
                </p>
                <Link to="/" className="memory-queue-link">
                  Review the saved case queue <span aria-hidden>→</span>
                </Link>
              </section>
            </div>
            <div className="memory-origin-grid">
              <RecurringOrigins kind="device" groups={data.recurring_devices} />
              <RecurringOrigins kind="region" groups={data.recurring_regions} />
            </div>
          </>
        )
      )}
    </div>
  );
}
