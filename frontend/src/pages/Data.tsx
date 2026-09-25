import { useState, type ChangeEvent } from "react";
import { useMutation } from "@tanstack/react-query";
import { Check, Database, FileText, Upload } from "lucide-react";
import { useRole } from "../App";
import { api } from "../api";
import { PageHeading, human } from "../components";
import "./secondary.css";
import "./data.css";

type ImportKind = "transactions" | "policy";
type DataRow = Record<string, unknown>;
interface IngestionResult {
  preview_id?: string;
  digest?: string;
  kind: string;
  status: string;
  rows?: number | DataRow[];
  chunks?: number | DataRow[];
  sample?: DataRow[];
  errors?: (string | DataRow)[];
  warnings?: string[];
  warning?: string;
  can_commit?: boolean;
  written_to_graph: boolean;
  counts?: Record<string, number>;
  items?: DataRow[];
  results?: DataRow[];
  graph?: { warning?: string; written_to_graph?: boolean; source?: string };
  [key: string]: unknown;
}
const formatValue = (value: unknown) =>
  value == null
    ? "—"
    : typeof value === "object"
      ? JSON.stringify(value)
      : String(value);
function ResultTable({ rows }: { rows: DataRow[] }) {
  if (!rows.length) return null;
  const columns = [...new Set(rows.flatMap((row) => Object.keys(row)))].slice(
    0,
    8,
  );
  return (
    <div
      className="data-preview-table"
      tabIndex={0}
      aria-label="Import result rows"
    >
      <table>
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column}>{human(column)}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 10).map((row, i) => (
            <tr key={i}>
              {columns.map((column) => (
                <td key={column}>{formatValue(row[column])}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
function ImportPanel({ kind }: { kind: ImportKind }) {
  const { role } = useRole();
  const isPolicy = kind === "policy";
  const limit = isPolicy ? 64 * 1024 : 2 * 1024 * 1024;
  const [filename, setFilename] = useState("");
  const [content, setContent] = useState("");
  const [size, setSize] = useState(0);
  const [fileError, setFileError] = useState("");
  const [reading, setReading] = useState(false);
  const preview = useMutation({
    mutationFn: () =>
      api<IngestionResult>(`/ingest/${kind}`, {
        method: "POST",
        body: JSON.stringify({ action: "preview", content, filename }),
      }),
  });
  const commit = useMutation({
    mutationFn: () =>
      api<IngestionResult>(`/ingest/${kind}`, {
        method: "POST",
        body: JSON.stringify({
          action: "commit",
          content,
          filename,
          preview_id: preview.data?.preview_id,
        }),
      }),
  });
  const busy = reading || preview.isPending || commit.isPending;
  const result = commit.data || preview.data;
  async function chooseFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    preview.reset();
    commit.reset();
    setFileError("");
    setContent("");
    setFilename("");
    setSize(0);
    if (!file) return;
    if (file.size > limit) {
      setFileError(
        `This file is too large. Choose a file up to ${isPolicy ? "64 KB" : "2 MB"}.`,
      );
      event.target.value = "";
      return;
    }
    if (!file.size) {
      setFileError("The selected file is empty.");
      return;
    }
    if (!(isPolicy ? /\.(md|markdown|txt)$/i : /\.csv$/i).test(file.name)) {
      setFileError(
        isPolicy
          ? "Choose a .md, .markdown, or .txt file."
          : "Choose a .csv file.",
      );
      event.target.value = "";
      return;
    }
    setReading(true);
    try {
      const text = await file.text();
      if (!text.trim()) throw new Error("The selected file contains no text.");
      setContent(text);
      setFilename(file.name);
      setSize(file.size);
    } catch (error) {
      setFileError(
        error instanceof Error
          ? error.message
          : "The selected file could not be read.",
      );
    } finally {
      setReading(false);
    }
  }
  const sample = Array.isArray(result?.sample) ? result.sample : [];
  const items = Array.isArray(result?.items)
    ? result.items
    : Array.isArray(result?.results)
      ? result.results
      : [];
  const errors = result?.errors || [];
  const warnings = [
    ...(result?.warnings || []),
    result?.warning,
    result?.graph?.warning,
  ].filter((value): value is string => Boolean(value));
  const resultTone =
    result?.status === "failed"
      ? "fraud"
      : ["partial", "staged_locally"].includes(result?.status || "")
        ? "uncertain"
        : (commit.data && result?.written_to_graph) || result?.can_commit
          ? "legitimate"
          : errors.length
            ? "uncertain"
            : "pending";
  return (
    <section
      className="secondary-panel data-import-panel"
      aria-labelledby={`${kind}-title`}
    >
      <div className="secondary-panel-heading">
        <div>
          <h2 id={`${kind}-title`}>
            {isPolicy ? (
              <FileText size={18} aria-hidden />
            ) : (
              <Database size={18} aria-hidden />
            )}
            {isPolicy ? "Policy documents" : "Transaction CSV"}
          </h2>
          <p>
            {isPolicy
              ? "Add policy passages to BGE retrieval context."
              : "Validate transaction records against the supplied dataset before graph upsert."}
          </p>
        </div>
      </div>
      <div className="data-import-body">
        <label className="data-file-label" htmlFor={`upload-${kind}`}>
          {isPolicy ? "Markdown or text document" : "CSV file"}
        </label>
        <input
          id={`upload-${kind}`}
          type="file"
          accept={
            isPolicy
              ? ".md,.markdown,.txt,text/plain,text/markdown"
              : ".csv,text/csv"
          }
          disabled={busy}
          onChange={(event) => void chooseFile(event)}
        />
        <p className="helper">
          {isPolicy
            ? "Up to 64 KB. Policy text supplies retrieval context; encoded bank rules still own decisions."
            : "Up to 2 MB and 2,000 rows. Use prepared txn_id or original TransactionID identifiers and dataset columns."}
        </p>
        {filename && (
          <div className="data-file-meta">
            <span>{filename}</span>
            <span>
              {size < 1024 ? `${size} bytes` : `${(size / 1024).toFixed(1)} KB`}
            </span>
          </div>
        )}
        {fileError && (
          <p className="form-error" role="alert">
            {fileError}
          </p>
        )}
        <div className="data-import-actions">
          <button
            className="button"
            type="button"
            disabled={!content || busy}
            aria-busy={preview.isPending}
            onClick={() => {
              commit.reset();
              preview.mutate();
            }}
          >
            <FileText size={14} aria-hidden />
            {preview.isPending ? "Validating…" : "Preview import"}
          </button>
          <button
            className="button primary"
            type="button"
            disabled={
              role !== "supervisor_l2" ||
              !preview.data?.can_commit ||
              !preview.data?.preview_id ||
              busy ||
              commit.data?.status === "committed"
            }
            aria-busy={commit.isPending}
            onClick={() => commit.mutate()}
          >
            <Upload size={14} aria-hidden />
            {commit.isPending
              ? "Importing…"
              : commit.data?.status === "committed"
                ? "Imported"
                : commit.data
                  ? "Retry import"
                  : "Commit import"}
          </button>
        </div>
        {role !== "supervisor_l2" && (
          <p className="helper">
            All roles can preview. Select Supervisor · L2 to commit a validated
            import.
          </p>
        )}
        {(preview.isError || commit.isError) && (
          <p className="form-error" role="alert">
            {(commit.error || preview.error)?.message}
          </p>
        )}
      </div>
      {result && (
        <div className="data-result">
          <div className="data-result-heading">
            <h3>{commit.data ? "Import result" : "Validation preview"}</h3>
            <span className={`chip ${resultTone}`}>{human(result.status)}</span>
          </div>
          <div className="data-result-counts">
            {!isPolicy && typeof result.rows === "number" && (
              <span>{result.rows.toLocaleString()} rows</span>
            )}
            {isPolicy && typeof result.chunks === "number" && (
              <span>{result.chunks.toLocaleString()} chunks</span>
            )}
            {Object.entries(result.counts || {}).map(([key, value]) => (
              <span key={key}>
                {human(key)}:{" "}
                <strong className="mono">{value.toLocaleString()}</strong>
              </span>
            ))}
          </div>
          <p className="data-write-status">
            {commit.data ? (
              result.written_to_graph ? (
                <>
                  <Check size={14} aria-hidden />
                  Graph writes confirmed by the import receipt.
                </>
              ) : (
                "The receipt does not confirm a complete graph write. Review item results and warnings."
              )
            ) : (
              "Preview only. No graph writes have been performed."
            )}
          </p>
          {errors.length > 0 && (
            <div className="data-validation-errors" role="alert">
              <strong>Validation issues</strong>
              <ul>
                {errors.map((error, i) => (
                  <li key={i}>{formatValue(error)}</li>
                ))}
              </ul>
            </div>
          )}
          {warnings.length > 0 && (
            <div className="data-import-warnings" role="status">
              {[...new Set(warnings)].map((warning) => (
                <p key={warning}>{warning}</p>
              ))}
            </div>
          )}
          <ResultTable rows={items.length ? items : sample} />
          {!commit.data && result.can_commit && (
            <p className="helper data-result-note">
              Review this preview, then commit the same file. Selecting another
              file requires a new preview.
            </p>
          )}
          <details className="data-receipt">
            <summary>
              Full {commit.data ? "import receipt" : "validation result"}
            </summary>
            <pre>{JSON.stringify(result, null, 2)}</pre>
          </details>
        </div>
      )}
    </section>
  );
}
export default function Data() {
  return (
    <div className="secondary-page data-page">
      <PageHeading
        title="Data"
        description="Preview transaction and policy imports before committing them to the existing graph."
      />
      <div className="data-import-grid">
        <ImportPanel kind="transactions" />
        <ImportPanel kind="policy" />
      </div>
    </div>
  );
}
