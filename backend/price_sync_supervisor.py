"""Crash-safe bounded scheduling for the versioned price catalog.

The supervisor contains no transport and never enables network access itself.
It reserves a due run in SQLite and delegates exactly one bounded attempt to a
configured :class:`PriceCatalogService`.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable

from backend.price_catalog_service import PriceCatalogError, PriceCatalogService


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise PriceCatalogError("price_schedule_clock_invalid")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        timezone.utc)


class PriceSyncSupervisor:
    """Persistent due-time, lease, restart and discrepancy coordinator."""

    def __init__(self, service: PriceCatalogService, *,
                 clock: Callable[[], datetime] | None = None):
        self.service = service
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        schedule = service.policy.get("scheduled_sync")
        if not isinstance(schedule, dict):
            raise PriceCatalogError("price_schedule_policy_missing")
        interval = int(schedule.get("interval_seconds", 0))
        minimum = int(schedule.get("minimum_interval_seconds", 0))
        backoff = schedule.get("failure_backoff_seconds")
        lease = int(schedule.get("lease_seconds", 0))
        try:
            threshold = Decimal(str(
                schedule.get("maximum_relative_change_without_alert")))
        except Exception as exc:
            raise PriceCatalogError("price_schedule_policy_invalid") from exc
        if (minimum < 60 or interval < minimum or lease < 1 or lease > 300
                or not isinstance(backoff, list) or not backoff
                or any(not isinstance(item, int) or item < 1 for item in backoff)
                or threshold < 0 or not threshold.is_finite()):
            raise PriceCatalogError("price_schedule_policy_invalid")
        self.policy = schedule
        self.interval = interval
        self.backoff = tuple(backoff)
        self.lease_seconds = lease
        self.change_threshold = threshold
        self._migrate()

    def _migrate(self) -> None:
        with self.service.connect() as db, db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS price_sync_schedule(
              source_id TEXT NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 0,
              state TEXT NOT NULL,
              generation INTEGER NOT NULL,
              consecutive_failures INTEGER NOT NULL,
              next_due_at TEXT,
              lease_id TEXT,
              lease_expires_at TEXT,
              last_started_at TEXT,
              last_completed_at TEXT,
              last_result_code TEXT,
              last_catalog_version TEXT,
              last_change_alert_count INTEGER NOT NULL DEFAULT 0,
              tenant_id TEXT NOT NULL,
              workspace_id TEXT NOT NULL,
              PRIMARY KEY(tenant_id,workspace_id,source_id));
            """)

    def configure(self, source_id: str, *, enabled: bool,
                  first_due_at: datetime | None = None) -> dict[str, Any]:
        self.service.security.authorize(
            "price.sync", required_role="price_sync_service"
        )
        if source_id not in self.service.policy["allowed_sources"]:
            raise PriceCatalogError("price_source_not_allowed")
        now = _utc(self.clock())
        due = _utc(first_due_at) if first_due_at is not None else now
        with self.service.connect() as db, db:
            prior = db.execute(
                """SELECT generation FROM price_sync_schedule WHERE source_id=?
                   AND tenant_id=? AND workspace_id=?""",
                (source_id, *self.service.scope.sql_parameters())).fetchone()
            generation = int(prior["generation"]) + 1 if prior else 1
            db.execute("""INSERT INTO price_sync_schedule(
              source_id,enabled,state,generation,consecutive_failures,next_due_at,
              tenant_id,workspace_id)
              VALUES(?,?,?,?,0,?,?,?)
              ON CONFLICT(tenant_id,workspace_id,source_id) DO UPDATE SET
              enabled=excluded.enabled,state=excluded.state,
              generation=excluded.generation,consecutive_failures=0,
              next_due_at=excluded.next_due_at,lease_id=NULL,lease_expires_at=NULL""",
              (source_id, int(enabled), "waiting" if enabled else "disabled",
               generation, _iso(due) if enabled else None,
               *self.service.scope.sql_parameters()))
        return self.status(source_id)

    def status(self, source_id: str) -> dict[str, Any]:
        self.service.security.authorize("price.read")
        with self.service.connect() as db:
            row = db.execute(
                """SELECT * FROM price_sync_schedule WHERE source_id=?
                   AND tenant_id=? AND workspace_id=?""",
                (source_id, *self.service.scope.sql_parameters())).fetchone()
        if row is None:
            return {"source_id": source_id, "enabled": False,
                    "state": "never_configured", "network_called": False}
        item = dict(row)
        item["enabled"] = bool(item["enabled"])
        item["network_called"] = False
        item.pop("lease_id", None)
        return item

    def _reserve(self, source_id: str, *, force: bool) -> tuple[str, str] | None:
        self.service.security.authorize(
            "price.sync", required_role="price_sync_service"
        )
        now = _utc(self.clock())
        lease_id = "PSL-" + uuid.uuid4().hex.upper()
        with self.service.connect() as db, db:
            row = db.execute(
                """SELECT * FROM price_sync_schedule WHERE source_id=?
                   AND tenant_id=? AND workspace_id=?""",
                (source_id, *self.service.scope.sql_parameters())).fetchone()
            if row is None or not bool(row["enabled"]):
                return None
            lease_live = (row["lease_expires_at"] is not None
                          and _parse(row["lease_expires_at"]) > now)
            if lease_live:
                return None
            if not force and (not row["next_due_at"]
                              or _parse(row["next_due_at"]) > now):
                return None
            generation = str(row["generation"])
            updated = db.execute("""UPDATE price_sync_schedule SET
              state='running',lease_id=?,lease_expires_at=?,last_started_at=?
              WHERE source_id=? AND generation=? AND tenant_id=? AND workspace_id=? AND
              (lease_expires_at IS NULL OR lease_expires_at<=?)""", (
                lease_id, _iso(now + timedelta(seconds=self.lease_seconds)),
                _iso(now), source_id, int(generation),
                *self.service.scope.sql_parameters(), _iso(now))).rowcount
            if updated != 1:
                return None
        return lease_id, generation

    def _change_alert_count(self, previous: str | None,
                            current: str | None) -> int:
        if not previous or not current or previous == current:
            return 0
        with self.service.connect() as db:
            old = db.execute("""SELECT environment_id,model_id,channel_id,currency,
              input_unit_price,output_unit_price FROM price_catalog_records
              WHERE catalog_version=? AND tenant_id=? AND workspace_id=?""",
              (previous, *self.service.scope.sql_parameters())).fetchall()
            new = db.execute("""SELECT environment_id,model_id,channel_id,currency,
              input_unit_price,output_unit_price FROM price_catalog_records
              WHERE catalog_version=? AND tenant_id=? AND workspace_id=?""",
              (current, *self.service.scope.sql_parameters())).fetchall()
        def keyed(rows):
            return {tuple(row[key] for key in (
                "environment_id", "model_id", "channel_id", "currency")): row
                    for row in rows}
        before, after = keyed(old), keyed(new)
        alerts = len(set(before) ^ set(after))
        for key in set(before) & set(after):
            for field in ("input_unit_price", "output_unit_price"):
                left, right = Decimal(before[key][field]), Decimal(after[key][field])
                relative = Decimal("Infinity") if left == 0 and right != 0 else (
                    Decimal(0) if left == right else abs(right - left) / left)
                if relative > self.change_threshold:
                    alerts += 1
        return alerts

    def run_due(self, source_id: str, *, allow_network: bool = False,
                force: bool = False) -> dict[str, Any]:
        self.service.security.authorize(
            "price.sync", required_role="price_sync_service"
        )
        reservation = self._reserve(source_id, force=force)
        if reservation is None:
            return {"status": "not_due_or_unavailable", "source_id": source_id,
                    "network_called": False}
        lease_id, generation = reservation
        previous = self.service.catalog_status(source_id).get("catalog_version")
        result = self.service.synchronize(source_id, allow_network=allow_network)
        now = _utc(self.clock())
        success = result.get("status") == "ready"
        current = result.get("catalog_version") if success else previous
        alerts = self._change_alert_count(previous, current) if success else 0
        with self.service.connect() as db, db:
            row = db.execute("SELECT consecutive_failures FROM price_sync_schedule "
                             "WHERE source_id=? AND generation=? AND lease_id=? "
                             "AND tenant_id=? AND workspace_id=?",
                             (source_id, int(generation), lease_id,
                              *self.service.scope.sql_parameters())).fetchone()
            if row is None:
                raise PriceCatalogError("price_schedule_generation_lost")
            failures = 0 if success else int(row["consecutive_failures"]) + 1
            delay = self.interval if success else self.backoff[
                min(failures - 1, len(self.backoff) - 1)]
            db.execute("""UPDATE price_sync_schedule SET state=?,
              consecutive_failures=?,next_due_at=?,lease_id=NULL,
              lease_expires_at=NULL,last_completed_at=?,last_result_code=?,
              last_catalog_version=?,last_change_alert_count=?
              WHERE source_id=? AND generation=? AND lease_id=?
                AND tenant_id=? AND workspace_id=?""", (
                "waiting" if success else "backoff", failures,
                _iso(now + timedelta(seconds=delay)), _iso(now),
                "ready" if success else str(result.get("reason") or "failed"),
                current, alerts, source_id, int(generation), lease_id,
                *self.service.scope.sql_parameters()))
        return {**result, "scheduled": True, "change_alert_count": alerts,
                "next_due_at": _iso(now + timedelta(seconds=delay))}

    def run_pending(self, *, allow_network: bool = False,
                    maximum_sources: int = 20) -> dict[str, Any]:
        """Run each due configured source once, with a deterministic hard bound.

        This is the scheduler-facing tick used by a background loop. Capacity is
        still reserved transactionally by :meth:`run_due`; a disabled, leased,
        or not-yet-due source performs no transport call.
        """
        self.service.security.authorize(
            "price.sync", required_role="price_sync_service")
        if (isinstance(maximum_sources, bool) or not isinstance(maximum_sources, int)
                or not 1 <= maximum_sources <= 100):
            raise PriceCatalogError("price_schedule_limit_invalid")
        now = _iso(_utc(self.clock()))
        with self.service.connect() as db:
            rows = db.execute(
                """SELECT source_id FROM price_sync_schedule
                   WHERE enabled=1 AND next_due_at IS NOT NULL AND next_due_at<=?
                     AND tenant_id=? AND workspace_id=?
                   ORDER BY next_due_at,source_id LIMIT ?""",
                (now, *self.service.scope.sql_parameters(), maximum_sources),
            ).fetchall()
        results = [
            self.run_due(str(row["source_id"]), allow_network=allow_network)
            for row in rows
        ]
        return {
            "status": "ready", "due_count": len(rows),
            "attempted_count": sum(
                1 for item in results if item.get("status") != "not_due_or_unavailable"),
            "results": results,
            "network_called": any(bool(item.get("network_called")) for item in results),
        }
