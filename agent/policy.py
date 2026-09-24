"""Pure recommendations under HHGOA_IEEE/README.md Fraud Policy v1.0.

The caller establishes facts and records evidence; this module never executes
bank actions. In particular, ``recurring_match`` means an established monthly
same-merchant/same-amount pattern, not merely a repeated product code.
"""

from dataclasses import dataclass
from math import isfinite


ACTIONS = frozenset({
    "ALLOW_TRANSACTION", "DECLINE_TRANSACTION", "MONITOR_CARD",
    "MONITOR_CONNECTED_CARDS", "WARN_CUSTOMER", "VERIFY_WITH_CUSTOMER",
    "STEP_UP_AUTH", "BLOCK_CARD", "BLOCK_ALL_CARDS", "GENERATE_REPORT",
    "CREATE_CASE", "FILE_REPORT", "ESCALATE_TO_ANALYST", "CLOSE_NO_FRAUD",
})

# Open the record, contain a pending authorization, obtain evidence, then
# recommend longer-lived protection and reporting. Verification precedes blocks.
_ACTION_ORDER = (
    "CREATE_CASE", "DECLINE_TRANSACTION", "VERIFY_WITH_CUSTOMER", "STEP_UP_AUTH",
    "BLOCK_CARD", "BLOCK_ALL_CARDS", "MONITOR_CARD", "MONITOR_CONNECTED_CARDS",
    "WARN_CUSTOMER", "FILE_REPORT", "ESCALATE_TO_ANALYST", "CLOSE_NO_FRAUD",
    "ALLOW_TRANSACTION", "GENERATE_REPORT",
)


@dataclass(frozen=True)
class PolicyContext:
    probability: float
    verdict: str
    exposure: float
    evidence_families: int = 0
    customer_response: str = ""
    disputed: bool = False
    recurring_match: bool = False
    card_testing: bool = False
    cleared_purchase_over_100: bool = False
    pending_authorization: bool = False
    shared_fraud: bool = False
    connected_cards: tuple[str, ...] = ()
    connected_group_count: int = 0
    unresolved_connected_groups: int = 0
    unresolved_customer_ids: tuple[str, ...] = ()
    undocumented: bool = False
    evidence_conflicts: bool = False
    request_type: str = ""
    confirmed_customer_cards: int = 0
    credentials_compromised: bool = False
    case_opened: bool = False

    def __post_init__(self) -> None:
        if not isfinite(self.probability) or not 0 <= self.probability <= 1:
            raise ValueError("probability must be finite and between 0 and 1")
        _validate_exposure(self.exposure)
        if self.verdict not in {"fraud", "legitimate", "uncertain"}:
            raise ValueError("invalid verdict")
        if self.evidence_families < 0 or self.confirmed_customer_cards < 0:
            raise ValueError("evidence and confirmed-card counts cannot be negative")
        if self.customer_response not in {"", "denied", "confirmed", "no_reply_24h"}:
            raise ValueError("invalid customer_response")
        if self.request_type not in {"", "customer_validation", "step_up_auth", "analyst_info"}:
            raise ValueError("invalid request_type")


def _validate_exposure(exposure: float) -> None:
    if not isfinite(exposure) or exposure < 0:
        raise ValueError("exposure must be finite and nonnegative")


def route_for(action: str, exposure: float) -> str:
    """Return the required approval route; unknown actions fail closed."""
    if action not in ACTIONS:
        raise ValueError(f"unknown policy action: {action}")
    _validate_exposure(exposure)
    if action in {"BLOCK_ALL_CARDS", "FILE_REPORT"}:
        return "L2"
    if action == "BLOCK_CARD":
        return "L1" if exposure <= 2500 else "L2"
    if action == "DECLINE_TRANSACTION":
        return "L1"
    return "auto"


def recommend(ctx: PolicyContext) -> list[dict[str, str]]:
    """Recommend one evidence stage; invoke again after simulated responses.

    ``shared_fraud`` and ``undocumented`` are caller-established findings of
    fraud across cards or coordinated/repeated abuse, respectively. Mere device
    overlap, missing profiles, or an unfamiliar product do not establish them.
    """
    reasons: dict[str, list[str]] = {}

    def add(action: str, reason: str) -> None:
        action_reasons = reasons.setdefault(action, [])
        if reason not in action_reasons:
            action_reasons.append(reason)

    recurring_dispute = ctx.disputed and ctx.recurring_match
    confirmed = ctx.customer_response == "confirmed"
    legitimate = ctx.verdict == "legitimate" or confirmed or recurring_dispute
    denied = ctx.customer_response == "denied" and not legitimate
    weak_signal = ctx.evidence_families <= 1 and ctx.probability < 0.70

    if ctx.probability >= 0.30 or ctx.disputed or ctx.request_type or ctx.case_opened:
        add("CREATE_CASE", "Section 3a: probability is at least 0.30, evidence is requested, a charge is disputed, or the investigation record is already open.")

    if recurring_dispute:
        add("CREATE_CASE", "R7: preserve the recurring-charge dispute as an internal case.")
        if not confirmed:
            add("VERIFY_WITH_CUSTOMER", "R7: verify the established same-merchant, same-amount monthly charge; do not block.")
        add("WARN_CUSTOMER", "R7: explain the recurring charge to the cardholder.")

    # A response resolves this request stage. Do not repeatedly request the
    # same evidence after a denial, confirmation, or a 24-hour timeout.
    if ctx.request_type and not ctx.customer_response:
        action = {
            "customer_validation": "VERIFY_WITH_CUSTOMER",
            "step_up_auth": "STEP_UP_AUTH",
            "analyst_info": "ESCALATE_TO_ANALYST",
        }[ctx.request_type]
        add(action, "Section 5 and section 3b: obtain the requested evidence before updating the recommendation.")
    if weak_signal and not legitimate and not ctx.customer_response:
        action = "STEP_UP_AUTH" if ctx.request_type == "step_up_auth" else "VERIFY_WITH_CUSTOMER"
        add(action, "R1: probability below 0.70 on at most one independent signal requires verification before any block.")

    if ctx.customer_response == "no_reply_24h" and not legitimate:
        add("MONITOR_CARD", "R4: no reply within 24 hours; monitor the active card for 72 hours.")
        if ctx.pending_authorization:
            add("DECLINE_TRANSACTION", "R4: decline pending authorizations after 24 hours without a reply.")
        if ctx.exposure > 500:
            add("ESCALATE_TO_ANALYST", "R4: no reply within 24 hours and exposure exceeds $500.")

    if not legitimate:
        if denied:
            add("BLOCK_CARD", "R2: the customer denies the transaction; recommend blocking and reissuing this card.")
            add("CREATE_CASE", "R2: retain the denied transaction and investigation evidence in a case.")
        if ctx.card_testing:
            add("DECLINE_TRANSACTION", "R5: at least three small online authorizations within one hour were followed by a larger purchase.")
            add("STEP_UP_AUTH", "R5: require step-up authentication after the observed card-testing sequence.")
            if ctx.cleared_purchase_over_100:
                add("BLOCK_CARD", "R5: a purchase over $100 has already cleared following card testing.")
        if ctx.verdict == "fraud" and not weak_signal:
            add("BLOCK_CARD", "R1 and section 2: the assessed fraud is not a weak signal requiring verification before blocking; apply the exposure-based approval route.")

        if ctx.confirmed_customer_cards >= 2 or ctx.credentials_compromised:
            # R10 supplies confirmed compromise independently of an alert's
            # score. One wider block supersedes the narrower recommendation.
            narrower = reasons.pop("BLOCK_CARD", [])
            for reason in narrower:
                add("BLOCK_ALL_CARDS", reason)
            add("BLOCK_ALL_CARDS", "R10: at least two customer cards have confirmed fraud or the customer's credentials are confirmed compromised; section 2 requires L2 approval.")

        strongly_suspected = ctx.probability >= 0.70 and ctx.evidence_families >= 2
        fraud_supported = denied or ctx.verdict == "fraud" or strongly_suspected
        if ctx.shared_fraud:
            add("CREATE_CASE", "R6: the evidence identifies fraud across cards from a shared origin.")
            add("FILE_REPORT", "R6 and section 3a: shared-origin fraud requires a suspicious activity report regardless of amount.")
            cards = ", ".join(sorted(set(ctx.connected_cards))) or "every card identified as sharing the origin"
            mapping = (f" Also monitor all {ctx.connected_group_count} origin-linked card groups, including {ctx.unresolved_connected_groups} unresolved groups "
                       f"referenced by customers {', '.join(ctx.unresolved_customer_ids)}; the analyst must map their card IDs from the cited transactions."
                       if ctx.unresolved_connected_groups else "")
            add("MONITOR_CONNECTED_CARDS", f"R6: monitor all connected cards: {cards}." + mapping)
        if ctx.undocumented:
            add("CREATE_CASE", "R9: preserve the coordinated or repeated abuse outside documented typologies.")
            add("FILE_REPORT", "R9 and section 3a: describe the supported undocumented pattern in a suspicious activity report.")
            add("ESCALATE_TO_ANALYST", "R9: an analyst must review the undocumented pattern.")
        if fraud_supported and ctx.exposure > 1000:
            rule = "R2 and section 3a" if denied else "Section 3a"
            add("FILE_REPORT", f"{rule}: confirmed or strongly suspected fraud has exposure exceeding $1,000.")

    if (ctx.verdict == "uncertain" and ctx.exposure > 500) or ctx.evidence_conflicts:
        add("ESCALATE_TO_ANALYST", "R8: the verdict is uncertain with exposure exceeding $500, or the evidence conflicts.")

    if confirmed:
        add("CLOSE_NO_FRAUD", "R3: the customer confirmed the transaction; record the confirmation and close as legitimate.")
    elif ctx.verdict == "legitimate" and not ctx.evidence_conflicts and not recurring_dispute:
        add("CLOSE_NO_FRAUD", "Sections 1 and 6: the assessed activity is legitimate; close the alert with its supporting evidence.")
    elif ctx.verdict == "uncertain" and not recurring_dispute:
        add("MONITOR_CARD", "Sections 1 and 3b: keep the card active under 72-hour monitoring while the uncertain investigation is resolved.")

    # Section 3a applies equally to evidence requests introduced by R1/R7 and
    # reports introduced by a rule even when the initial probability was low.
    if any(action in reasons for action in ("VERIFY_WITH_CUSTOMER", "STEP_UP_AUTH", "FILE_REPORT")):
        add("CREATE_CASE", "Section 3a: an evidence request or regulatory report must have an internal case behind it.")

    return [
        {"action": action, "route": route_for(action, ctx.exposure), "reason": " ".join(reasons[action])}
        for action in _ACTION_ORDER if action in reasons
    ]


def stopping_reason(ctx: PolicyContext, verified: bool = False) -> str | None:
    """Return a policy-supported stop, or None when more work may be useful.

    The caller must separately record explicit exhaustion of useful evidence.
    ``verified`` means a response settled the question, not simply that any
    response arrived; conflicting evidence must not be passed as settled.
    """
    if verified:
        return "Section 6: a verification response settled the question."
    if ctx.evidence_conflicts:
        return None
    if ctx.evidence_families >= 2 and ctx.probability >= 0.85:
        return "Section 6: fraud probability is at least 0.85 with at least two independent evidence families."
    if ctx.evidence_families >= 2 and ctx.probability <= 0.15:
        return "Section 6: fraud probability is at most 0.15 with at least two independent evidence families."
    return None
