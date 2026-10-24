# -*- coding: utf-8 -*-
"""Loads specs/contract.yaml. The ONLY place the contract is read from disk.

Everything business-related -- FX rates, list prices, status spellings, dedup
precedence, quality thresholds -- comes from here. No module under app/ may
hardcode any of it, and evals/test_contract_drift.py fails the build if one does.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent


def contract_path() -> Path:
    """Overridable so a client can point at their own contract without a code change."""
    return Path(os.environ.get("CLOUDNOVA_CONTRACT", ROOT / "specs" / "contract.yaml"))


class Contract:
    def __init__(self, raw: dict[str, Any], path: Path) -> None:
        self._raw = raw
        self.path = path

    @classmethod
    def load(cls, path: Path | None = None) -> "Contract":
        p = Path(path) if path else contract_path()
        if not p.is_file():
            raise FileNotFoundError("contract not found: %s" % p)
        with open(p, encoding="utf-8") as fh:
            return cls(yaml.safe_load(fh), p)

    # -- plain accessors -----------------------------------------------------
    @property
    def version(self) -> str:
        return str(self._raw.get("contract_version", "0"))

    @property
    def list_price(self) -> dict[str, float]:
        return {k: float(v) for k, v in
                self._raw["pricing"]["list_price_per_seat_per_month_usd"].items()}

    @property
    def fx(self) -> dict[str, float]:
        return {k: float(v) for k, v in self._raw["fx_to_usd"].items()}

    @property
    def annual_months(self) -> int:
        return int(self._raw["pricing"]["annual_months_billed"])

    @property
    def date_formats(self) -> list[str]:
        return list(self._raw["date_formats"])

    @property
    def recognised_status(self) -> list[str]:
        return list(self._raw["revenue"]["recognised_status"])

    @property
    def netting_status(self) -> list[str]:
        return list(self._raw["revenue"]["netting_status"])

    @property
    def status_precedence(self) -> list[str]:
        return list(self._raw["deduplication"]["status_precedence"])

    @property
    def gold_tables(self) -> list[dict[str, str]]:
        return list(self._raw["gold_tables"])

    @property
    def checks(self) -> list[dict[str, Any]]:
        return list(self._raw["quality_gate"]["checks"])

    @property
    def quarantine_reasons(self) -> list[str]:
        return [q["reason"] for q in self._raw["quarantine_when"]]

    def threshold(self, check_id: str) -> float:
        for c in self.checks:
            if c["id"] == check_id:
                return float(c["threshold"])
        raise KeyError("no such check: %s" % check_id)

    def mapping(self, field: str) -> dict[str, str]:
        """Flatten {canonical: [variants]} into {variant_lowercased: canonical}.

        Lower-cased and stripped, so ' Starter ', 'STARTER' and 'starter' all
        collapse. A variant NOT present here yields no canonical value, and the
        row is quarantined rather than guessed at.
        """
        out: dict[str, str] = {}
        for canonical, variants in self._raw["normalise"][field].items():
            for v in variants:
                out[str(v).strip().lower()] = str(canonical)
        return out

    def sql_case(self, field: str, column: str) -> str:
        """Build a CASE expression from the mapping, so DuckDB does the work."""
        whens = []
        for variant, canonical in sorted(self.mapping(field).items()):
            whens.append("WHEN lower(trim(%s)) = %s THEN %s"
                         % (column, _lit(variant), _lit(canonical)))
        return "CASE %s ELSE NULL END" % " ".join(whens)

    def sql_map(self, mapping: dict[str, float], column: str) -> str:
        whens = ["WHEN %s = %s THEN %r" % (column, _lit(k), float(v))
                 for k, v in sorted(mapping.items())]
        return "CASE %s ELSE NULL END" % " ".join(whens)


def _lit(value: str) -> str:
    """Single-quoted SQL literal with quotes escaped."""
    return "'" + str(value).replace("'", "''") + "'"
