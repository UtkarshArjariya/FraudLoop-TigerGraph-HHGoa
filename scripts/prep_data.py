"""Prepare a bounded, label-free graph extract from the supplied HHGOA CSVs.

Run: python -m scripts.prep_data --source HHGOA_IEEE
Only selected columns are read, in 50,000-row chunks. Known card IDs are mapped
from the explicit historical/benchmark transaction anchors, never guessed.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from agent.store import TransactionStore


TEXT_COLS = ["TransactionID", "customer_id", "ts", "channel", "ProductCD", "card1",
             "card2", "card3", "card4", "card5", "card6", "addr1", "addr2",
             "P_emaildomain", "R_emaildomain", "M4", "M6"]
NUMBER_COLS = ["TransactionDT", "TransactionAmt", "D1", "dist1", "C1", "C2", "C4", "C13", "C14", "risk_score"]
ID_COLS = ["TransactionID", "DeviceInfo", "DeviceType", "id_15", "id_23", "id_30", "id_31", "id_33", "id_34", "id_28", "id_29"]
RENAME = {"TransactionID": "txn_id", "TransactionDT": "txn_seconds", "TransactionAmt": "amount",
          "ProductCD": "product", "addr1": "region", "addr2": "country", "P_emaildomain": "p_email", "R_emaildomain": "r_email"}


def chunks(path: Path):
    dtypes = {col: str for col in TEXT_COLS}
    dtypes.update({col: "float64" if col == "TransactionAmt" else "float32" for col in NUMBER_COLS})
    return pd.read_csv(path, usecols=TEXT_COLS + NUMBER_COLS, dtype=dtypes, chunksize=50_000)


def key(customer: str, card_type: str) -> str:
    return f"{customer}|{card_type}"


def prepare(source: Path, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    pack = pd.read_csv(source / "case_pack.csv", dtype=str).fillna("")
    history = pd.read_csv(source / "closed_cases_history.csv", dtype=str).fillna("")
    identity = pd.concat(pd.read_csv(source / "identity.csv", usecols=ID_COLS,
                                    dtype=str, chunksize=50_000), ignore_index=True).fillna("")
    identity["device_profile"] = identity[["DeviceInfo", "id_30", "id_31", "id_33"]].agg("|".join, axis=1)
    identity.loc[identity.device_profile.eq("|||"), "device_profile"] = ""
    identity = identity.rename(columns={"TransactionID": "txn_id"})
    identity_index = identity.set_index("txn_id")
    anchors: dict[str, str] = {}
    for row in history.itertuples(index=False):
        for txn in row.txn_ids.split("|"):
            if txn:
                if txn in anchors and anchors[txn] != row.card_id:
                    raise ValueError(f"Conflicting authoritative card mapping: {txn}")
                anchors[txn] = row.card_id
    anchors.update(zip(pack.flagged_txn_id, pack.card_id))
    customers = set(pack.customer_id)
    base = []
    total = 0
    for chunk in chunks(source / "transactions.csv"):
        total += len(chunk)
        keep = chunk.customer_id.isin(customers) | chunk.TransactionID.isin(anchors)
        base.append(chunk[keep])
    base = pd.concat(base, ignore_index=True).fillna({col: "" for col in TEXT_COLS})
    card_map: dict[str, str] = {}
    for row in base[base.TransactionID.isin(anchors)].itertuples(index=False):
        signature = key(row.customer_id, row.card6)
        card_id = anchors[row.TransactionID]
        if signature in card_map and card_map[signature] != card_id:
            raise ValueError(f"Ambiguous customer/card-type signature: {signature}; need stronger mapping")
        card_map[signature] = card_id
    target = base[base.customer_id.isin(customers)]
    recent = target[target.ts.ge("2016-11-01")]
    regions = set(recent.addr1) - {""}
    target_devices = set(identity_index.reindex(recent.TransactionID).device_profile.dropna()) - {""}
    selected = []
    neighbor_signatures: set[str] = set()
    for chunk in chunks(source / "transactions.csv"):
        chunk = chunk.fillna({col: "" for col in TEXT_COLS})
        devices = chunk.TransactionID.map(identity_index.device_profile).fillna("")
        neighbor = chunk.ts.ge("2016-11-01") & (chunk.addr1.isin(regions) | devices.isin(target_devices))
        neighbor_signatures.update(key(c, t) for c, t in zip(chunk.loc[neighbor, "customer_id"], chunk.loc[neighbor, "card6"]))
        keep = chunk.customer_id.isin(customers) | chunk.TransactionID.isin(anchors) | neighbor
        selected.append(chunk[keep])
    # Expand reached cards across the exam period so a change of region/device
    # cannot silently cut a connected fraud episode in half.
    selected_ids = {txn for frame in selected for txn in frame.TransactionID}
    for chunk in chunks(source / "transactions.csv"):
        chunk = chunk.fillna({col: "" for col in TEXT_COLS})
        signatures = chunk.customer_id + "|" + chunk.card6
        keep = chunk.ts.ge("2016-11-01") & signatures.isin(neighbor_signatures) & ~chunk.TransactionID.isin(selected_ids)
        selected.append(chunk[keep])
    txns = pd.concat(selected, ignore_index=True).rename(columns=RENAME)
    txns = txns.merge(identity, on="txn_id", how="left", validate="one_to_one")
    txns[identity.columns.drop("txn_id")] = txns[identity.columns.drop("txn_id")].fillna("")
    txns["card_id"] = [card_map.get(key(c, t), "") for c, t in zip(txns.customer_id, txns.card6)]
    # Unknown cards get an internal grouping key, never emitted as dataset card IDs.
    txns["card_key"] = [cid or f"unresolved:{c}:{t}" for cid, c, t in zip(txns.card_id, txns.customer_id, txns.card6)]
    origin_day = np.floor(txns.txn_seconds.astype("float64") / 86400 - txns.D1)
    txns["uid"] = [f"{c}_{r}_{int(d) if math.isfinite(d) else 'unknown'}" for c, r, d in zip(txns.card1, txns.region, origin_day)]
    # pandas 3 uses microsecond-resolution datetime64; explicit unit avoids drift.
    txns["epoch"] = pd.to_datetime(txns.ts).astype("datetime64[ns]").astype("int64") // 10**9
    txns = txns.sort_values(["ts", "txn_id"]).reset_index(drop=True)
    missing = set(anchors) - set(txns.txn_id)
    if missing:
        raise ValueError(f"Missing {len(missing)} case/history transaction IDs")
    for row in pack.itertuples(index=False):
        actual = txns.loc[txns.txn_id.eq(row.flagged_txn_id), "card_id"].item()
        if actual != row.card_id:
            raise ValueError(f"Incorrect flagged card mapping for {row.case_id}")
    store = TransactionStore(txns, history, pack, card_map)
    store.save(output.parent / "transaction_store.pkl")
    txns.to_csv(output / "transactions.csv", index=False)
    cards = txns[["card_key", "card_id", "customer_id"]].drop_duplicates("card_key")
    # Some historical rings name cards without an anchored transaction. Preserve
    # those explicit dataset vertices without inventing their transaction edges.
    referenced_cards = {card for value in history.connected_card_ids for card in value.split("|") if card}
    extra_cards = referenced_cards - set(cards.card_key)
    extra = pd.DataFrame([(card, card, card.rsplit("-K", 1)[0]) for card in sorted(extra_cards)],
                         columns=["card_key", "card_id", "customer_id"])
    cards = pd.concat([cards, extra], ignore_index=True)
    customers_frame = pd.concat([txns[["customer_id"]], cards[["customer_id"]]]).drop_duplicates()
    customers_frame.to_csv(output / "customers.csv", index=False)
    cards.to_csv(output / "cards.csv", index=False)
    txns.loc[txns.device_profile.ne(""), ["device_profile"]].drop_duplicates().to_csv(output / "devices.csv", index=False)
    txns.loc[txns.region.ne(""), ["region"]].drop_duplicates().to_csv(output / "regions.csv", index=False)
    emails = pd.concat([txns.p_email, txns.r_email]).dropna()
    emails[emails.ne("")].drop_duplicates().to_frame("email_domain").to_csv(output / "emails.csv", index=False)
    history.to_csv(output / "closed_cases.csv", index=False)
    next_rows = txns[["card_key", "txn_id"]].copy()
    next_rows["next_txn_id"] = next_rows.groupby("card_key").txn_id.shift(-1)
    next_rows.dropna()[["txn_id", "next_txn_id"]].to_csv(output / "next.csv", index=False)
    involves = [(r.case_id, txn) for r in history.itertuples(index=False) for txn in r.txn_ids.split("|") if txn]
    pd.DataFrame(involves, columns=["case_id", "txn_id"]).to_csv(output / "involves.csv", index=False)
    connected = [(r.case_id, card) for r in history.itertuples(index=False) for card in r.connected_card_ids.split("|") if card]
    pd.DataFrame(connected, columns=["case_id", "card_id"]).to_csv(output / "connected.csv", index=False)
    manifest = {"source_rows": total, "selected_rows": len(txns), "selected_columns": len(TEXT_COLS + NUMBER_COLS),
                "case_count": len(pack), "closed_case_count": len(history), "known_card_count": len(card_map),
                "device_count": int(txns.device_profile[txns.device_profile.ne("")].nunique()),
                "memory_mb": round(txns.memory_usage(deep=True).sum() / 1024**2, 1),
                "mapping": "Authoritative case transaction -> (customer_id, card6); unanchored cards remain unresolved",
                "subset": "All benchmark-customer history; all closed-case transactions; all November-December transactions on reached device/region-neighbor cards",
                "columns": txns.columns.tolist()}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (output / "card_map.json").write_text(json.dumps(card_map, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("HHGOA_IEEE"))
    parser.add_argument("--output", type=Path, default=Path("data/graph"))
    args = parser.parse_args()
    prepare(args.source, args.output)


if __name__ == "__main__":
    main()
