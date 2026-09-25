import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
} from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Check,
  Copy,
  FileText,
  GitBranch,
  Play,
  RotateCcw,
  Save,
  SlidersHorizontal,
} from "lucide-react";
import { api } from "../api";
import { useRole } from "../App";
import {
  EmptyState,
  ErrorState,
  PageHeading,
  Skeleton,
  human,
} from "../components";
import type { QueueCase } from "../types";
import "./secondary.css";
import "./rag.css";

type IndexId = "bge" | "gemma" | "gem001" | "gem2";
interface RagConfig {
  enabled_indexes: Record<IndexId, boolean>;
  rrf_weights: Record<IndexId, number>;
  rrf_k: number;
  top_k_precedents: number;
  top_k_policy_chunks: number;
  graph_hop_depth: number;
  time_window_days: number;
  min_similarity: number;
  graph_similarity_weight: number;
}
interface ConfigResponse {
  config: RagConfig;
  defaults: RagConfig;
}
interface Contribution {
  source: string;
  rank: number;
  weight: number;
  contribution: number;
  similarity?: number;
  paths?: string[];
}
interface RagResult {
  case_id?: string;
  doc_id?: string;
  source_id?: string;
  id?: string;
  title?: string;
  outcome?: string;
  pattern?: string;
  analyst_notes?: string;
  explanation?: string;
  text?: string;
  text_data?: string;
  match_label?: string;
  source_kind?: string;
  notes_truncated?: boolean;
  text_truncated?: boolean;
  fused_score: number;
  provenance: Contribution[];
}
interface PreviewResponse {
  config: RagConfig;
  precedents: RagResult[];
  policy_chunks: RagResult[];
  graph_paths: string[];
  evidence_brief: Record<string, unknown>;
  llm_context: string;
  system_instruction?: string;
  estimated_tokens: number;
  stage: string;
  warnings: string[];
  backend: string;
  no_llm: boolean;
  written_to_graph: boolean;
}
const indexes: { id: IndexId; label: string; detail: string }[] = [
  { id: "bge", label: "BGE", detail: "Local text index" },
  { id: "gemma", label: "EmbeddingGemma", detail: "Local retrieval index" },
  { id: "gem001", label: "Gemini 001", detail: "Gemini embedding index" },
  { id: "gem2", label: "Gemini 2", detail: "Gemini embedding index" },
];
type NumericKey = Exclude<keyof RagConfig, "enabled_indexes" | "rrf_weights">;
const fields: {
  key: NumericKey;
  label: string;
  min: number;
  max: number;
  step: number;
  help: string;
}[] = [
  {
    key: "rrf_k",
    label: "Rank fusion constant",
    min: 1,
    max: 200,
    step: 1,
    help: "Lower values give more influence to the highest ranks.",
  },
  {
    key: "top_k_precedents",
    label: "Precedents to retrieve",
    min: 1,
    max: 20,
    step: 1,
    help: "Maximum historical or earlier agent cases in context.",
  },
  {
    key: "top_k_policy_chunks",
    label: "Policy chunks to retrieve",
    min: 1,
    max: 30,
    step: 1,
    help: "Maximum policy passages returned by retrieval.",
  },
  {
    key: "graph_hop_depth",
    label: "Graph hop depth",
    min: 0,
    max: 4,
    step: 1,
    help: "From the card: shared card 1 hop, device 3, region 4. Zero disables relationship contributions.",
  },
  {
    key: "time_window_days",
    label: "History window · days",
    min: 1,
    max: 3650,
    step: 1,
    help: "Look back from the case opening time.",
  },
  {
    key: "min_similarity",
    label: "Minimum vector similarity",
    min: -1,
    max: 1,
    step: 0.05,
    help: "Cosine cutoff for vector candidates. Graph, pattern, and text matches remain eligible.",
  },
  {
    key: "graph_similarity_weight",
    label: "Graph relationship weight",
    min: 0,
    max: 5,
    step: 0.1,
    help: "Contribution from shared entities and graph paths.",
  },
];
const tabs = [
  { id: "precedents", label: "Precedents" },
  { id: "policy", label: "Policy chunks" },
  { id: "paths", label: "Graph paths" },
  { id: "context", label: "LLM context" },
] as const;
type Tab = (typeof tabs)[number]["id"];
const score = (value: number | undefined) =>
  typeof value === "number" ? value.toFixed(5) : "—";
const sourceLabel = (source: string) =>
  source.replace(/^vector[:/]/, "").replaceAll("_", " ");

function ContributionBars({ items }: { items: Contribution[] }) {
  const maximum = Math.max(
    ...items.map((item) => item.contribution || 0),
    0.000001,
  );
  if (!items.length)
    return <p className="helper">No ranking contribution was recorded.</p>;
  return (
    <div
      className="rag-contributions"
      aria-label="Retrieval source contributions"
    >
      {items.map((item, i) => (
        <div
          key={`${item.source}-${i}`}
          className="rag-contribution"
          aria-label={`${sourceLabel(item.source)}, rank ${item.rank}, weight ${item.weight}, contribution ${score(item.contribution)}`}
        >
          <span className="rag-source-name" title={item.source}>
            {sourceLabel(item.source)}
          </span>
          <div className="rag-contribution-track" aria-hidden>
            <span
              style={{
                width: `${Math.max(0, (item.contribution || 0) / maximum) * 100}%`,
              }}
            />
          </div>
          <span className="mono">
            #{item.rank}{" "}
            <span className="muted">{score(item.contribution)}</span>
          </span>
        </div>
      ))}
      <p className="helper">
        Bars show each source’s contribution to the fused score. Ranks are
        within each source.
      </p>
    </div>
  );
}

function ResultList({
  results,
  kind,
}: {
  results: RagResult[];
  kind: "precedent" | "policy";
}) {
  if (!results.length)
    return (
      <EmptyState
        title={`No ${kind === "precedent" ? "precedents" : "policy chunks"} returned`}
      >
        Try another query or adjust the enabled indexes, history window, and
        similarity threshold.
      </EmptyState>
    );
  return (
    <div className="rag-result-list">
      {results.map((result, i) => {
        const id =
          result.case_id ||
          result.doc_id ||
          result.source_id ||
          result.id ||
          `Result ${i + 1}`;
        return (
          <article className="rag-result" key={`${id}-${i}`}>
            <div className="rag-result-heading">
              <div>
                <strong className="mono">{id}</strong>
                {result.outcome && (
                  <span
                    className={`chip ${result.outcome === "confirmed_fraud" ? "fraud" : result.outcome === "cleared" ? "legitimate" : "pending"}`}
                  >
                    {human(result.outcome)}
                  </span>
                )}
              </div>
              <span className="rag-result-score">
                Fused score{" "}
                <strong className="mono">{score(result.fused_score)}</strong>
              </span>
            </div>
            {result.title && <h3>{result.title}</h3>}
            {result.pattern && (
              <p className="helper">{human(result.pattern)}</p>
            )}
            {result.source_kind === "agent_conclusion" && (
              <p className="helper">
                Earlier agent conclusion · not a confirmed bank outcome
              </p>
            )}
            {kind === "precedent" && (
              <p className="rag-result-prose">
                {result.explanation ||
                  result.analyst_notes ||
                  "No historical analyst narrative is available."}
              </p>
            )}
            {kind === "policy" && (
              <details className="rag-policy-text" open={results.length <= 3}>
                <summary>Read policy text</summary>
                <p>
                  {result.text_data ||
                    result.text ||
                    "No passage text returned."}
                </p>
                {result.text_truncated && (
                  <p className="helper">
                    Excerpt shown; the passage is shortened for narration
                    context.
                  </p>
                )}
              </details>
            )}
            {result.match_label && (
              <p className="helper">{result.match_label}</p>
            )}
            <ContributionBars items={result.provenance || []} />
            {(result.provenance || []).some((item) => item.paths?.length) && (
              <details className="rag-result-paths">
                <summary>Graph-path provenance</summary>
                <ul>
                  {[
                    ...new Set(
                      result.provenance.flatMap((item) => item.paths || []),
                    ),
                  ].map((path) => (
                    <li key={path}>{path}</li>
                  ))}
                </ul>
              </details>
            )}
          </article>
        );
      })}
    </div>
  );
}

export default function GraphRAG() {
  const { role } = useRole();
  const canSave = role === "supervisor_l2";
  const client = useQueryClient();
  const query = useQuery({
    queryKey: ["rag-config"],
    queryFn: () => api<ConfigResponse>("/rag/config"),
  });
  const cases = useQuery({
    queryKey: ["cases"],
    queryFn: () => api<{ cases: QueueCase[] }>("/cases"),
  });
  const [draft, setDraft] = useState<RagConfig | null>(null);
  const [queryMode, setQueryMode] = useState<"case" | "text">("case");
  const [caseId, setCaseId] = useState("");
  const [queryText, setQueryText] = useState("");
  const [tab, setTab] = useState<Tab>("precedents");
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState("");
  const tabRefs = useRef<(HTMLButtonElement | null)[]>([]);
  useEffect(() => {
    if (query.data) setDraft(query.data.config);
  }, [query.data]);
  useEffect(() => {
    if (!caseId && cases.data?.cases.length)
      setCaseId(cases.data.cases[0].case_id);
  }, [caseId, cases.data]);
  const save = useMutation({
    mutationFn: (config: RagConfig) =>
      api<ConfigResponse>("/rag/config", {
        method: "PUT",
        body: JSON.stringify(config),
      }),
    onSuccess: (result) => {
      client.setQueryData(["rag-config"], result);
      setDraft(result.config);
    },
  });
  const preview = useMutation({
    mutationFn: (body: { case_id: string } | { query_text: string }) =>
      api<PreviewResponse>("/rag/preview", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    onSuccess: () => {
      setCopied(false);
      setCopyError("");
    },
  });
  const dirty = Boolean(
    draft &&
    query.data &&
    JSON.stringify(draft) !== JSON.stringify(query.data.config),
  );
  const stalePreview = Boolean(
    preview.data &&
    query.data &&
    JSON.stringify(preview.data.config) !== JSON.stringify(query.data.config),
  );
  function update<K extends keyof RagConfig>(key: K, value: RagConfig[K]) {
    setDraft((current) => current && { ...current, [key]: value });
    save.reset();
  }
  function submit(event: FormEvent) {
    event.preventDefault();
    if (draft && canSave) save.mutate(draft);
  }
  function runPreview(event: FormEvent) {
    event.preventDefault();
    preview.mutate(
      queryMode === "case"
        ? { case_id: caseId }
        : { query_text: queryText.trim() },
    );
  }
  function moveTab(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    let next = index;
    if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
    else if (event.key === "ArrowLeft")
      next = (index + tabs.length - 1) % tabs.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = tabs.length - 1;
    else return;
    event.preventDefault();
    setTab(tabs[next].id);
    tabRefs.current[next]?.focus();
  }
  async function copyContext() {
    try {
      await navigator.clipboard.writeText(
        preview.data?.llm_context ||
          JSON.stringify(preview.data?.evidence_brief || {}, null, 2),
      );
      setCopied(true);
      setCopyError("");
    } catch {
      setCopyError(
        "Clipboard is unavailable. Select and copy the context below.",
      );
    }
  }
  const data = preview.data;
  return (
    <div className="secondary-page rag-page">
      <PageHeading
        title="GraphRAG"
        description="Tune retrieval, inspect its sources, and review the context prepared for narration."
      />
      {query.isPending ? (
        <Skeleton rows={10} />
      ) : query.isError ? (
        <ErrorState error={query.error} retry={() => void query.refetch()} />
      ) : (
        draft && (
          <div className="rag-layout">
            <section
              className="secondary-panel rag-settings"
              aria-labelledby="rag-settings-title"
            >
              <div className="secondary-panel-heading">
                <div>
                  <h2 id="rag-settings-title">
                    <SlidersHorizontal size={17} aria-hidden />
                    Retrieval settings
                  </h2>
                  <p>Saved settings apply to subsequent investigations.</p>
                </div>
              </div>
              <form onSubmit={submit}>
                <fieldset
                  className="rag-index-fields"
                  disabled={!canSave || save.isPending}
                >
                  <legend>Embedding indexes &amp; weights</legend>
                  {indexes.map((index) => (
                    <div className="rag-index-control" key={index.id}>
                      <label className="rag-index-toggle">
                        <input
                          type="checkbox"
                          checked={draft.enabled_indexes[index.id]}
                          onChange={(event) =>
                            update("enabled_indexes", {
                              ...draft.enabled_indexes,
                              [index.id]: event.target.checked,
                            })
                          }
                        />
                        <span>
                          <strong>{index.label}</strong>
                          <small>{index.detail}</small>
                        </span>
                      </label>
                      <label className="rag-weight">
                        <span className="sr-only">{index.label} weight</span>
                        <input
                          type="number"
                          min={0}
                          max={5}
                          step={0.1}
                          required
                          value={draft.rrf_weights[index.id]}
                          disabled={!draft.enabled_indexes[index.id]}
                          onChange={(event) =>
                            update("rrf_weights", {
                              ...draft.rrf_weights,
                              [index.id]: Number(event.target.value),
                            })
                          }
                        />
                        <span>weight</span>
                      </label>
                    </div>
                  ))}
                  <p className="helper">
                    Enabling an index uses its available coverage. It does not
                    create embeddings.
                  </p>
                </fieldset>
                <fieldset
                  className="rag-range-fields"
                  disabled={!canSave || save.isPending}
                >
                  <legend>Ranking &amp; scope</legend>
                  {fields.map((field) => (
                    <div className="rag-range-field" key={field.key}>
                      <label htmlFor={`rag-${field.key}`}>{field.label}</label>
                      <div>
                        <input
                          id={`rag-${field.key}`}
                          type="range"
                          min={field.min}
                          max={field.max}
                          step={field.step}
                          value={draft[field.key]}
                          onChange={(event) =>
                            update(field.key, Number(event.target.value))
                          }
                        />
                        <input
                          aria-label={`${field.label} value`}
                          type="number"
                          min={field.min}
                          max={field.max}
                          step={field.step}
                          value={draft[field.key]}
                          required
                          onChange={(event) =>
                            update(field.key, Number(event.target.value))
                          }
                        />
                      </div>
                      <p className="helper">{field.help}</p>
                    </div>
                  ))}
                </fieldset>
                <div className="rag-settings-footer">
                  {!canSave && (
                    <p className="helper">
                      Select Supervisor · L2 to change retrieval settings. All
                      roles can preview retrieval.
                    </p>
                  )}
                  {save.isError && (
                    <p className="form-error" role="alert">
                      {save.error.message}
                    </p>
                  )}
                  {save.isSuccess && !dirty && (
                    <p className="rag-save-success" role="status">
                      <Check size={14} aria-hidden />
                      Settings saved
                    </p>
                  )}
                  <div>
                    <button
                      type="button"
                      className="button"
                      disabled={!canSave || save.isPending}
                      onClick={() => {
                        setDraft(query.data!.defaults);
                        save.reset();
                      }}
                    >
                      <RotateCcw size={14} aria-hidden />
                      Reset to defaults
                    </button>
                    <button
                      type="submit"
                      className="button primary"
                      disabled={!canSave || !dirty || save.isPending}
                      aria-busy={save.isPending}
                    >
                      <Save size={14} aria-hidden />
                      {save.isPending ? "Saving…" : "Save settings"}
                    </button>
                  </div>
                  {dirty && (
                    <p className="helper">
                      Unsaved changes. Save settings to use them in previews and
                      new investigations.
                    </p>
                  )}
                </div>
              </form>
            </section>
            <section
              className="secondary-panel rag-playground"
              aria-labelledby="rag-playground-title"
            >
              <div className="secondary-panel-heading">
                <div>
                  <h2 id="rag-playground-title">Retrieval playground</h2>
                  <p>
                    Preview evidence using the saved settings. No narration or
                    graph writes.
                  </p>
                </div>
              </div>
              <form className="rag-query-form" onSubmit={runPreview}>
                <fieldset className="rag-query-mode">
                  <legend className="sr-only">Preview input</legend>
                  <label>
                    <input
                      type="radio"
                      name="rag-query-mode"
                      checked={queryMode === "case"}
                      onChange={() => setQueryMode("case")}
                    />
                    Existing case
                  </label>
                  <label>
                    <input
                      type="radio"
                      name="rag-query-mode"
                      checked={queryMode === "text"}
                      onChange={() => setQueryMode("text")}
                    />
                    Free text
                  </label>
                </fieldset>
                {queryMode === "case" ? (
                  <>
                    <label htmlFor="rag-case" className="sr-only">
                      Case to preview
                    </label>
                    <select
                      id="rag-case"
                      value={caseId}
                      onChange={(event) => setCaseId(event.target.value)}
                      disabled={cases.isPending || !cases.data?.cases.length}
                      required
                    >
                      {!cases.data?.cases.length && (
                        <option value="">
                          {cases.isPending
                            ? "Loading cases…"
                            : "No cases available"}
                        </option>
                      )}
                      {cases.data?.cases.map((item) => (
                        <option key={item.case_id} value={item.case_id}>
                          {item.case_id} · {human(item.trigger_type)}
                          {item.pattern ? ` · ${human(item.pattern)}` : ""}
                        </option>
                      ))}
                    </select>
                    {cases.isError && (
                      <p className="form-error" role="alert">
                        Case list unavailable. Use free text or refresh this
                        page.
                      </p>
                    )}
                  </>
                ) : (
                  <>
                    <label htmlFor="rag-query" className="sr-only">
                      Retrieval query
                    </label>
                    <textarea
                      id="rag-query"
                      rows={3}
                      value={queryText}
                      onChange={(event) => setQueryText(event.target.value)}
                      placeholder="Describe the activity, entities, or policy question to retrieve…"
                      required
                      maxLength={2000}
                    />
                  </>
                )}
                <div className="rag-query-actions">
                  <span className="helper">
                    {queryMode === "case"
                      ? "Evidence is bounded by the case opening time."
                      : "Free text explores retrieval; it does not make a case decision."}
                  </span>
                  <button
                    className="button primary"
                    disabled={
                      preview.isPending ||
                      (queryMode === "case" ? !caseId : !queryText.trim())
                    }
                    aria-busy={preview.isPending}
                  >
                    <Play size={14} aria-hidden />
                    {preview.isPending ? "Retrieving…" : "Preview retrieval"}
                  </button>
                </div>
              </form>
              {preview.isError && (
                <div className="rag-preview-error">
                  <ErrorState error={preview.error} />
                </div>
              )}
              {preview.isPending ? (
                <Skeleton rows={7} />
              ) : !data ? (
                <EmptyState title="Inspect the evidence before narration">
                  Choose a case or enter a query to see precedent rankings,
                  policy passages, graph paths, and the exact context brief.
                </EmptyState>
              ) : (
                <>
                  <div className="rag-preview-summary">
                    <span>{data.precedents.length} precedents</span>
                    <span>{data.policy_chunks.length} policy chunks</span>
                    <span className="mono">
                      ~{data.estimated_tokens.toLocaleString()} tokens
                    </span>
                    <span>{human(data.backend)}</span>
                  </div>
                  {stalePreview && (
                    <p className="rag-notice" role="status">
                      Settings changed since this preview. Preview retrieval
                      again to inspect the saved configuration.
                    </p>
                  )}
                  {!!data.warnings?.length && (
                    <div className="rag-notice">
                      {data.warnings.map((warning, i) => (
                        <p key={i}>{warning}</p>
                      ))}
                    </div>
                  )}
                  <div
                    className="rag-tabs"
                    role="tablist"
                    aria-label="Retrieval results"
                  >
                    {tabs.map((item, index) => (
                      <button
                        key={item.id}
                        id={`rag-tab-${item.id}`}
                        type="button"
                        role="tab"
                        aria-selected={tab === item.id}
                        aria-controls={`rag-panel-${item.id}`}
                        tabIndex={tab === item.id ? 0 : -1}
                        ref={(node) => {
                          tabRefs.current[index] = node;
                        }}
                        onClick={() => setTab(item.id)}
                        onKeyDown={(event) => moveTab(event, index)}
                      >
                        {item.label}
                      </button>
                    ))}
                  </div>
                  <div
                    className="rag-tab-panel"
                    role="tabpanel"
                    id={`rag-panel-${tab}`}
                    aria-labelledby={`rag-tab-${tab}`}
                    tabIndex={0}
                  >
                    {tab === "precedents" && (
                      <ResultList results={data.precedents} kind="precedent" />
                    )}
                    {tab === "policy" && (
                      <ResultList results={data.policy_chunks} kind="policy" />
                    )}
                    {tab === "paths" &&
                      (data.graph_paths.length ? (
                        <div className="rag-path-list">
                          <p className="helper">
                            Relationships used to retrieve these candidates.
                            Shared entities support context; they do not
                            establish fraud on their own.
                          </p>
                          <ul>
                            {data.graph_paths.map((path, i) => (
                              <li key={i}>
                                <GitBranch size={16} aria-hidden />
                                <span>{path}</span>
                              </li>
                            ))}
                          </ul>
                        </div>
                      ) : (
                        <EmptyState title="No graph paths contributed">
                          This preview returned candidates through its other
                          enabled retrieval sources.
                        </EmptyState>
                      ))}
                    {tab === "context" && (
                      <div className="rag-context">
                        <div className="rag-context-heading">
                          <div>
                            <h3>
                              <FileText size={15} aria-hidden />
                              Compact evidence brief
                            </h3>
                            <p className="helper">
                              {data.stage ? `${human(data.stage)} · ` : ""}
                              Estimated {data.estimated_tokens.toLocaleString()}{" "}
                              tokens including system instructions. No LLM was
                              called.
                            </p>
                          </div>
                          <button
                            className="button"
                            type="button"
                            onClick={() => void copyContext()}
                          >
                            {copied ? (
                              <Check size={14} aria-hidden />
                            ) : (
                              <Copy size={14} aria-hidden />
                            )}
                            {copied ? "Copied" : "Copy context"}
                          </button>
                        </div>
                        {copyError && (
                          <p className="form-error" role="alert">
                            {copyError}
                          </p>
                        )}
                        <pre tabIndex={0}>
                          {data.llm_context ||
                            JSON.stringify(data.evidence_brief, null, 2)}
                        </pre>
                        {data.system_instruction && (
                          <details className="rag-policy-text">
                            <summary>System instructions</summary>
                            <pre tabIndex={0}>{data.system_instruction}</pre>
                          </details>
                        )}
                      </div>
                    )}
                  </div>
                </>
              )}
            </section>
          </div>
        )
      )}
    </div>
  );
}
