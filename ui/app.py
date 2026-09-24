"""Read-only saved investigation evidence and retrieval provenance viewer."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import streamlit as st
from agent.retrieval import match_label

st.set_page_config(page_title="FRAUDLOOP evidence", layout="wide")
st.title("FRAUDLOOP")
st.caption("Saved investigations · recommendations await the stated approvals · additional verification replies are simulated")
files = sorted(Path("cases").glob("HHG-*.json"))
if not files:
    st.info("No saved case answers are available.")
    st.stop()
selected = st.sidebar.selectbox("Investigation", files, format_func=lambda path: path.stem)
answer = json.loads(selected.read_text())
case = answer["case"]
st.subheader(f"{answer['case_id']} · {case['verdict']}")
st.write(case["summary"])
meta = answer.get("metadata", {})
st.caption(f"Narration: {meta.get('llm_model_used', 'not recorded')} · Attempts: {meta.get('llm_attempts', 'not recorded')}")
st.subheader("Evidence")
for item in case["evidence"]:
    st.write(item["claim"])
    st.caption(f"{item['source']} · {item['ref']}")
trace_path = Path("data/runs") / f"{answer['case_id']}.trace.json"
if trace_path.exists():
    trace = json.loads(trace_path.read_text())
    for label, key in (("Precedent matches", "precedent_retrieval"), ("Policy matches", "document_retrieval")):
        rows = trace.get(key, [])
        if rows:
            st.subheader(label)
            st.dataframe([{"Result": row.get("case_id", row.get("id")),
                           "Matched by": match_label(row.get("provenance", [])),
                           "RRF score": row.get("fused_score")} for row in rows], hide_index=True)
with st.expander("Before and after recommendations"):
    st.json(answer["next_best_actions"])
with st.expander("SAR recommendation and approvals"):
    st.json(answer["sar"])
