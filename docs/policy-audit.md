# FRAUDLOOP Phase 1 policy audit

Audited on 2026-09-25 against the versioned [grading specification](../HHGOA_IEEE/README.md), all 20 saved `cases/HHG-*.json` files, their original `data/runs/*.trace.json` files, and `agent/policy.py`, `agent/assess.py`, `agent/investigate.py`, and `agent/answer.py`. This is a policy and internal-consistency audit, not a comparison with the hidden answer key.

**Result: 9 fraud, 8 legitimate, 3 uncertain; 2 SAR recommendations (HHG-006 and HHG-014).** No unambiguous violation requiring a policy-engine change was found. The graded JSON files were preserved; no regeneration, graph write, model call, embedding job, or schema operation was performed for this audit.

Two interpretation limits deserve explicit review: partial or common device-fingerprint overlap is not treated as proof of a shared fraud origin, and HHG-006's purchase-splitting analogy does not prove a current cross-customer ring. Those limits and the independent SAR basis are discussed below.

## Verification

- `.venv/bin/python -m scripts.validate` — passed for all 20 cases: schema, dataset IDs, episode exposure, chronology, routes and policy checks.
- `.venv/bin/python -m pytest -q` — baseline passed: **159 tests in 3.42 seconds**.
- `.venv/bin/python -m pytest -q tests/test_policy.py` — focused audit check passed: **30 tests in 0.57 seconds**.
- A separate read-only pass through all saved JSON/trace files checked every initial/final action route, SAR/action equivalence, legitimate-case invariants and the recorded ordering of initial recommendation, evidence request, reassessment and final recommendation. All 20 passed.
- Existing traces place `INITIAL_NBA` before `REQUEST_EVIDENCE` and `REASSESS`, then `FINAL_NBA`, in all five cases with evidence requests. Each request records step 4 and explicitly labels its reply `SIMULATED`. The other 15 cases have identical initial/final actions and `what_changed: "nothing"`.

The validator uses the prepared transaction store derived from the supplied dataset. No original Kaggle labels were used. Entity IDs, amount sums and temporal references were validated against that store; this audit did not load the full transaction CSV. Saved `written_to_graph=true` fields describe the original answer run. They do not prove that each graph payload was re-read during this audit.

## Verdicts, exposure and actions

Routes shown after each action are exact saved values. Exposure is the final suspicious episode, not the original risk-score amount or a customer balance. For cases resolved as legitimate after evidence, initial candidate exposure is noted in the evidence table below.

| Case | Verdict | Probability | Pattern | Exposure USD | Affected txns | Initial actions / route | Final actions / route | SAR |
|---|---|---:|---|---:|---:|---|---|---|
| [HHG-001](../cases/HHG-001.json) | legitimate | 0.0293 | `none` | $0.00 | 0 | `CREATE_CASE` (auto)<br>`CLOSE_NO_FRAUD` (auto) | `CREATE_CASE` (auto)<br>`CLOSE_NO_FRAUD` (auto) | No |
| [HHG-002](../cases/HHG-002.json) | legitimate | 0.0400 | `none` | $0.00 | 0 | `CREATE_CASE` (auto)<br>`VERIFY_WITH_CUSTOMER` (auto)<br>`MONITOR_CARD` (auto) | `CREATE_CASE` (auto)<br>`CLOSE_NO_FRAUD` (auto) | No |
| [HHG-003](../cases/HHG-003.json) | fraud | 0.9000 | `none` | $49.00 | 1 | `CREATE_CASE` (auto)<br>`BLOCK_CARD` (L1) | `CREATE_CASE` (auto)<br>`BLOCK_CARD` (L1) | No |
| [HHG-004](../cases/HHG-004.json) | fraud | 0.9606 | `card_not_present_new_device` | $128.33 | 1 | `CREATE_CASE` (auto)<br>`BLOCK_CARD` (L1) | `CREATE_CASE` (auto)<br>`BLOCK_CARD` (L1) | No |
| [HHG-005](../cases/HHG-005.json) | legitimate | 0.0339 | `none` | $0.00 | 0 | `CREATE_CASE` (auto)<br>`CLOSE_NO_FRAUD` (auto) | `CREATE_CASE` (auto)<br>`CLOSE_NO_FRAUD` (auto) | No |
| [HHG-006](../cases/HHG-006.json) | fraud | 0.9979 | `undocumented` | $1,906.07 | 4 | `CREATE_CASE` (auto)<br>`BLOCK_CARD` (L1)<br>`FILE_REPORT` (L2)<br>`ESCALATE_TO_ANALYST` (auto) | `CREATE_CASE` (auto)<br>`BLOCK_CARD` (L1)<br>`FILE_REPORT` (L2)<br>`ESCALATE_TO_ANALYST` (auto) | Yes |
| [HHG-007](../cases/HHG-007.json) | legitimate | 0.0293 | `none` | $0.00 | 0 | `CREATE_CASE` (auto)<br>`CLOSE_NO_FRAUD` (auto) | `CREATE_CASE` (auto)<br>`CLOSE_NO_FRAUD` (auto) | No |
| [HHG-008](../cases/HHG-008.json) | fraud | 0.9340 | `card_not_present_fraud` | $166.97 | 3 | `CREATE_CASE` (auto)<br>`BLOCK_CARD` (L1) | `CREATE_CASE` (auto)<br>`BLOCK_CARD` (L1) | No |
| [HHG-009](../cases/HHG-009.json) | fraud | 0.9000 | `card_not_present_fraud` | $30.02 | 1 | `CREATE_CASE` (auto)<br>`BLOCK_CARD` (L1) | `CREATE_CASE` (auto)<br>`BLOCK_CARD` (L1) | No |
| [HHG-010](../cases/HHG-010.json) | uncertain | 0.5622 | `card_not_present_new_device` | $1,000.03 | 1 | `CREATE_CASE` (auto)<br>`MONITOR_CARD` (auto)<br>`ESCALATE_TO_ANALYST` (auto) | `CREATE_CASE` (auto)<br>`MONITOR_CARD` (auto)<br>`ESCALATE_TO_ANALYST` (auto) | No |
| [HHG-011](../cases/HHG-011.json) | fraud | 0.9129 | `card_not_present_new_device` | $131.30 | 1 | `CREATE_CASE` (auto)<br>`BLOCK_CARD` (L1) | `CREATE_CASE` (auto)<br>`BLOCK_CARD` (L1) | No |
| [HHG-012](../cases/HHG-012.json) | legitimate | 0.0293 | `none` | $0.00 | 0 | `CREATE_CASE` (auto)<br>`CLOSE_NO_FRAUD` (auto) | `CREATE_CASE` (auto)<br>`CLOSE_NO_FRAUD` (auto) | No |
| [HHG-013](../cases/HHG-013.json) | legitimate | 0.0547 | `none` | $0.00 | 0 | `CREATE_CASE` (auto)<br>`CLOSE_NO_FRAUD` (auto) | `CREATE_CASE` (auto)<br>`CLOSE_NO_FRAUD` (auto) | No |
| [HHG-014](../cases/HHG-014.json) | fraud | 0.9100 | `undocumented` | $3,778.14 | 26 | `CREATE_CASE` (auto)<br>`BLOCK_CARD` (L2)<br>`MONITOR_CONNECTED_CARDS` (auto)<br>`FILE_REPORT` (L2)<br>`ESCALATE_TO_ANALYST` (auto) | `CREATE_CASE` (auto)<br>`BLOCK_CARD` (L2)<br>`MONITOR_CONNECTED_CARDS` (auto)<br>`FILE_REPORT` (L2)<br>`ESCALATE_TO_ANALYST` (auto) | Yes |
| [HHG-015](../cases/HHG-015.json) | uncertain | 0.7697 | `card_not_present_new_device` | $599.94 | 1 | `CREATE_CASE` (auto)<br>`MONITOR_CARD` (auto)<br>`ESCALATE_TO_ANALYST` (auto) | `CREATE_CASE` (auto)<br>`MONITOR_CARD` (auto)<br>`ESCALATE_TO_ANALYST` (auto) | No |
| [HHG-016](../cases/HHG-016.json) | fraud | 0.9000 | `card_not_present_new_device` | $59.67 | 1 | `CREATE_CASE` (auto)<br>`BLOCK_CARD` (L1) | `CREATE_CASE` (auto)<br>`BLOCK_CARD` (L1) | No |
| [HHG-017](../cases/HHG-017.json) | legitimate | 0.0400 | `none` | $0.00 | 0 | `CREATE_CASE` (auto)<br>`VERIFY_WITH_CUSTOMER` (auto)<br>`MONITOR_CARD` (auto) | `CREATE_CASE` (auto)<br>`CLOSE_NO_FRAUD` (auto) | No |
| [HHG-018](../cases/HHG-018.json) | fraud | 0.9000 | `none` | $39.08 | 1 | `CREATE_CASE` (auto)<br>`BLOCK_CARD` (L1) | `CREATE_CASE` (auto)<br>`BLOCK_CARD` (L1) | No |
| [HHG-019](../cases/HHG-019.json) | legitimate | 0.0339 | `none` | $0.00 | 0 | `CREATE_CASE` (auto)<br>`CLOSE_NO_FRAUD` (auto) | `CREATE_CASE` (auto)<br>`CLOSE_NO_FRAUD` (auto) | No |
| [HHG-020](../cases/HHG-020.json) | uncertain | 0.5374 | `card_not_present_new_device` | $125.08 | 1 | `CREATE_CASE` (auto)<br>`STEP_UP_AUTH` (auto)<br>`MONITOR_CARD` (auto) | `CREATE_CASE` (auto)<br>`MONITOR_CARD` (auto) | No |

## Device, region and customer links

“Profile groups” is the count stated in the saved device-query evidence; it includes the flagged group and is not a count of confirmed compromised cards. “Linked compromise” refers to the actual scoped fraud finding used for R6/section 3a. A historical case with a similar device name is a precedent, not proof that every present-day matching card is fraudulent. Billing regions are anonymized codes, not cities or precise locations.

| Case | Flagged customer / card | Saved device evidence | Billing-region evidence | Linked compromise |
|---|---|---|---|---|
| HHG-001 | `C12382` / `C12382-K1` | Not an online device finding | 444.0 present in prior card history | No additional card/customer compromise established |
| HHG-002 | `C11891` / `C11891-K1` | 0 profile groups; partial/missing; no usable profile | No billing-region finding in saved evidence | No additional card/customer compromise established |
| HHG-003 | `C08623` / `C08623-K2` | Not an online device finding | 330.0 present in prior card history | No additional card/customer compromise established |
| HHG-004 | `C08106` / `C08106-K1` | 1 profile groups; partial/missing. `&#124;&#124;firefox 47.0&#124;` | No billing-region finding in saved evidence | No additional card/customer compromise established |
| HHG-005 | `C02923` / `C02923-K1` | 1 profile groups; complete. `iOS Device&#124;iOS 9.3.5&#124;mobile safari 9.0&#124;1024x768` | 330.0 present in prior card history | No additional card/customer compromise established |
| HHG-006 | `C07297` / `C07297-K1` | 19 profile groups; complete. `Trident/7.0&#124;Windows 7&#124;ie 11.0 for desktop&#124;1920x1080` | 264.0 present in prior card history | No additional card/customer compromise established |
| HHG-007 | `C09933` / `C09933-K2` | Not an online device finding | 264.0 present in prior card history | No additional card/customer compromise established |
| HHG-008 | `C13171` / `C13171-K2` | 86 profile groups; partial/missing. `&#124;&#124;chrome 66.0&#124;` | No billing-region finding in saved evidence | No additional card/customer compromise established |
| HHG-009 | `C08299` / `C08299-K1` | 0 profile groups; partial/missing; no usable profile | 203.0 present in prior card history | No additional card/customer compromise established |
| HHG-010 | `C10434` / `C10434-K1` | 8 profile groups; complete. `Windows&#124;Windows 10&#124;edge 16.0&#124;1366x768` | 469.0 present in prior card history | No additional card/customer compromise established |
| HHG-011 | `C11923` / `C11923-K2` | 3 profile groups; partial/missing. `SM-G610F Build/NRD90M&#124;&#124;chrome 66.0 for android&#124;` | No billing-region finding in saved evidence | No additional card/customer compromise established |
| HHG-012 | `C05876` / `C05876-K2` | Not an online device finding | 494.0 present in prior card history | No additional card/customer compromise established |
| HHG-013 | `C07671` / `C07671-K2` | 63 profile groups; partial/missing. `Windows&#124;&#124;chrome 66.0&#124;` | No billing-region finding in saved evidence | No additional card/customer compromise established |
| HHG-014 | `C13487` / `C13487-K1` | 19 profile groups; complete. `SM-G935F Build/NRD90M&#124;Android 7.0&#124;chrome 62.0 for android&#124;1920x1080` | 191.0 present in prior card history | 20 scoped groups across 20 customers; 10 other known cards plus 9 groups needing analyst card-ID mapping; exact shared device (details below) |
| HHG-015 | `C03042` / `C03042-K1` | 1 profile groups; complete. `Trident/7.0&#124;Windows 8.1&#124;ie 11.0 for desktop&#124;1680x1050` | 327.0 absent in prior card history | No additional card/customer compromise established |
| HHG-016 | `C09988` / `C09988-K1` | 23 profile groups; partial/missing. `Windows&#124;&#124;edge 16.0&#124;` | No billing-region finding in saved evidence | No additional card/customer compromise established |
| HHG-017 | `C04570` / `C04570-K1` | 70 profile groups; complete. `Windows&#124;Windows 10&#124;chrome 65.0&#124;1920x1080` | 204.0 present in prior card history | No additional card/customer compromise established |
| HHG-018 | `C02354` / `C02354-K2` | Not an online device finding | 126.0 present in prior card history | No additional card/customer compromise established |
| HHG-019 | `C07987` / `C07987-K2` | 3 profile groups; complete. `Windows&#124;other&#124;chrome 61.0&#124;1280x720` | 264.0 present in prior card history | No additional card/customer compromise established |
| HHG-020 | `C12265` / `C12265-K2` | 17 profile groups; complete. `Trident/7.0&#124;Windows 10&#124;ie 11.0 for desktop&#124;1920x1080` | 264.0 present in prior card history | No additional card/customer compromise established |

HHG-014 identifies the exact shared profile `SM-G935F Build/NRD90M|Android 7.0|chrome 62.0 for android|1920x1080`, a proxy indicator, confirmed historical coordinated-abuse precedents, and a scoped episode of 26 transactions. The saved seven-day profile count is 19 groups; the expanded analyst episode spans 2016-11-14 through 2016-11-22 and includes 20 groups. These are different windows, not interchangeable counts.

Its ten known connected cards are `C01996-K2`, `C02910-K1`, `C02975-K1`, `C03676-K2`, `C04108-K2`, `C10193-K2`, `C10326-K1`, `C11082-K1`, `C11670-K2`, and `C12796-K1`. The flagged card is `C13487-K1`. Nine additional groups are cited through existing customers `C01289`, `C04274`, `C05448`, `C06710`, `C08168`, `C09049`, `C09906`, `C09975`, and `C13291`, plus their actual transaction IDs; no invented `unresolved:` card IDs appear. `MONITOR_CONNECTED_CARDS` explicitly covers every scoped group and requests analyst mapping.

The HHG-014 episode includes billing regions 123.0, 126.0, 157.0, 181.0, 184.0, 191.0, 204.0, 231.0, 299.0, 315.0, 327.0, 330.0, 441.0 and 472.0. The supported common origin is the device, not a claim that these codes all represent one physical region. HHG-006 includes regions 264.0 and 476.0 but only one identified customer/card in its suspicious episode.

## Evidence requests and meaningful changes

| Case | Initial probability / candidate exposure | Requested evidence | Assumed result | What changed |
|---|---|---|---|---|
| HHG-002 | 0.4335 / $292.36 | `customer_validation` after step 4 | Simulated customer confirms authorization | The explicitly simulated customer_validation response changed probability from 0.433 to 0.040 and the verdict from uncertain to legitimate; final recommendations reflect that response. |
| HHG-010 | 0.5622 / $1,000.03 | `analyst_info` after step 4 | Simulated analyst has no additional authorization fact | The explicitly simulated response provided no decisive authorization evidence. The final recommendation retains the unresolved verdict and the policy-required monitoring or escalation. |
| HHG-015 | 0.7697 / $599.94 | `analyst_info` after step 4 | Simulated analyst has no additional authorization fact | The explicitly simulated response provided no decisive authorization evidence. The final recommendation retains the unresolved verdict and the policy-required monitoring or escalation. |
| HHG-017 | 0.3806 / $300.14 | `customer_validation` after step 4 | Simulated customer confirms authorization | The explicitly simulated customer_validation response changed probability from 0.381 to 0.040 and the verdict from uncertain to legitimate; final recommendations reflect that response. |
| HHG-020 | 0.5374 / $125.08 | `step_up_auth` after step 4 | Simulated step-up receives no response within 24 hours | The explicitly simulated response provided no decisive authorization evidence. The final recommendation retains the unresolved verdict and the policy-required monitoring or escalation. |

HHG-002 and HHG-017 provide a real change in the saved recommendation: verification and monitoring become `CLOSE_NO_FRAUD` after the explicitly simulated confirmation; the final suspicious IDs and exposure are cleared. HHG-010 and HHG-015 retain the same action names because the analyst simulation supplies no decisive new fact; the saved explanation says why. HHG-020 removes the completed step-up request and keeps monitoring after the simulated timeout. No pending authorization is established, so R4 does not justify inventing a decline.

## Stopping rule, case by case

The following is the exact saved `stop_reason`; it is checked against the recorded response/family context, not merely the final probability.

| Case | Saved stop_reason | Audit basis |
|---|---|---|
| HHG-001 | Section 6: fraud probability is at most 0.15 with at least two independent evidence families. | 3 independent evidence families with probability at most 0.15. |
| HHG-002 | Section 6: a verification response settled the question. | Explicitly simulated verification settles the question; two independent families are not required for this separate stop condition. |
| HHG-003 | R2 and section 6: the customer denial already recorded in the case pack settles the authorization question for this recommendation. No additional customer contact was performed. | Observed case-pack denial settles authorization under R2/section 6; additional contact is not claimed. |
| HHG-004 | R2 and section 6: the customer denial already recorded in the case pack settles the authorization question for this recommendation. No additional customer contact was performed. | Observed case-pack denial settles authorization under R2/section 6; additional contact is not claimed. |
| HHG-005 | Section 6: fraud probability is at most 0.15 with at least two independent evidence families. | 3 independent evidence families with probability at most 0.15. |
| HHG-006 | R2 and section 6: the customer denial already recorded in the case pack settles the authorization question for this recommendation. No additional customer contact was performed. | Observed case-pack denial settles authorization under R2/section 6; additional contact is not claimed. |
| HHG-007 | Section 6: fraud probability is at most 0.15 with at least two independent evidence families. | 3 independent evidence families with probability at most 0.15. |
| HHG-008 | R2 and section 6: the customer denial already recorded in the case pack settles the authorization question for this recommendation. No additional customer contact was performed. | Observed case-pack denial settles authorization under R2/section 6; additional contact is not claimed. |
| HHG-009 | R2 and section 6: the customer denial already recorded in the case pack settles the authorization question for this recommendation. No additional customer contact was performed. | Observed case-pack denial settles authorization under R2/section 6; additional contact is not claimed. |
| HHG-010 | Section 6: the remaining uncertainty is authorization that anonymized records cannot settle. The simulated response added no decisive evidence; further automated steps are unlikely to change the recommendation. R8 escalation is recommended. | Explicit exhaustion of useful automated authorization evidence; escalation or monitoring remains recommended. |
| HHG-011 | R2 and section 6: the customer denial already recorded in the case pack settles the authorization question for this recommendation. No additional customer contact was performed. | Observed case-pack denial settles authorization under R2/section 6; additional contact is not claimed. |
| HHG-012 | Section 6: fraud probability is at most 0.15 with at least two independent evidence families. | 3 independent evidence families with probability at most 0.15. |
| HHG-013 | Section 6: fraud probability is at most 0.15 with at least two independent evidence families. | 2 independent evidence families with probability at most 0.15. |
| HHG-014 | Section 6: fraud probability is at least 0.85 with at least two independent evidence families. | 3 independent evidence families with probability at least 0.85. |
| HHG-015 | Section 6: the remaining uncertainty is authorization that anonymized records cannot settle. The simulated response added no decisive evidence; further automated steps are unlikely to change the recommendation. R8 escalation is recommended. | Explicit exhaustion of useful automated authorization evidence; escalation or monitoring remains recommended. |
| HHG-016 | R2 and section 6: the customer denial already recorded in the case pack settles the authorization question for this recommendation. No additional customer contact was performed. | Observed case-pack denial settles authorization under R2/section 6; additional contact is not claimed. |
| HHG-017 | Section 6: a verification response settled the question. | Explicitly simulated verification settles the question; two independent families are not required for this separate stop condition. |
| HHG-018 | R2 and section 6: the customer denial already recorded in the case pack settles the authorization question for this recommendation. No additional customer contact was performed. | Observed case-pack denial settles authorization under R2/section 6; additional contact is not claimed. |
| HHG-019 | Section 6: fraud probability is at most 0.15 with at least two independent evidence families. | 3 independent evidence families with probability at most 0.15. |
| HHG-020 | Section 6: the remaining uncertainty is authorization that anonymized records cannot settle. The simulated response added no decisive evidence; further automated steps are unlikely to change the recommendation. The card remains under monitoring and the case remains open. | Explicit exhaustion of useful automated authorization evidence; escalation or monitoring remains recommended. |

## Rule-level findings

| Rule / invariant | Finding |
|---|---|
| Section 3a: an internal case is distinct from a report | Every saved recommendation retains `CREATE_CASE`, reflecting the existing investigation record. Both SARs have a case. No legitimate case files a SAR. |
| Section 3a: supported fraud plus a reporting trigger | HHG-006 has an observed denial and $1,906.07 exposure. HHG-014 has 0.91 probability, three supporting families, $3,778.14 exposure and a scoped shared origin. Both qualify independently of a risk score. HHG-010 exceeds $1,000 but remains at 0.5622 without a qualifying shared/coordinated finding, so exposure alone does not force a SAR. HHG-015 has 0.7697 probability but $599.94 exposure and no qualifying connection/pattern trigger. |
| SAR/action equivalence | For all 20 cases, `sar.file` is true exactly when `FILE_REPORT` is in final actions. Both report actions route to L2. Their narratives include subjects, activity, time, channels/region codes, method, suspicious basis and human-approval disclosure. |
| Section 2: approval routes | Every initial/final route matches policy. Eight low-exposure fraud cases use `BLOCK_CARD` L1, including HHG-006 at $1,906.07. HHG-014 uses L2 at $3,778.14. No `BLOCK_ALL_CARDS` occurs. All nonrestricted actions use auto. |
| R1: weak-signal verification | HHG-002 and HHG-017 begin below 0.70 with one supporting evidence family and recommend verification before any block. Neither blocks. HHG-020 requests step-up and does not block. The cases already resolved by observed customer denial follow the separate R2 basis. |
| R2: customer denial | All eight customer-report triggers retain `CREATE_CASE` and `BLOCK_CARD`. HHG-006 also files a report because its total exposure exceeds $1,000. The other seven lack a supported shared-origin finding and remain below the amount trigger. Partial-fingerprint interpretation is disclosed below. |
| R3: customer confirmation | HHG-002 and HHG-017 explicitly record simulated authorization confirmation and recommend `CLOSE_NO_FRAUD`; their legitimate episode fields are empty/zero. |
| R4: 24-hour timeout | HHG-020 retains `MONITOR_CARD`; exposure is $125.08. No pending authorization is assumed, so no decline is added. |
| R5: card testing | None of the 20 saved cases claims three small online authorizations followed by a larger purchase. HHG-006 is four near-$500 purchases, not card testing. Focused policy tests cover the required decline/step-up and cleared-purchase block behavior. |
| R6: shared-origin fraud | HHG-014 recommends `CREATE_CASE`, `FILE_REPORT` L2 and `MONITOR_CONNECTED_CARDS`, names the shared origin, and covers both known and unresolved card groups through existing IDs. |
| R7: recurring-charge disputes | No saved case asserts a same-merchant match because the dataset has no merchant ID. HHG-018 discloses its recurring amount/billing candidate without converting it into a proven R7 exception to the observed denial. |
| R8: uncertainty and exposure/conflict | HHG-010 ($1,000.03) and HHG-015 ($599.94) recommend analyst escalation initially and finally. HHG-020 ($125.08) stays open with monitoring. No saved context marks unresolved evidence conflict without escalation. |
| R9: undocumented coordinated/repeated activity | HHG-014 has the supported cross-customer device episode and all three required actions. HHG-006 has repeated purchase splitting and historical cross-customer analogies; its narrative expressly stops short of proving a common actor. Its extra analyst escalation is conservative, and the report remains independently required by R2/3a. |
| R10: broad blocking | No saved recommendation blocks all of a customer's cards. Tests require two confirmed cards or compromised credentials before that action. |
| Legitimate-case invariant | All eight legitimate results have no affected transaction IDs, zero exposure, `pattern=none` and no SAR. |
| Identifiers and chronology | The validator passed structured IDs, recognized IDs in prose, first suspicious transaction, absolute-amount exposure sums, SAR dates, as-of transaction bounds and prior-case/memory chronology. No generated internal `unresolved:` identifier appears as an authoritative entity. |
| Section 6: stopping | Six legitimate cases stop at low probability with two or three families; HHG-014 stops at high probability with three; eight observed-denial cases and two simulated-confirmation cases use the response-settled condition; three uncertain cases explicitly explain exhausted automated evidence. |

## Interpretation limits and preservation decision

A device profile in this dataset is a description of a device/OS/browser/screen combination, not a unique hardware identifier. HHG-008 (86 groups), HHG-011 (3 groups) and HHG-016 (23 groups) contain only partial profiles; they do not establish a specific shared fraud origin. The saved system therefore does not promote those raw overlaps to R6 or a section 3a connection. R2's literal phrase “shared device profile” could be read more broadly; under that reading more SARs would be recommended. This audit preserves the project's evidence-based interpretation rather than silently changing the definition of a qualifying link or claiming the ambiguity is absent.

HHG-006's current episode contains one customer and one authoritative card. The R9 wording refers to historical cross-customer purchase-splitting precedents, not a demonstrated current ring. Its narrative makes that limit explicit. Removing its SAR would be a real regression: customer denial plus exposure above $1,000 independently requires the report. The unsupported stronger statement “HHG-006 proves a coordinated cross-customer ring” must not appear in demo narration or delivery copy.

No code or answer regeneration was needed to correct these saved action sets. Changing assessment thresholds, forcing a verdict distribution, broadening fraud episodes, reclassifying coarse profiles, or renarrating valid answers would exceed the narrow Phase 1 correction scope and risk the preserved submission.

## Demo recommendation

Use **HHG-014** for graph expansion, L2 routing and a SAR recommendation. Use **HHG-002** for the uncertainty loop: initial probability 0.4335, one supporting family, `VERIFY_WITH_CUSTOMER` and monitoring; then an explicitly simulated customer confirmation, probability 0.04, empty suspicious episode, and `CLOSE_NO_FRAUD`. The two cases demonstrate different capabilities without falsely implying that live customers were contacted or that a regulatory filing occurred.
