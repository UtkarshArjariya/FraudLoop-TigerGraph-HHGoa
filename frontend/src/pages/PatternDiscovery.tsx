import { useState, type FormEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { ArrowUpRight, GitBranch, Search } from "lucide-react";
import { api } from "../api";
import { EmptyState, human } from "../components";
import "./patterns.css";

interface PatternComponent {
  component_id: string;
  card_ids: string[];
  customer_ids: string[];
  device_profiles: string[];
  transaction_ids: string[];
  anchor_txn_id: string;
  anchor_card_id: string;
  size: number;
  node_count: number;
  card_count: number;
  customer_count: number;
  pattern_status: string;
  component_source: string;
  trigger_payload: {
    type: string;
    card_id: string;
    note: string;
    mode_evidence: string;
  };
  open_payload?: {
    component_id: string;
    window_days: number;
    mode_evidence?: "simulated" | "human";
  };
}
interface DiscoveryResult {
  components: PatternComponent[];
  native_wcc: Record<string, unknown> | null;
  component_source: string;
  as_of: string;
  window_days: number;
  warnings: string[];
  disclosure: string;
}
export default function PatternDiscovery() {
  const [windowDays, setWindowDays] = useState(30);
  const navigate = useNavigate();
  const client = useQueryClient();
  const discover = useMutation({
    mutationFn: () =>
      api<DiscoveryResult>("/patterns/discover", {
        method: "POST",
        body: JSON.stringify({ window_days: windowDays }),
      }),
  });
  const open = useMutation({
    mutationFn: (component: PatternComponent) =>
      api<{ case_id: string }>("/patterns/open", {
        method: "POST",
        body: JSON.stringify(
          component.open_payload || {
            component_id: component.component_id,
            window_days: discover.data?.window_days ?? windowDays,
            mode_evidence: "simulated",
          },
        ),
      }),
    onSuccess: (result) => {
      void client.invalidateQueries({ queryKey: ["cases"] });
      navigate(`/cases/${encodeURIComponent(result.case_id)}`);
    },
  });
  function submit(event: FormEvent) {
    event.preventDefault();
    open.reset();
    discover.mutate();
  }
  const data = discover.data;
  return (
    <section
      className="secondary-panel pattern-discovery"
      aria-labelledby="pattern-discovery-title"
    >
      <div className="secondary-panel-heading">
        <div>
          <h2 id="pattern-discovery-title">
            <GitBranch size={18} aria-hidden />
            Pattern discovery
          </h2>
          <p>
            Inspect connected card-device groups with mixed customers and no
            documented pattern.
          </p>
        </div>
      </div>
      <form className="pattern-discovery-form" onSubmit={submit}>
        <label htmlFor="pattern-window">
          History window <span className="muted">days</span>
          <input
            id="pattern-window"
            type="number"
            required
            min={1}
            max={183}
            value={windowDays}
            disabled={discover.isPending}
            onChange={(event) => setWindowDays(Number(event.target.value))}
          />
        </label>
        <button
          className="button"
          type="submit"
          disabled={discover.isPending}
          aria-busy={discover.isPending}
        >
          <Search size={14} aria-hidden />
          {discover.isPending ? "Discovering…" : "Discover groups"}
        </button>
        <p className="helper">
          Review candidates with at least three card/device vertices and two
          customers. Shared profiles alone do not establish coordinated fraud.
        </p>
      </form>
      {discover.isError && (
        <p className="form-error pattern-error" role="alert">
          {discover.error.message}
        </p>
      )}
      {open.isError && (
        <p className="form-error pattern-error" role="alert">
          {open.error.message}
        </p>
      )}
      {data && (
        <>
          <div className="pattern-result-meta">
            <span>{data.components.length} candidate groups</span>
            <span>{data.window_days}-day window</span>
            <span>As of {data.as_of}</span>
            <span className="pattern-provenance">
              {human(data.component_source)}
            </span>
          </div>
          {data.disclosure && (
            <p className="pattern-disclosure">{data.disclosure}</p>
          )}
          {!!data.warnings?.length && (
            <div className="pattern-warnings" role="status">
              {data.warnings.map((warning, i) => (
                <p key={i}>{warning}</p>
              ))}
            </div>
          )}
          {data.components.length ? (
            <div className="pattern-components">
              {data.components.map((component) => (
                <article
                  className="pattern-component"
                  key={component.component_id}
                >
                  <div className="pattern-component-heading">
                    <div>
                      <h3 className="mono">{component.component_id}</h3>
                      <p>
                        {component.card_count} cards ·{" "}
                        {component.customer_count} customers ·{" "}
                        {component.node_count} vertices
                      </p>
                    </div>
                    <button
                      className="button"
                      type="button"
                      disabled={
                        open.isPending || !component.trigger_payload?.card_id
                      }
                      aria-busy={
                        open.isPending &&
                        open.variables?.component_id === component.component_id
                      }
                      onClick={() => open.mutate(component)}
                    >
                      {open.isPending &&
                      open.variables?.component_id === component.component_id
                        ? "Opening…"
                        : "Open R9 review"}
                      <ArrowUpRight size={14} aria-hidden />
                    </button>
                  </div>
                  <p className="pattern-anchor">
                    Review anchor:{" "}
                    <span className="mono">{component.anchor_card_id}</span> ·
                    transaction{" "}
                    <span className="mono">{component.anchor_txn_id}</span>
                  </p>
                  <div
                    className="pattern-cards"
                    aria-label="Cards in candidate group"
                  >
                    {component.card_ids.slice(0, 10).map((card) => (
                      <span className="mono" key={card}>
                        {card}
                      </span>
                    ))}
                    {component.card_ids.length > 10 && (
                      <span>+{component.card_ids.length - 10} more</span>
                    )}
                  </div>
                  <details className="pattern-entities">
                    <summary>Inspect member IDs and provenance</summary>
                    <dl>
                      <dt>Source</dt>
                      <dd>{human(component.component_source)}</dd>
                      <dt>Pattern status</dt>
                      <dd>{human(component.pattern_status)}</dd>
                      <dt>Cards</dt>
                      <dd className="mono">{component.card_ids.join(", ")}</dd>
                      <dt>Customers</dt>
                      <dd className="mono">
                        {component.customer_ids.join(", ")}
                      </dd>
                      <dt>Device profiles</dt>
                      <dd>
                        {component.device_profiles.join("; ") ||
                          "No device profile returned"}
                      </dd>
                      <dt>Transactions</dt>
                      <dd className="mono">
                        {component.transaction_ids.join(", ")}
                      </dd>
                    </dl>
                  </details>
                  <p className="helper">
                    R9 is a review hypothesis. The investigation determines the
                    pattern, verdict, and actions from case evidence.
                  </p>
                </article>
              ))}
            </div>
          ) : (
            <EmptyState title="No candidate groups in this window">
              Try a different history window. No documented pattern or verdict
              is inferred from an empty result.
            </EmptyState>
          )}
          <details className="pattern-native">
            <summary>Native WCC query evidence</summary>
            {data.native_wcc ? (
              <pre>{JSON.stringify(data.native_wcc, null, 2)}</pre>
            ) : (
              <p className="helper">
                No native WCC result is available for this discovery. Candidate
                provenance is shown above.
              </p>
            )}
          </details>
        </>
      )}
    </section>
  );
}
