"""Small, local transaction store. Never contains the original fraud labels."""
from __future__ import annotations

import json
import pickle
from pathlib import Path

import pandas as pd


def records(frame: pd.DataFrame) -> list[dict]:
    """Convert pandas values into portable JSON scalars without NaN."""
    return json.loads(frame.to_json(orient="records", date_format="iso"))


class TransactionStore:
    def __init__(self, transactions: pd.DataFrame, history: pd.DataFrame,
                 case_pack: pd.DataFrame, card_map: dict[str, str]):
        self.transactions = transactions
        self.history = history
        self.case_pack = case_pack
        self.card_map = card_map
        self.case_memory: dict[str, dict] = {}
        self.documents: list[dict] = []
        self.embeddings: dict[str, list[float]] = {}

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        with temp.open("wb") as handle:
            pickle.dump(self, handle, protocol=pickle.HIGHEST_PROTOCOL)
        temp.replace(path)

    @classmethod
    def load(cls, path: str | Path = "data/transaction_store.pkl") -> "TransactionStore":
        # Only open the pickle created locally by scripts.prep_data.
        with Path(path).open("rb") as handle:
            return pickle.load(handle)

    def transaction(self, txn_id: str) -> dict:
        rows = self.transactions[self.transactions.txn_id.eq(str(txn_id))]
        if len(rows) != 1:
            raise ValueError(f"Expected one dataset transaction for {txn_id}, got {len(rows)}")
        return records(rows)[0]

    def known_ids(self) -> set[str]:
        ids: set[str] = set()
        for frame, cols in [(self.transactions, ["txn_id", "customer_id", "card_id", "device_profile", "region", "p_email", "r_email"]),
                            (self.history, ["case_id", "card_id", "customer_id"]),
                            (self.case_pack, ["case_id", "card_id", "customer_id"])]:
            for col in cols:
                ids.update(str(x) for x in frame[col].dropna().unique() if str(x))
        for value in self.history.connected_card_ids:
            ids.update(x for x in str(value).split("|") if x)
        return ids

