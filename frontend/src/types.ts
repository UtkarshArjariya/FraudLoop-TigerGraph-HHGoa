export type Role = 'viewer' | 'analyst_l1' | 'supervisor_l2';
export type Verdict = 'fraud' | 'legitimate' | 'uncertain';
export interface Action { action: string; route: 'auto' | 'L1' | 'L2'; reason: string }
export interface Evidence { claim: string; source: string; ref: string; entity_ids: string[]; weight?: number }
export interface Answer {
 case_id: string;
 case: { status: string; verdict: Verdict; fraud_probability: number; pattern: string; pattern_description: string; affected_txn_ids: string[]; first_suspicious_txn_id: string; connected_card_ids: string[]; connected_device_profiles: string[]; exposure_usd: number; evidence: Evidence[]; similar_prior_cases: string[]; summary: string; written_to_graph: boolean; graph_case_id: string };
 evidence_requests: {type: string; asked_after_step: number; assumed_response: string}[];
 next_best_actions: { initial: Action[]; final: Action[]; what_changed: string };
 sar: {file: boolean; reason: string; narrative: string; subjects: string[]; total_amount_usd: number; activity_dates: string[]};
 stop_reason: string; tool_calls: number; tokens: number; latency_s: number;
 metadata?: {llm_model_used?: string; llm_attempts?: number};
}
export interface QueueCase { case_id: string; trigger_type: string; opened_at: string; verdict: Verdict | null; probability: number | null; pattern: string; status: string; sar: boolean; sar_flag?: boolean; pending_approval_count: number }
export interface TimelineEvent { event_type: string; payload: Record<string, unknown>; sequence?: number; elapsed_ms?: number; elapsed_s?: number; timestamp?: string; reconstructed?: boolean; source?: string }
export interface Trigger {case_id?: string; trigger_type: string; opened_at: string; trigger_text: string; flagged_txn_id: string; card_id: string; customer_id: string; risk_score?: string}
export interface CaseDetail { answer: Answer | null; timeline: TimelineEvent[]; trigger: Trigger; status?: string; run_id?: string }
export interface VectorIndex { id: string; model: string; attribute: string; count: number; state: string; coverage_source: string; total?: number; coverage?: number }
export interface Health { tigergraph: {gse: string; gpe: string; restpp: string}; mcp: {status: string; detail: string}; gemini: {status: string; models: string[]; cooldowns: unknown; live_checked: boolean}; vector_indexes: VectorIndex[]; demo_role_header: boolean }
export interface GraphData { nodes: {data: {id: string; label: string; type: string; flagged?: boolean; fraud_history?: boolean; [key: string]: unknown}}[]; edges: {data: {id: string; source: string; target: string; label: string}}[]; source: string; truncated: boolean }
export interface Precedent { case_id: string; outcome: string; similarity: number | null; provenance: {source: string; rank?: number; paths?: string[]}[] | string[]; explanation?: string; analyst_notes?: string; pattern?: string; exposure_usd?: number }
export interface Approval { id: string; case_id: string; action: string; route: 'L1' | 'L2'; reason: string; status?: string; decision?: string; note?: string; role?: Role; timestamp?: string; decided_at?: string; graph_written?: boolean }
export interface MemoryStats {closed_cases: number; vector_indexes: VectorIndex[]; pattern_counts: Record<string, number>; recurring_devices: {device?: string; device_profile?: string; id?: string; case_ids?: string[]; count?: number}[]; recurring_regions: {region?: string; id?: string; case_ids?: string[]; count?: number}[]}
