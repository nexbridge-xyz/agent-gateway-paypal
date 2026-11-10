"""Append-only audit ledger. Each entry carries the SHA-256 of the one before it,
so changing or deleting any past entry breaks every hash after it."""
from __future__ import annotations

from .util import canonical_bytes, now_iso, sha256_hex

GENESIS = "0" * 64


def entry_hash(entry: dict) -> str:
    body = {k: entry[k] for k in ("seq", "ts", "kind", "summary", "data", "prev")}
    return sha256_hex(canonical_bytes(body))


class Ledger:
    def __init__(self, entries: list):
        # The list is owned by the gateway state, so it is saved with it.
        self.entries = entries

    def append(self, kind: str, summary: str, data: dict) -> dict:
        prev = self.entries[-1]["hash"] if self.entries else GENESIS
        entry = {"seq": len(self.entries) + 1, "ts": now_iso(), "kind": kind,
                 "summary": summary, "data": data, "prev": prev}
        entry["hash"] = entry_hash(entry)
        self.entries.append(entry)
        return entry

    @property
    def head(self) -> str:
        return self.entries[-1]["hash"] if self.entries else GENESIS

    def verify(self) -> dict:
        return verify_entries(self.entries)


def verify_entries(entries: list) -> dict:
    prev = GENESIS
    for i, e in enumerate(entries):
        if e.get("seq") != i + 1:
            return {"ok": False, "count": len(entries), "broken_at": i + 1, "why": "sequence gap"}
        if e.get("prev") != prev:
            return {"ok": False, "count": len(entries), "broken_at": e.get("seq"), "why": "previous hash does not match"}
        try:
            recomputed = entry_hash(e)
        except (KeyError, TypeError, AttributeError):
            return {"ok": False, "count": len(entries), "broken_at": i + 1, "why": "entry is missing fields"}
        if recomputed != e.get("hash"):
            return {"ok": False, "count": len(entries), "broken_at": e.get("seq"), "why": "entry was changed"}
        prev = e["hash"]
    return {"ok": True, "count": len(entries), "head": prev}
