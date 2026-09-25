"""Bounded stream input and transparent alert scoring, separate from case policy."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import math
from pathlib import Path

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.data import atomic_json, csv_rows, epoch, read_json


class DetectorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)
    threshold: float = Field(.6, ge=0, le=1)
    high_risk_score: float = Field(.7, ge=0, le=1)
    medium_risk_score: float = Field(.3, ge=0, le=1)
    velocity_count: int = Field(5, ge=2, le=100)
    velocity_window_minutes: int = Field(60, ge=1, le=1440)
    card_testing_count: int = Field(3, ge=2, le=20)
    small_amount: float = Field(5, ge=.01, le=100)
    new_region_days: int = Field(183, ge=1, le=183)

    @model_validator(mode="after")
    def bands(self):
        if self.medium_risk_score >= self.high_risk_score:
            raise ValueError("medium_risk_score must be below high_risk_score")
        return self


class DetectorSettings:
    def __init__(self, root):
        self.path = Path(root) / "data/detector_config.json"

    def get(self):
        return DetectorConfig.model_validate(read_json(self.path, {}))

    def update(self, values):
        config = DetectorConfig.model_validate({**self.get().model_dump(), **values})
        atomic_json(self.path, config.model_dump())
        return config


# These are the existing prepared Txn attributes, never the raw 397-column input.
TXN_COLUMNS = ("txn_id", "txn_seconds", "amount", "product", "card1", "card2", "card3", "card4", "card5", "card6",
    "region", "country", "dist1", "p_email", "r_email", "C1", "C2", "C4", "C13", "C14", "D1", "M4", "M6",
    "customer_id", "ts", "channel", "risk_score", "id_15", "id_23", "id_28", "id_29", "id_30", "id_31", "id_33",
    "id_34", "DeviceType", "DeviceInfo", "device_profile", "card_id", "card_key", "uid", "epoch")
CONTEXT_COLUMNS = ("txn_id", "epoch", "amount", "ts", "card_id", "card_key", "customer_id", "device_profile", "region", "channel")


def timestamp(value):
    parsed = pd.Timestamp(value)
    return parsed.tz_localize("UTC") if parsed.tzinfo is None else parsed.tz_convert("UTC")


@dataclass
class PreparedSource:
    rows: pd.DataFrame
    context: pd.DataFrame
    history: list[dict]
    start: str
    end: str

    def __post_init__(self):
        self.cards = self.context.groupby("card_id", sort=False).indices
        self.devices = self.context.groupby("device_profile", sort=False).indices
        self.txn_cases = defaultdict(set)
        self.closed_cases = {}
        for row in self.history:
            try:
                closed = epoch(row["closed_at"])
            except (ValueError, KeyError, TypeError):
                continue
            if row.get("outcome") != "confirmed_fraud":
                continue
            case = {**row, "closed_epoch": closed}
            self.closed_cases[row["case_id"]] = case
            for txn_id in (str(row.get("txn_ids", "")) + "|" + str(row.get("first_fraud_txn_id", ""))).split("|"):
                if txn_id:
                    self.txn_cases[txn_id].add(row["case_id"])

    @classmethod
    def load(cls, repository, *, chunk_size=10000):
        path = repository.data / "graph/transactions.csv"
        maximum = None
        for chunk in pd.read_csv(path, usecols=["epoch"], chunksize=chunk_size):
            value = pd.to_numeric(chunk.epoch, errors="raise").max()
            maximum = value if maximum is None else max(maximum, value)
        if maximum is None or not math.isfinite(float(maximum)):
            raise ValueError("Prepared transaction data is empty or has invalid timestamps.")
        end = pd.Timestamp(int(maximum), unit="s", tz="UTC")
        start = end - pd.DateOffset(months=2)
        lower = int(start.timestamp())
        frames, context = [], []
        for chunk in pd.read_csv(path, usecols=lambda name: name in TXN_COLUMNS, dtype=str,
                                 keep_default_na=False, chunksize=chunk_size):
            for column in TXN_COLUMNS:
                if column not in chunk:
                    chunk[column] = ""
            chunk["epoch"] = pd.to_numeric(chunk.epoch, errors="raise").astype("int64")
            chunk["amount"] = pd.to_numeric(chunk.amount, errors="raise")
            # Retain only narrow historical context; full attributes are held
            # only for the final two calendar months that can be replayed.
            context.append(chunk.loc[:, list(CONTEXT_COLUMNS)].copy())
            selected = chunk.loc[chunk.epoch.ge(lower), list(TXN_COLUMNS)]
            if len(selected):
                frames.append(selected.copy())
        rows = pd.concat(frames, ignore_index=True).sort_values(["epoch", "txn_id"]).reset_index(drop=True)
        narrow = pd.concat(context, ignore_index=True)
        if narrow.txn_id.duplicated().any():
            raise ValueError("Prepared transaction IDs must be unique.")
        return cls(rows, narrow, list(csv_rows(repository.root / "HHGOA_IEEE/closed_cases_history.csv")),
                   start.isoformat(), end.isoformat())

    def offset(self, start_date=None):
        date = timestamp(start_date or self.start)
        if date < timestamp(self.start) or date > timestamp(self.end):
            raise ValueError("start_date must be within the final two months of the supplied data.")
        return int(self.rows.epoch.searchsorted(int(date.timestamp()), side="left")), date.isoformat()

    def local_queries(self, row, config):
        cutoff = int(row["epoch"])
        card = str(row.get("card_id", ""))
        indices = self.cards.get(card, []) if card and not card.startswith("unresolved:") else []
        card_rows = self.context.iloc[indices]
        window = card_rows[card_rows.epoch.between(cutoff - config.velocity_window_minutes * 60, cutoff)]
        baseline = card_rows[card_rows.epoch.ge(cutoff - config.new_region_days * 86400) & card_rows.epoch.lt(cutoff)]
        profile = str(row.get("device_profile", ""))
        indices = self.devices.get(profile, []) if profile and profile != "|||" else []
        device_rows = self.context.iloc[indices]
        ring = device_rows[device_rows.epoch.between(cutoff - 7 * 86400, cutoff)]
        related = {cid for txn in ring.txn_id for cid in self.txn_cases.get(str(txn), ())}
        cases = [self.closed_cases[cid] for cid in sorted(related)
                 if cutoff - 7 * 86400 <= self.closed_cases[cid]["closed_epoch"] <= cutoff]
        return {"window": window.to_dict("records"), "baseline": baseline.to_dict("records"),
                "ring": ring.to_dict("records"), "cases": cases}


def score_transaction(row, context, config: DetectorConfig):
    """Produce a trigger score; this never determines case verdicts or actions."""
    cutoff = int(row["epoch"])
    risk = float(row.get("risk_score") or 0)
    risk = max(0., min(1., risk)) if math.isfinite(risk) else 0.
    band = "high" if risk >= config.high_risk_score else "medium" if risk >= config.medium_risk_score else "low"
    risk_part = {"high": .25, "medium": .1, "low": 0.}[band]
    reasons = [f"Recorded risk score is in the {band} band ({risk:.2f}); it is only an alert signal."] if risk_part else []
    window = [item for item in context.get("window", [])
              if cutoff - config.velocity_window_minutes * 60 <= int(item["epoch"]) <= cutoff]
    # Include the current supplied transaction once even if a graph write could
    # not finish. Duplicate query rows cannot inflate velocity.
    unique = {str(item["txn_id"]): item for item in window}
    unique[str(row["txn_id"])] = row
    window = list(unique.values())
    small = [item for item in window if str(item["txn_id"]) != str(row["txn_id"])
             and int(item["epoch"]) < cutoff
             and item.get("channel") == "online" and abs(float(item["amount"])) < config.small_amount]
    testing = len(small) >= config.card_testing_count and abs(float(row["amount"])) >= config.small_amount and row.get("channel") == "online"
    velocity = len(window) >= config.velocity_count
    behavior = .6 if testing else .45 if velocity else 0.
    if testing:
        reasons.append(f"Card-testing sequence: {len(small)} earlier online authorizations below ${config.small_amount:g} precede this larger purchase.")
    elif velocity:
        reasons.append(f"Card velocity: {len(window)} transactions in {config.velocity_window_minutes} minutes.")
    profile = str(row.get("device_profile", ""))
    complete = len(profile.split("|")) == 4 and all(profile.split("|"))
    ring_ids = {str(item["txn_id"]) for item in context.get("ring", [])
                if cutoff - 7 * 86400 <= int(item["epoch"]) <= cutoff and str(item.get("card_id", "")) != str(row.get("card_id", ""))}
    confirmed = []
    for case in context.get("cases", []):
        try:
            closed = int(case["closed_epoch"]) if "closed_epoch" in case else epoch(case["closed_at"])
        except (ValueError, KeyError, TypeError):
            continue
        txn_ids = set((str(case.get("txn_ids", "")) + "|" + str(case.get("first_fraud_txn_id", ""))).split("|"))
        if (complete and case.get("outcome") == "confirmed_fraud" and cutoff - 7 * 86400 <= closed <= cutoff
                and str(case.get("card_id", "")) != str(row.get("card_id", "")) and txn_ids & ring_ids):
            confirmed.append(str(case["case_id"]))
    if confirmed:
        reasons.append("The complete device profile links another card to confirmed fraud closed within seven days: " + ", ".join(sorted(set(confirmed))) + ".")
    baseline = [item for item in context.get("baseline", [])
                if cutoff - config.new_region_days * 86400 <= int(item["epoch"]) < cutoff]
    known_regions = {str(item.get("region", "")) for item in baseline} - {""}
    new_region = bool(row.get("region") and known_regions and str(row["region"]) not in known_regions)
    if new_region:
        reasons.append(f"Billing region is new compared with recorded card history in the previous {config.new_region_days} days.")
    contributions = {"risk_band": risk_part, "card_behavior": behavior,
                     "confirmed_device_history": .5 if confirmed else 0., "new_region": .15 if new_region else 0.}
    score = round(min(1., sum(contributions.values())), 4)
    return {"alert_score": score, "threshold": config.threshold, "alert": score >= config.threshold,
            "risk_band": band, "reasons": reasons or ["No configured detector signal exceeded its evidence condition."],
            "contributions": contributions, "checks": {"velocity_count": len(window), "card_testing": testing,
                "confirmed_case_ids": sorted(set(confirmed)), "new_region": new_region, "baseline_count": len(baseline)},
            "disclosure": "Detector score opens a review only. Policy and case evidence independently decide the outcome."}
