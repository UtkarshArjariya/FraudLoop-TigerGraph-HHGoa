import { useState } from "react";
import { MessageSquareText, Send } from "lucide-react";
import { useRole } from "../App";
import { human } from "../components";
import type { EvidenceResponse, PendingEvidence } from "../types";

const responses: { value: EvidenceResponse; label: string }[] = [
  { value: "customer_confirms", label: "Customer confirms" },
  { value: "customer_denies", label: "Customer denies" },
  { value: "stepup_passed", label: "Step-up passed" },
  { value: "stepup_failed", label: "Step-up failed" },
];

export default function EvidencePanel({
  request,
  submitting,
  error,
  onRespond,
}: {
  request: PendingEvidence;
  submitting: boolean;
  error: string;
  onRespond: (response: EvidenceResponse, note: string) => void;
}) {
  const { role } = useRole();
  const [note, setNote] = useState("");
  const canRespond = role === "analyst_l1" || role === "supervisor_l2";
  const answered = request.status === "answered";
  const disabled = !canRespond || submitting || answered;
  return (
    <section
      className="panel human-evidence-panel"
      aria-labelledby="evidence-request-title"
    >
      <div className="panel-heading">
        <h2 id="evidence-request-title">
          <MessageSquareText size={18} aria-hidden />
          Evidence requested
        </h2>
        <span className="chip uncertain">
          {answered ? "Resuming" : "Awaiting response"}
        </span>
      </div>
      <div className="human-evidence-body">
        <div className="request-context">
          <h3>{human(request.type) || "Additional verification"}</h3>
          <p>
            {request.reason ||
              "The agent needs additional evidence before making its final recommendation."}
          </p>
          <div className="precedent-basis">
            <strong>Precedent basis</strong>
            {request.precedent_ids?.length ? (
              <div className="provenance-chips">
                {request.precedent_ids.map((id) => (
                  <span className="source-chip mono" key={id}>
                    {id}
                  </span>
                ))}
              </div>
            ) : (
              <p className="helper">
                This request follows the case evidence and policy. No historical
                precedent was cited.
              </p>
            )}
            {request.precedents
              ?.filter((prior) => prior.explanation || prior.analyst_notes)
              .map((prior) => (
                <p className="helper" key={prior.case_id}>
                  <span className="mono">{prior.case_id}</span>:{" "}
                  {prior.explanation || prior.analyst_notes}
                </p>
              ))}
          </div>
          <p className="helper">
            Your response is recorded as human evidence with your demo role and
            the response time.
          </p>
        </div>
        <div className="response-controls" aria-busy={submitting}>
          <label htmlFor="evidence-note">
            Evidence note{" "}
            <span className="muted">
              (optional for a verification response)
            </span>
          </label>
          <textarea
            id="evidence-note"
            rows={3}
            maxLength={3000}
            value={note}
            disabled={disabled}
            onChange={(event) => setNote(event.target.value)}
            placeholder="Record what was verified and any relevant context."
          />
          <div className="response-buttons">
            {responses.map((response) => (
              <button
                type="button"
                className="button"
                key={response.value}
                disabled={disabled}
                onClick={() => onRespond(response.value, note.trim())}
              >
                {response.label}
              </button>
            ))}
          </div>
          <button
            type="button"
            className="button primary note-response"
            disabled={disabled || !note.trim()}
            onClick={() => onRespond("analyst_note", note.trim())}
          >
            <Send size={14} aria-hidden />
            Submit analyst note
          </button>
          {!canRespond && (
            <p className="helper">
              Select Analyst · L1 or Supervisor · L2 in the role selector to
              answer this request.
            </p>
          )}
          {(submitting || answered) && (
            <p className="helper" role="status">
              {answered
                ? "Evidence saved. The agent is reassessing the case."
                : "Saving evidence and resuming the investigation…"}
            </p>
          )}
          {error && (
            <p className="form-error" role="alert">
              {error}
            </p>
          )}
        </div>
      </div>
    </section>
  );
}
