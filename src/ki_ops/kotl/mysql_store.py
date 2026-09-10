"""MySQL backend for the KOTL ledger (same surface as the CSV ``KotlStore``).

Tables (idempotent DDL, created lazily on first use):

- ``kotl_submits`` — one row per send attempt, keyed by ``submit_id``;
  ``payload_json`` / ``flex_response_json`` are JSON columns with the same
  content the CSV store serializes. ``trade_date`` + ``claim_submit_id`` link
  each attempt to the day's ``kotl_submit_claims`` row (winner points at
  itself, forced top-ups at the winner; NULL for offline sends and rows
  written before the columns existed — added in place by ``ensure_schema``).
- ``kotl_working_orders`` — one row per Flex parent order, keyed by
  ``flex_order_id`` (upsert semantics identical to the CSV store).
- ``kotl_eod_snapshots`` — one row per ``kotl eod`` run (summary JSON), in
  addition to the file snapshot the EOD loop always writes.

Credentials: ``KOTL_DB_HOST`` / ``KOTL_DB_PORT`` / ``KOTL_DB_USER`` /
``KOTL_DB_PASSWORD`` / ``KOTL_DB_SCHEMA`` env vars take priority (local docker
dev — see ``docker-compose.kotl-db.yml``); otherwise a Secrets Manager secret
id (``--db-secret``, e.g. ``dev/kelaidb``) following the kelaidata pattern —
fields inspected defensively (``host``/``hostname``/``endpoint``,
``username``/``user``, ``password``, ``port``).

``mysql-connector-python`` is an optional dependency: ``pip install ki-ops[db]``.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

from ki_ops.kotl.enums import OrderStatus
from ki_ops.kotl.models import Submit, WorkingOrder

DEFAULT_REGION = "us-east-1"
DEFAULT_PORT = 3306

_DDL = (
    """
    CREATE TABLE IF NOT EXISTS kotl_submits (
        submit_id VARCHAR(64) NOT NULL PRIMARY KEY,
        submitted_at DATETIME(6) NOT NULL,
        env VARCHAR(16) NOT NULL,
        ok TINYINT(1) NOT NULL,
        flex_order_ids JSON NOT NULL,
        payload_json JSON NOT NULL,
        flex_response_json JSON NULL,
        trade_date DATE NULL,
        claim_submit_id VARCHAR(64) NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS kotl_working_orders (
        flex_order_id VARCHAR(128) NOT NULL PRIMARY KEY,
        submit_id VARCHAR(64) NOT NULL,
        trade_date DATE NOT NULL,
        symbol VARCHAR(32) NOT NULL,
        side VARCHAR(16) NOT NULL,
        fund VARCHAR(64) NOT NULL DEFAULT '',
        position_group VARCHAR(128) NOT NULL DEFAULT '',
        sent_qty DECIMAL(24, 6) NOT NULL,
        filled_qty DECIMAL(24, 6) NOT NULL,
        leaves_qty DECIMAL(24, 6) NOT NULL,
        status VARCHAR(16) NOT NULL,
        last_seen_at DATETIME(6) NOT NULL,
        avg_fill_px DECIMAL(24, 8) NULL,
        flex_batch_id VARCHAR(128) NULL,
        broker VARCHAR(64) NULL,
        algo VARCHAR(64) NULL,
        order_type VARCHAR(32) NULL,
        KEY idx_kotl_wo_trade_date (trade_date),
        KEY idx_kotl_wo_submit (submit_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS kotl_eod_snapshots (
        trade_date DATE NOT NULL,
        created_at DATETIME(6) NOT NULL,
        summary_json JSON NOT NULL,
        PRIMARY KEY (trade_date, created_at)
    )
    """,
    # Once-a-day submission claim: the PK insert is the atomic gate that makes
    # concurrent duplicate runs impossible (target mode; see kotl/submit.py).
    """
    CREATE TABLE IF NOT EXISTS kotl_submit_claims (
        trade_date DATE NOT NULL,
        env VARCHAR(16) NOT NULL,
        submit_id VARCHAR(64) NOT NULL,
        claimed_at DATETIME(6) NOT NULL,
        PRIMARY KEY (trade_date, env)
    )
    """,
)

# Idempotent column additions for tables created before the column existed
# (CREATE TABLE IF NOT EXISTS never alters an existing table).
_COLUMN_MIGRATIONS = (
    ("kotl_submits", "trade_date", "ADD COLUMN trade_date DATE NULL"),
    ("kotl_submits", "claim_submit_id", "ADD COLUMN claim_submit_id VARCHAR(64) NULL"),
)


def _utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _to_db_ts(ts: datetime) -> datetime:
    """tz-aware → naive UTC (MySQL DATETIME has no tz)."""
    return _utc(ts).astimezone(timezone.utc).replace(tzinfo=None)


def _from_db_ts(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc)


def _json_or_none(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    return json.loads(value) if isinstance(value, str) else value


def load_db_secret(secret_id: str, *, region: str = DEFAULT_REGION) -> dict[str, Any]:
    import boto3

    raw = boto3.client("secretsmanager", region_name=region).get_secret_value(
        SecretId=secret_id
    )["SecretString"]
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"secret {secret_id} is not a JSON object")
    return data


def resolve_db_params(
    *,
    db_secret: str | None = None,
    db_schema: str | None = None,
    region: str = DEFAULT_REGION,
) -> dict[str, Any]:
    """Env vars win; else the Secrets Manager secret (fields read defensively)."""
    host = os.environ.get("KOTL_DB_HOST")
    if host:
        return {
            "host": host,
            "port": int(os.environ.get("KOTL_DB_PORT") or DEFAULT_PORT),
            "user": os.environ.get("KOTL_DB_USER") or "root",
            "password": os.environ.get("KOTL_DB_PASSWORD") or "",
            "schema": db_schema or os.environ.get("KOTL_DB_SCHEMA") or "kelai",
        }
    if not db_secret:
        raise ValueError(
            "no MySQL credentials: set KOTL_DB_HOST/KOTL_DB_PORT/KOTL_DB_USER/"
            "KOTL_DB_PASSWORD/KOTL_DB_SCHEMA, or pass --db-secret (e.g. dev/kelaidb)"
        )
    secret = load_db_secret(db_secret, region=region)
    host = secret.get("host") or secret.get("hostname") or secret.get("endpoint")
    if not host:
        raise ValueError(f"secret {db_secret} has no host/hostname/endpoint field")
    return {
        "host": str(host),
        "port": int(secret.get("port") or DEFAULT_PORT),
        "user": str(secret.get("username") or secret.get("user") or "root"),
        "password": str(secret.get("password") or ""),
        "schema": db_schema
        or os.environ.get("KOTL_DB_SCHEMA")
        or str(secret.get("dbname") or secret.get("schema") or "kelai"),
    }


class MysqlKotlStore:
    """KOTL ledger in MySQL — drop-in for :class:`ki_ops.kotl.store.KotlStore`."""

    def __init__(
        self,
        *,
        host: str,
        user: str,
        password: str,
        schema: str,
        port: int = DEFAULT_PORT,
    ) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.schema = schema
        self._conn = None
        self._schema_ready = False

    @classmethod
    def from_env_or_secret(
        cls,
        *,
        db_secret: str | None = None,
        db_schema: str | None = None,
        region: str = DEFAULT_REGION,
    ) -> "MysqlKotlStore":
        params = resolve_db_params(db_secret=db_secret, db_schema=db_schema, region=region)
        return cls(
            host=params["host"],
            port=params["port"],
            user=params["user"],
            password=params["password"],
            schema=params["schema"],
        )

    # --- connection / schema ---------------------------------------------

    def _connect(self):
        try:
            import mysql.connector
        except ImportError as exc:
            raise RuntimeError(
                "mysql-connector-python is required for --store mysql: "
                "pip install 'ki-ops[db]'"
            ) from exc
        return mysql.connector.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            autocommit=False,
        )

    def connection(self):
        if self._conn is None or not self._conn.is_connected():
            self._conn = self._connect()
            self._schema_ready = False
        if not self._schema_ready:
            self.ensure_schema()
        return self._conn

    def ensure_schema(self) -> None:
        """Idempotent: create the schema (if allowed) and the KOTL tables."""
        if self._conn is None or not self._conn.is_connected():
            self._conn = self._connect()
        cur = self._conn.cursor()
        try:
            try:
                cur.execute(f"CREATE DATABASE IF NOT EXISTS `{self.schema}`")
            except Exception:
                pass  # no CREATE privilege — the schema must already exist
            cur.execute(f"USE `{self.schema}`")
            for ddl in _DDL:
                cur.execute(ddl)
            for table, column, clause in _COLUMN_MIGRATIONS:
                cur.execute(
                    "SELECT COUNT(*) FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s",
                    (self.schema, table, column),
                )
                if cur.fetchone()[0] == 0:
                    cur.execute(f"ALTER TABLE `{table}` {clause}")
            self._conn.commit()
        finally:
            cur.close()
        self._schema_ready = True

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None
                self._schema_ready = False

    # --- submits -----------------------------------------------------------

    def append_submit(self, submit: Submit) -> None:
        conn = self.connection()
        cur = conn.cursor()
        try:
            cur.execute(
                """
                INSERT INTO kotl_submits
                    (submit_id, submitted_at, env, ok, flex_order_ids,
                     payload_json, flex_response_json, trade_date, claim_submit_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    submit.submit_id,
                    _to_db_ts(submit.submitted_at),
                    submit.env,
                    1 if submit.ok else 0,
                    json.dumps(list(submit.flex_order_ids)),
                    json.dumps(list(submit.payload), sort_keys=True),
                    json.dumps(submit.flex_response, sort_keys=True)
                    if submit.flex_response is not None
                    else None,
                    submit.trade_date,
                    submit.claim_submit_id,
                ),
            )
            conn.commit()
        finally:
            cur.close()

    def load_submits(self) -> list[Submit]:
        conn = self.connection()
        cur = conn.cursor()
        try:
            cur.execute(
                """
                SELECT submit_id, submitted_at, env, ok, flex_order_ids,
                       payload_json, flex_response_json, trade_date, claim_submit_id
                FROM kotl_submits ORDER BY submitted_at, submit_id
                """
            )
            rows = cur.fetchall()
        finally:
            cur.close()
        out = []
        for row in rows:
            (
                submit_id,
                submitted_at,
                env,
                ok,
                ids_raw,
                payload_raw,
                response_raw,
                trade_date,
                claim_submit_id,
            ) = row
            out.append(
                Submit(
                    submit_id=submit_id,
                    submitted_at=_from_db_ts(submitted_at),
                    env=env,
                    ok=bool(ok),
                    flex_order_ids=tuple(_json_or_none(ids_raw) or ()),
                    payload=tuple(_json_or_none(payload_raw) or ()),
                    flex_response=_json_or_none(response_raw),
                    trade_date=trade_date,
                    claim_submit_id=claim_submit_id or None,
                )
            )
        return out

    # --- working orders ----------------------------------------------------

    def load_working_orders(
        self,
        *,
        trade_date: date | None = None,
        submit_id: str | None = None,
    ) -> list[WorkingOrder]:
        clauses, params = [], []
        if trade_date is not None:
            clauses.append("trade_date = %s")
            params.append(trade_date)
        if submit_id is not None:
            clauses.append("submit_id = %s")
            params.append(submit_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        conn = self.connection()
        cur = conn.cursor()
        try:
            cur.execute(
                f"""
                SELECT flex_order_id, submit_id, trade_date, symbol, side, fund,
                       position_group, sent_qty, filled_qty, leaves_qty, status,
                       last_seen_at, avg_fill_px, flex_batch_id, broker, algo,
                       order_type
                FROM kotl_working_orders {where}
                ORDER BY trade_date, flex_order_id
                """,
                params,
            )
            rows = cur.fetchall()
        finally:
            cur.close()
        return [self._working_order_from_db(row) for row in rows]

    def get_working_order(self, flex_order_id: str) -> WorkingOrder | None:
        conn = self.connection()
        cur = conn.cursor()
        try:
            cur.execute(
                """
                SELECT flex_order_id, submit_id, trade_date, symbol, side, fund,
                       position_group, sent_qty, filled_qty, leaves_qty, status,
                       last_seen_at, avg_fill_px, flex_batch_id, broker, algo,
                       order_type
                FROM kotl_working_orders WHERE flex_order_id = %s
                """,
                (flex_order_id,),
            )
            row = cur.fetchone()
        finally:
            cur.close()
        return self._working_order_from_db(row) if row else None

    def upsert_working_orders(self, orders: Iterable[WorkingOrder]) -> None:
        rows = [self._working_order_to_db(o) for o in orders]
        if not rows:
            return
        conn = self.connection()
        cur = conn.cursor()
        try:
            cur.executemany(
                """
                INSERT INTO kotl_working_orders
                    (flex_order_id, submit_id, trade_date, symbol, side, fund,
                     position_group, sent_qty, filled_qty, leaves_qty, status,
                     last_seen_at, avg_fill_px, flex_batch_id, broker, algo,
                     order_type)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    submit_id = VALUES(submit_id),
                    trade_date = VALUES(trade_date),
                    symbol = VALUES(symbol),
                    side = VALUES(side),
                    fund = VALUES(fund),
                    position_group = VALUES(position_group),
                    sent_qty = VALUES(sent_qty),
                    filled_qty = VALUES(filled_qty),
                    leaves_qty = VALUES(leaves_qty),
                    status = VALUES(status),
                    last_seen_at = VALUES(last_seen_at),
                    avg_fill_px = VALUES(avg_fill_px),
                    flex_batch_id = VALUES(flex_batch_id),
                    broker = VALUES(broker),
                    algo = VALUES(algo),
                    order_type = VALUES(order_type)
                """,
                rows,
            )
            conn.commit()
        finally:
            cur.close()

    # --- submission claims ---------------------------------------------------

    def claim_submission(self, trade_date: date, env: str, submit_id: str) -> str | None:
        """Once-a-day submission claim: ``None`` when this call won the claim,
        else the ``submit_id`` that already holds it.

        A plain PK ``INSERT`` — MySQL serializes concurrent claimers, so two
        simultaneous pipeline runs can never both pass (duplicate-key loses).
        """
        import mysql.connector

        conn = self.connection()
        cur = conn.cursor()
        try:
            try:
                cur.execute(
                    """
                    INSERT INTO kotl_submit_claims (trade_date, env, submit_id, claimed_at)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (
                        trade_date,
                        env.upper(),
                        submit_id,
                        _to_db_ts(datetime.now(timezone.utc)),
                    ),
                )
                conn.commit()
                return None
            except mysql.connector.IntegrityError:
                conn.rollback()
                cur.execute(
                    "SELECT submit_id FROM kotl_submit_claims WHERE trade_date = %s AND env = %s",
                    (trade_date, env.upper()),
                )
                row = cur.fetchone()
                return str(row[0]) if row else "unknown"
        finally:
            cur.close()

    # --- EOD snapshots ------------------------------------------------------

    def append_eod_snapshot(self, trade_date: date, summary: dict[str, Any]) -> None:
        conn = self.connection()
        cur = conn.cursor()
        try:
            cur.execute(
                """
                INSERT INTO kotl_eod_snapshots (trade_date, created_at, summary_json)
                VALUES (%s, %s, %s)
                """,
                (
                    trade_date,
                    _to_db_ts(datetime.now(timezone.utc)),
                    json.dumps(summary, sort_keys=True),
                ),
            )
            conn.commit()
        finally:
            cur.close()

    def load_eod_snapshots(self, *, trade_date: date | None = None) -> list[dict[str, Any]]:
        conn = self.connection()
        cur = conn.cursor()
        try:
            if trade_date is not None:
                cur.execute(
                    "SELECT trade_date, created_at, summary_json FROM kotl_eod_snapshots "
                    "WHERE trade_date = %s ORDER BY created_at",
                    (trade_date,),
                )
            else:
                cur.execute(
                    "SELECT trade_date, created_at, summary_json FROM kotl_eod_snapshots "
                    "ORDER BY trade_date, created_at"
                )
            rows = cur.fetchall()
        finally:
            cur.close()
        return [
            {
                "trade_date": row[0].isoformat() if hasattr(row[0], "isoformat") else str(row[0]),
                "created_at": _from_db_ts(row[1]).isoformat(),
                "summary": _json_or_none(row[2]),
            }
            for row in rows
        ]

    # --- row mapping ---------------------------------------------------------

    @staticmethod
    def _working_order_to_db(order: WorkingOrder) -> tuple:
        return (
            order.flex_order_id,
            order.submit_id,
            order.trade_date,
            order.symbol,
            order.side,
            order.fund,
            order.position_group,
            order.sent_qty,
            order.filled_qty,
            order.leaves_qty,
            order.status.value,
            _to_db_ts(order.last_seen_at),
            order.avg_fill_px,
            order.flex_batch_id,
            order.broker,
            order.algo,
            order.order_type,
        )

    @staticmethod
    def _working_order_from_db(row: tuple) -> WorkingOrder:
        (
            flex_order_id,
            submit_id,
            trade_date,
            symbol,
            side,
            fund,
            position_group,
            sent_qty,
            filled_qty,
            leaves_qty,
            status,
            last_seen_at,
            avg_fill_px,
            flex_batch_id,
            broker,
            algo,
            order_type,
        ) = row
        return WorkingOrder(
            flex_order_id=flex_order_id,
            submit_id=submit_id,
            trade_date=trade_date,
            symbol=symbol,
            side=side,
            fund=fund,
            position_group=position_group,
            sent_qty=Decimal(str(sent_qty)),
            filled_qty=Decimal(str(filled_qty)),
            leaves_qty=Decimal(str(leaves_qty)),
            status=OrderStatus(status),
            last_seen_at=_from_db_ts(last_seen_at),
            avg_fill_px=Decimal(str(avg_fill_px)) if avg_fill_px is not None else None,
            flex_batch_id=flex_batch_id or None,
            broker=broker or None,
            algo=algo or None,
            order_type=order_type or None,
        )
