"""Transactional SQLite tenant/workspace migration for the enterprise console.

Version 2 closes the legacy-schema gap left by the additive v1 migration.  It
uses shadow tables to scope business primary/unique keys and foreign keys, and
rewrites explicit (including partial/expression) unique indexes.  Integer
``PRIMARY KEY`` columns remain global surrogate row identifiers because SQLite
cannot retain AUTOINCREMENT semantics in a composite primary key; they do not
prevent tenant-local business identifiers from coexisting and are reported.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable, Iterable
import uuid

from backend.tenant_security import (
    DEFAULT_DEVELOPMENT_TENANT_ID,
    DEFAULT_DEVELOPMENT_WORKSPACE_ID,
)


MIGRATION_VERSION = 2
POLICY_PATH = Path(__file__).resolve().parents[1] / "config" / "enterprise_tenant_policy_v1.json"
_SAFE_TABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_SCOPE = ("tenant_id", "workspace_id")
_MIGRATION_TABLES = {
    "enterprise_schema_migrations",
    "enterprise_tenant_migration_runs",
    "enterprise_tenant_migration_quarantine",
    "enterprise_tenants",
    "enterprise_workspaces",
}


class TenantMigrationError(RuntimeError):
    """A stable, non-sensitive migration error code."""


class ConstraintRewriteError(TenantMigrationError):
    def __init__(self, table: str, code: str):
        super().__init__(code)
        self.table = table
        self.code = code


@dataclass(frozen=True, slots=True)
class MigrationPolicy:
    schema_version: str
    migration_version: int
    default_tenant_id: str
    default_workspace_id: str
    tenant_owned_tables: frozenset[str]
    system_tables: frozenset[str]

    @classmethod
    def load(cls, path: Path = POLICY_PATH) -> "MigrationPolicy":
        raw = json.loads(path.read_text(encoding="utf-8"))
        required = {
            "schema_version", "migration_version", "default_tenant_id",
            "default_workspace_id", "tenant_owned_tables", "system_tables",
        }
        if set(raw) < required:
            raise TenantMigrationError("enterprise_tenant_policy_incomplete")
        if raw["schema_version"] != "enterprise_tenant_policy_v1":
            raise TenantMigrationError("enterprise_tenant_policy_version_invalid")
        if int(raw["migration_version"]) != MIGRATION_VERSION:
            raise TenantMigrationError("enterprise_tenant_migration_version_invalid")
        if (raw["default_tenant_id"] != DEFAULT_DEVELOPMENT_TENANT_ID or
                raw["default_workspace_id"] != DEFAULT_DEVELOPMENT_WORKSPACE_ID):
            raise TenantMigrationError("enterprise_tenant_defaults_invalid")
        owned = frozenset(_safe_name(item) for item in raw["tenant_owned_tables"])
        system = frozenset(_safe_name(item) for item in raw["system_tables"])
        if owned & system:
            raise TenantMigrationError("enterprise_tenant_policy_overlap")
        return cls(raw["schema_version"], int(raw["migration_version"]),
                   raw["default_tenant_id"], raw["default_workspace_id"], owned, system)


def _safe_name(value: Any) -> str:
    if not isinstance(value, str) or not _SAFE_TABLE.fullmatch(value):
        raise TenantMigrationError("unsafe_sql_identifier")
    return value


def _quoted(name: str) -> str:
    return '"' + _safe_name(name) + '"'


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _split_sql_list(value: str) -> list[str]:
    parts: list[str] = []
    start = depth = 0
    quote: str | None = None
    index = 0
    while index < len(value):
        char = value[index]
        if quote:
            if quote == "]" and char == "]":
                quote = None
            elif quote != "]" and char == quote:
                if index + 1 < len(value) and value[index + 1] == quote:
                    index += 1
                else:
                    quote = None
        elif char in "'\"`":
            quote = char
        elif char == "[":
            quote = "]"
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(value[start:index].strip())
            start = index + 1
        index += 1
    parts.append(value[start:].strip())
    return [part for part in parts if part]


def _matching_paren(value: str, opening: int) -> int:
    depth = 0
    quote: str | None = None
    index = opening
    while index < len(value):
        char = value[index]
        if quote:
            if quote == "]" and char == "]":
                quote = None
            elif quote != "]" and char == quote:
                if index + 1 < len(value) and value[index + 1] == quote:
                    index += 1
                else:
                    quote = None
        elif char in "'\"`":
            quote = char
        elif char == "[":
            quote = "]"
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    raise TenantMigrationError("migration_sql_parenthesis_invalid")


def _leading_identifier(definition: str) -> str | None:
    match = re.match(r'\s*(?:"([^"]+)"|`([^`]+)`|\[([^]]+)\]|([A-Za-z_]\w*))', definition)
    if not match:
        return None
    return next((item for item in match.groups() if item is not None), None)


def _constraint_kind(definition: str) -> str | None:
    normalized = re.sub(r"^\s*CONSTRAINT\s+(?:\w+|\"[^\"]+\"|\[[^]]+\]|`[^`]+`)\s+",
                        "", definition, flags=re.I)
    if re.match(r"^PRIMARY\s+KEY\b", normalized, re.I):
        return "primary_key"
    if re.match(r"^UNIQUE\b", normalized, re.I):
        return "unique"
    if re.match(r"^FOREIGN\s+KEY\b", normalized, re.I):
        return "foreign_key"
    return None


def _scope_constraint(definition: str) -> str:
    opening = definition.find("(")
    if opening < 0:
        raise TenantMigrationError("migration_constraint_parse_failed")
    closing = _matching_paren(definition, opening)
    fields = _split_sql_list(definition[opening + 1:closing])
    normalized = {re.sub(r"[\"`\[\]]", "", item).strip().lower() for item in fields}
    prefix = [_quoted(name) for name in _SCOPE if name not in normalized]
    return definition[:opening + 1] + ",".join(prefix + fields) + definition[closing:]


class EnterpriseTenantMigrator:
    """Apply the fail-closed tenant migration and return an audit-safe manifest."""

    def __init__(self, database_path: Path | str, policy_path: Path = POLICY_PATH):
        self.database_path = Path(database_path)
        self.policy = MigrationPolicy.load(policy_path)

    def connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.database_path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    @staticmethod
    def _control_schema(db: sqlite3.Connection) -> None:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS enterprise_schema_migrations(
          version INTEGER PRIMARY KEY, policy_version TEXT NOT NULL,
          applied_at TEXT NOT NULL, manifest_sha256 TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS enterprise_tenant_migration_runs(
          run_id TEXT PRIMARY KEY, migration_version INTEGER NOT NULL,
          started_at TEXT NOT NULL, completed_at TEXT, status TEXT NOT NULL,
          manifest_json TEXT, error_code TEXT);
        CREATE TABLE IF NOT EXISTS enterprise_tenant_migration_quarantine(
          quarantine_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, table_name TEXT NOT NULL,
          reason_code TEXT NOT NULL, created_at TEXT NOT NULL,
          FOREIGN KEY(run_id) REFERENCES enterprise_tenant_migration_runs(run_id));
        CREATE TABLE IF NOT EXISTS enterprise_tenants(
          tenant_id TEXT PRIMARY KEY, display_name TEXT NOT NULL,
          is_development INTEGER NOT NULL CHECK(is_development IN (0,1)), created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS enterprise_workspaces(
          tenant_id TEXT NOT NULL, workspace_id TEXT NOT NULL, display_name TEXT NOT NULL,
          is_development INTEGER NOT NULL CHECK(is_development IN (0,1)), created_at TEXT NOT NULL,
          PRIMARY KEY(tenant_id,workspace_id),
          FOREIGN KEY(tenant_id) REFERENCES enterprise_tenants(tenant_id));
        """)

    @staticmethod
    def _table_names(db: sqlite3.Connection) -> list[str]:
        return [str(row[0]) for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]

    @staticmethod
    def _columns(db: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
        return db.execute(f"PRAGMA table_info({_quoted(table)})").fetchall()

    @staticmethod
    def _table_sql(db: sqlite3.Connection, table: str) -> str:
        row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        return str(row[0] or "") if row else ""

    @classmethod
    def _safe_integer_surrogate(cls, db: sqlite3.Connection, table: str) -> list[str]:
        primary = sorted((row for row in cls._columns(db, table) if int(row[5]) > 0), key=lambda row: int(row[5]))
        if len(primary) == 1 and str(primary[0][2]).strip().upper() == "INTEGER":
            return [str(primary[0][1])]
        return []

    @classmethod
    def _unscoped_unique_constraints(cls, db: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
        remaining: list[dict[str, Any]] = []
        columns = cls._columns(db, table)
        primary = [str(row[1]) for row in sorted(columns, key=lambda row: int(row[5]) or 9999) if int(row[5]) > 0]
        safe_surrogate = cls._safe_integer_surrogate(db, table)
        if primary and not set(_SCOPE).issubset(primary) and not safe_surrogate:
            remaining.append({"kind": "primary_key", "name": "PRIMARY", "columns": primary})
        for item in db.execute(f"PRAGMA index_list({_quoted(table)})"):
            if not int(item[2]) or str(item[3]) == "pk":
                continue
            index_name = str(item[1])
            index_columns = [str(row[2]) for row in db.execute(
                f"PRAGMA index_xinfo({_quoted(index_name)})") if int(row[5]) == 1 and row[2] is not None]
            if not set(_SCOPE).issubset(index_columns):
                remaining.append({"kind": "unique_index", "name": index_name,
                                  "columns": index_columns, "partial": bool(item[4]),
                                  "origin": str(item[3])})
        return remaining

    @classmethod
    def _scope_complete(cls, db: sqlite3.Connection, table: str) -> bool:
        columns = {str(row[1]): row for row in cls._columns(db, table)}
        if any(name not in columns or int(columns[name][3]) != 1 for name in _SCOPE):
            return False
        indexes = {str(row[1]) for row in db.execute(f"PRAGMA index_list({_quoted(table)})")}
        return f"ix_ent_scope_{table}"[:128] in indexes and not cls._unscoped_unique_constraints(db, table)

    @classmethod
    def _value_fingerprint(cls, db: sqlite3.Connection, table: str,
                           columns: Iterable[str]) -> str:
        names = list(columns)
        digest = hashlib.sha256()
        if not names:
            return digest.hexdigest()
        select = ",".join(_quoted(name) for name in names)
        order = ",".join(_quoted(name) for name in names)
        for row in db.execute(f"SELECT {select} FROM {_quoted(table)} ORDER BY {order}"):
            for value in row:
                raw = b"N" if value is None else (b"B" + value if isinstance(value, bytes)
                                                   else b"T" + str(value).encode("utf-8"))
                digest.update(len(raw).to_bytes(8, "big")); digest.update(raw)
        return digest.hexdigest()

    @classmethod
    def _snapshot(cls, db: sqlite3.Connection, table: str,
                  identity_override: Iterable[str] | None = None) -> dict[str, Any]:
        columns = cls._columns(db, table)
        names = [str(row[1]) for row in columns]
        protected = [name for name in names if any(token in name.lower() for token in ("payload", "sha", "hash"))]
        identity = list(identity_override) if identity_override is not None else [str(row[1]) for row in columns if int(row[5]) > 0]
        has_rowid = "WITHOUT ROWID" not in cls._table_sql(db, table).upper()
        return {"table": table,
                "row_count": int(db.execute(f"SELECT COUNT(*) FROM {_quoted(table)}").fetchone()[0]),
                "schema_sha256": hashlib.sha256(cls._table_sql(db, table).encode()).hexdigest(),
                "columns": names, "identity_columns": identity,
                "identity_fingerprint": cls._value_fingerprint(db, table, identity),
                "rowid_fingerprint": cls._value_fingerprint(db, table, ["rowid"]) if has_rowid else None,
                "protected_columns": protected,
                "protected_fingerprint": cls._value_fingerprint(db, table, protected)}

    @staticmethod
    def _foreign_keys(db: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
        groups: dict[int, dict[str, Any]] = {}
        for row in db.execute(f"PRAGMA foreign_key_list({_quoted(table)})"):
            spec = groups.setdefault(int(row[0]), {"parent": str(row[2]), "from": [], "to": [],
                                                   "on_update": str(row[5]), "on_delete": str(row[6]),
                                                   "match": str(row[7])})
            spec["from"].append(str(row[3])); spec["to"].append(None if row[4] is None else str(row[4]))
        return list(groups.values())

    def _foreign_key_definition(self, db: sqlite3.Connection, spec: dict[str, Any]) -> str:
        parent = _safe_name(spec["parent"])
        children = list(spec["from"]); parents = list(spec["to"])
        if any(item is None for item in parents):
            parent_pk = [str(row[1]) for row in sorted(self._columns(db, parent), key=lambda row: int(row[5]) or 9999)
                         if int(row[5]) > 0]
            if len(parent_pk) != len(parents):
                raise TenantMigrationError("migration_implicit_foreign_key_unresolved")
            parents = parent_pk
        if parent in self.policy.tenant_owned_tables:
            children = [name for name in _SCOPE if name not in children] + children
            parents = [name for name in _SCOPE if name not in parents] + parents
        clause = ("FOREIGN KEY(" + ",".join(_quoted(name) for name in children) + ") REFERENCES " +
                  _quoted(parent) + "(" + ",".join(_quoted(str(name)) for name in parents) + ")")
        if spec["on_update"].upper() != "NO ACTION": clause += " ON UPDATE " + spec["on_update"]
        if spec["on_delete"].upper() != "NO ACTION": clause += " ON DELETE " + spec["on_delete"]
        if spec["match"].upper() != "NONE": clause += " MATCH " + spec["match"]
        return clause

    @staticmethod
    def _rewrite_index_sql(sql: str) -> str:
        on_match = re.search(r"\bON\b", sql, re.I)
        if not on_match:
            raise TenantMigrationError("migration_index_parse_failed")
        opening = sql.find("(", on_match.end())
        if opening < 0:
            raise TenantMigrationError("migration_index_parse_failed")
        closing = _matching_paren(sql, opening)
        fields = _split_sql_list(sql[opening + 1:closing])
        normalized = {re.sub(r"[\"`\[\]]", "", item).strip().lower() for item in fields}
        prefix = [_quoted(name) for name in _SCOPE if name not in normalized]
        return sql[:opening + 1] + ",".join(prefix + fields) + sql[closing:]

    def _shadow_sql(self, db: sqlite3.Connection, table: str, shadow: str) -> str:
        original = self._table_sql(db, table)
        if not original or re.search(r"\bVIRTUAL\s+TABLE\b", original, re.I) or re.search(r"\bDEFERRABLE\b", original, re.I):
            raise ConstraintRewriteError(table, "migration_unsupported_table_definition")
        opening = original.find("("); closing = _matching_paren(original, opening)
        definitions = _split_sql_list(original[opening + 1:closing])
        metadata = self._columns(db, table)
        primary = [str(row[1]) for row in sorted(metadata, key=lambda row: int(row[5]) or 9999) if int(row[5]) > 0]
        safe_surrogate = self._safe_integer_surrogate(db, table)
        unique_singletons = {str(row[2]) for item in db.execute(f"PRAGMA index_list({_quoted(table)})")
                             if int(item[2]) and str(item[3]) == "u"
                             for row in db.execute(f"PRAGMA index_info({_quoted(str(item[1]))})") if row[2] is not None}
        rebuilt: list[str] = []
        append_constraints: list[str] = []
        has_table_primary = False
        for definition in definitions:
            kind = _constraint_kind(definition)
            if kind == "foreign_key":
                continue
            if kind in {"primary_key", "unique"}:
                has_table_primary = has_table_primary or kind == "primary_key"
                rebuilt.append(definition if set(_SCOPE).issubset(
                    {re.sub(r"[\"`\[\]]", "", f).strip().lower() for f in _split_sql_list(
                        definition[definition.find("(") + 1:_matching_paren(definition, definition.find("("))])})
                               else _scope_constraint(definition))
                continue
            column = _leading_identifier(definition)
            if column is None:
                rebuilt.append(definition); continue
            if column in _SCOPE and not re.search(r"\bNOT\s+NULL\b", definition, re.I):
                definition += " NOT NULL"
            if column in primary and not safe_surrogate and re.search(r"\bPRIMARY\s+KEY\b", definition, re.I):
                definition = re.sub(r"(?:CONSTRAINT\s+(?:\w+|\"[^\"]+\")\s+)?PRIMARY\s+KEY"
                                    r"(?:\s+(?:ASC|DESC))?(?:\s+ON\s+CONFLICT\s+\w+)?(?:\s+AUTOINCREMENT)?",
                                    "", definition, flags=re.I)
                if not re.search(r"\bNOT\s+NULL\b", definition, re.I): definition += " NOT NULL"
            if column in unique_singletons and re.search(r"\bUNIQUE\b", definition, re.I):
                definition = re.sub(r"(?:CONSTRAINT\s+(?:\w+|\"[^\"]+\")\s+)?UNIQUE"
                                    r"(?:\s+ON\s+CONFLICT\s+\w+)?", "", definition, flags=re.I)
                append_constraints.append("UNIQUE(" + ",".join(_quoted(name) for name in (*_SCOPE, column)) + ")")
            definition = re.sub(r"\s+REFERENCES\s+(?:\w+|\"[^\"]+\"|\[[^]]+\]|`[^`]+`)\s*\([^)]*\)"
                                r"(?:\s+ON\s+(?:DELETE|UPDATE)\s+(?:SET\s+(?:NULL|DEFAULT)|CASCADE|RESTRICT|NO\s+ACTION))*"
                                r"(?:\s+MATCH\s+\w+)?", "", definition, flags=re.I)
            rebuilt.append(definition.strip())
        if primary and not safe_surrogate and not set(_SCOPE).issubset(primary) and not has_table_primary:
            append_constraints.append("PRIMARY KEY(" + ",".join(_quoted(name) for name in (*_SCOPE, *primary)) + ")")
        append_constraints.extend(self._foreign_key_definition(db, spec) for spec in self._foreign_keys(db, table))
        has_scope_fk = any(spec["parent"] == "enterprise_workspaces" for spec in self._foreign_keys(db, table))
        if not has_scope_fk:
            append_constraints.append("FOREIGN KEY(\"tenant_id\",\"workspace_id\") "
                                      "REFERENCES \"enterprise_workspaces\"(\"tenant_id\",\"workspace_id\")")
        suffix = original[closing + 1:].strip().rstrip(";")
        return f"CREATE TABLE {_quoted(shadow)}(" + ",".join(rebuilt + append_constraints) + ")" + (" " + suffix if suffix else "")

    def _rebuild_table(self, db: sqlite3.Connection, table: str) -> dict[str, Any]:
        schema_objects = [(str(row[0]), str(row[1]), str(row[2])) for row in db.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger') "
            "AND sql IS NOT NULL ORDER BY type,name", (table,))]
        shadow = _safe_name("__ent_shadow_" + hashlib.sha256(table.encode()).hexdigest()[:20])
        db.execute(self._shadow_sql(db, table, shadow))
        columns = [str(row[1]) for row in self._columns(db, table)]
        column_sql = ",".join(_quoted(name) for name in columns)
        table_sql = self._table_sql(db, table)
        safe_integer = bool(self._safe_integer_surrogate(db, table))
        preserve_rowid = "WITHOUT ROWID" not in table_sql.upper() and not safe_integer
        target = ("rowid," if preserve_rowid else "") + column_sql
        source = ("rowid," if preserve_rowid else "") + column_sql
        db.execute(f"INSERT INTO {_quoted(shadow)}({target}) SELECT {source} FROM {_quoted(table)}")
        db.execute(f"DROP TABLE {_quoted(table)}")
        db.execute(f"ALTER TABLE {_quoted(shadow)} RENAME TO {_quoted(table)}")
        rewritten_indexes: list[str] = []
        for object_type, name, sql in schema_objects:
            if object_type == "index" and re.match(r"\s*CREATE\s+UNIQUE\s+INDEX\b", sql, re.I):
                sql = self._rewrite_index_sql(sql); rewritten_indexes.append(name)
            db.execute(sql)
        scope_index = _safe_name(f"ix_ent_scope_{table}"[:128])
        db.execute(f"CREATE INDEX IF NOT EXISTS {_quoted(scope_index)} ON {_quoted(table)}(tenant_id,workspace_id)")
        return {"table": table, "rewritten_indexes": rewritten_indexes,
                "safe_global_surrogate_primary_key": self._safe_integer_surrogate(db, table)}

    def _record_quarantine(self, db: sqlite3.Connection, run_id: str, table: str, reason: str) -> None:
        db.execute("INSERT INTO enterprise_tenant_migration_quarantine VALUES(?,?,?,?,?)",
                   ("ETQ-" + uuid.uuid4().hex.upper(), run_id, _safe_name(table), reason, _now()))

    def migrate(self, *, interrupt_after_tables: int | None = None,
                step_hook: Callable[[str, int], None] | None = None) -> dict[str, Any]:
        run_id = "ETM-" + uuid.uuid4().hex.upper(); started_at = _now()
        with self.connect() as db:
            self._control_schema(db)
            db.execute("INSERT INTO enterprise_tenant_migration_runs"
                       "(run_id,migration_version,started_at,status) VALUES(?,?,?,'running')",
                       (run_id, self.policy.migration_version, started_at))
            applied = db.execute("SELECT 1 FROM enterprise_schema_migrations WHERE version=?",
                                 (self.policy.migration_version,)).fetchone()
            discovered = self._table_names(db)
            owned = sorted(set(discovered) & self.policy.tenant_owned_tables)
            unknown = sorted(set(discovered) - self.policy.tenant_owned_tables - self.policy.system_tables - _MIGRATION_TABLES)
            if unknown:
                for table in unknown: self._record_quarantine(db, run_id, table, "unclassified_persistent_table")
                manifest = {"schema_version": "enterprise_tenant_migration_manifest_v2", "run_id": run_id,
                            "status": "blocked", "unknown_tables": unknown,
                            "quarantined_table_count": len(unknown), "network_called": False}
                db.execute("UPDATE enterprise_tenant_migration_runs SET completed_at=?,status='blocked',"
                           "manifest_json=?,error_code='unclassified_persistent_table' WHERE run_id=?",
                           (_now(), json.dumps(manifest, sort_keys=True), run_id))
                return manifest
            incomplete = [table for table in owned if not self._scope_complete(db, table)]
            if applied and not incomplete:
                manifest = {"schema_version": "enterprise_tenant_migration_manifest_v2", "run_id": run_id,
                            "status": "already_applied", "migration_version": self.policy.migration_version,
                            "tenant_owned_tables": owned, "remaining_scoped_unique_rewrites": {},
                            "network_called": False}
                db.execute("UPDATE enterprise_tenant_migration_runs SET completed_at=?,status=?,manifest_json=? WHERE run_id=?",
                           (_now(), "already_applied", json.dumps(manifest, sort_keys=True), run_id))
                return manifest
            pre = {table: self._snapshot(db, table) for table in owned}
            targets = owned if not applied else incomplete
            changed: list[str] = []; rebuilds: list[dict[str, Any]] = []
            db.execute("PRAGMA foreign_keys=OFF")
            try:
                db.execute("BEGIN IMMEDIATE")
                timestamp = _now()
                db.execute("INSERT OR IGNORE INTO enterprise_tenants VALUES(?,?,1,?)",
                           (self.policy.default_tenant_id, "Local development tenant", timestamp))
                db.execute("INSERT OR IGNORE INTO enterprise_workspaces VALUES(?,?,?,1,?)",
                           (self.policy.default_tenant_id, self.policy.default_workspace_id,
                            "Local development workspace", timestamp))
                for index, table in enumerate(targets, 1):
                    columns = {str(row[1]): row for row in self._columns(db, table)}
                    for name, value in zip(_SCOPE, (self.policy.default_tenant_id, self.policy.default_workspace_id)):
                        if name not in columns:
                            # Policy validation pins these values to the fixed safe local
                            # identifiers; SQLite does not accept bind parameters in DDL.
                            db.execute(f"ALTER TABLE {_quoted(table)} ADD COLUMN {_quoted(name)} "
                                       f"TEXT NOT NULL DEFAULT '{value}'")
                        else:
                            db.execute(f"UPDATE {_quoted(table)} SET {_quoted(name)}=? WHERE {_quoted(name)} IS NULL", (value,))
                    try:
                        rebuilds.append(self._rebuild_table(db, table))
                    except ConstraintRewriteError:
                        raise
                    except (TenantMigrationError, sqlite3.DatabaseError) as exc:
                        raise ConstraintRewriteError(
                            table, "migration_constraint_rewrite_failed"
                        ) from exc
                    changed.append(table)
                    if step_hook: step_hook(table, index)
                    if interrupt_after_tables is not None and index >= interrupt_after_tables:
                        raise TenantMigrationError("simulated_migration_interruption")
                post = {table: self._snapshot(db, table, pre[table]["identity_columns"]) for table in owned}
                for table in owned:
                    if pre[table]["row_count"] != post[table]["row_count"]: raise TenantMigrationError("migration_row_count_changed")
                    if pre[table]["identity_fingerprint"] != post[table]["identity_fingerprint"]: raise TenantMigrationError("migration_identity_changed")
                    if pre[table]["rowid_fingerprint"] != post[table]["rowid_fingerprint"]: raise TenantMigrationError("migration_rowid_changed")
                    if pre[table]["protected_fingerprint"] != post[table]["protected_fingerprint"]: raise TenantMigrationError("migration_protected_value_changed")
                    if not self._scope_complete(db, table):
                        raise ConstraintRewriteError(table, "migration_constraint_rewrite_incomplete")
                fk_violations = [list(row) for row in db.execute("PRAGMA foreign_key_check")]
                integrity = str(db.execute("PRAGMA integrity_check").fetchone()[0])
                if fk_violations or integrity != "ok": raise TenantMigrationError("migration_database_integrity_failed")
                remaining = {table: value for table in owned
                             if (value := self._unscoped_unique_constraints(db, table))}
                safe = {table: value for table in owned
                        if (value := self._safe_integer_surrogate(db, table))}
                manifest = {"schema_version": "enterprise_tenant_migration_manifest_v2", "run_id": run_id,
                            "status": "applied", "migration_version": self.policy.migration_version,
                            "default_tenant_id": self.policy.default_tenant_id,
                            "default_workspace_id": self.policy.default_workspace_id,
                            "tenant_owned_tables": owned, "changed_tables": changed,
                            "shadow_rebuilds": rebuilds, "pre_migration": pre, "post_migration": post,
                            "remaining_scoped_unique_rewrites": remaining,
                            "safe_global_surrogate_primary_keys": safe,
                            "foreign_key_violation_count": 0, "integrity_check": integrity,
                            "quarantined_table_count": 0, "protected_external_evidence_modified": False,
                            "network_called": False}
                encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
                db.execute("INSERT INTO enterprise_schema_migrations VALUES(?,?,?,?) ON CONFLICT(version) DO UPDATE SET "
                           "policy_version=excluded.policy_version,applied_at=excluded.applied_at,"
                           "manifest_sha256=excluded.manifest_sha256",
                           (self.policy.migration_version, self.policy.schema_version, _now(),
                            hashlib.sha256(encoded.encode()).hexdigest()))
                db.execute("COMMIT")
            except Exception as exc:
                if db.in_transaction: db.execute("ROLLBACK")
                code = str(exc) if isinstance(exc, TenantMigrationError) else "migration_failed"
                status = "blocked" if isinstance(exc, ConstraintRewriteError) else "failed"
                if isinstance(exc, ConstraintRewriteError): self._record_quarantine(db, run_id, exc.table, exc.code)
                failure = {"schema_version": "enterprise_tenant_migration_manifest_v2", "run_id": run_id,
                           "status": status, "error_code": code, "rolled_back": True,
                           "changed_tables_before_rollback": changed,
                           "quarantined_table_count": int(isinstance(exc, ConstraintRewriteError)),
                           "network_called": False}
                db.execute("UPDATE enterprise_tenant_migration_runs SET completed_at=?,status=?,manifest_json=?,error_code=? WHERE run_id=?",
                           (_now(), status, json.dumps(failure, sort_keys=True), code, run_id))
                return failure
            finally:
                db.execute("PRAGMA foreign_keys=ON")
            db.execute("UPDATE enterprise_tenant_migration_runs SET completed_at=?,status='applied',manifest_json=? WHERE run_id=?",
                       (_now(), json.dumps(manifest, sort_keys=True), run_id))
            return manifest


__all__ = ["EnterpriseTenantMigrator", "MIGRATION_VERSION", "MigrationPolicy", "TenantMigrationError"]
