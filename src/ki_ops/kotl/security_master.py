"""Kelai security master (Snowflake ``KELAI.LSEG[_CANARY].SECURITY_MASTER_DT``).

The kelai security master is a Snowflake dynamic table rebuilt every day by
the kelaidata Airflow ``lseg_pipeline`` DAG. It is keyed by ds2 **INFOCODE**
(the same key as the H5 snapshot / the dollar files) and carries the listing
identifiers the pre-submit Flex resolution wants — most importantly **SEDOL**
(FlexTrade's preferred equity identifier, and listing-level so it cannot pick
the wrong exchange for a dual-listed name, unlike ISIN).

Prod reads ``KELAI.LSEG``; canary reads the isolated ``KELAI.LSEG_CANARY``
clone (same convention as the kelaidb ``kelai`` / ``kelai_canary`` split).

Connection reuses the **Airflow pipeline service account** conventions so
KOTL works unchanged inside MWAA / Batch: key-pair auth as
``AIRFLOW_PIPELINE_SVC`` with role ``DATA_PIPELINE_ROLE``, private key from
AWS Secrets Manager. Every setting is env-overridable (same variable names
the kelaidata plugins use):

- ``SNOWFLAKE_ACCOUNT``            (default ``ernqufb-xqb94021``)
- ``SNOWFLAKE_USER``               (default ``AIRFLOW_PIPELINE_SVC``)
- ``SNOWFLAKE_WAREHOUSE``          (default ``COMPUTE_WH``)
- ``SNOWFLAKE_ROLE``               (default ``DATA_PIPELINE_ROLE``)
- ``SNOWFLAKE_PRIVATE_KEY_SECRET`` (default ``kelai/airflow-pipeline-svc/private-key``)
- ``KOTL_SECMASTER_SCHEMA``        (default ``LSEG`` for PROD, ``LSEG_CANARY`` otherwise)

Requires the ``secmaster`` extra (``snowflake-connector-python`` +
``cryptography`` + ``boto3``); imports are lazy so the rest of KOTL never
needs them.
"""

from __future__ import annotations

import json
import os
import re
from typing import Iterable, Mapping

DEFAULT_ACCOUNT = "ernqufb-xqb94021"
DEFAULT_USER = "AIRFLOW_PIPELINE_SVC"
DEFAULT_WAREHOUSE = "COMPUTE_WH"
DEFAULT_ROLE = "DATA_PIPELINE_ROLE"
DEFAULT_PRIVATE_KEY_SECRET = "kelai/airflow-pipeline-svc/private-key"
DEFAULT_REGION = "us-east-1"

SECURITY_MASTER_TABLE = "SECURITY_MASTER_DT"

# IN-list chunk: a full book is ~2.3k infocodes, so normally a single query.
QUERY_CHUNK = 5000


class SecurityMasterError(RuntimeError):
    """Security master unreachable or query failed."""


def secmaster_schema(env: str) -> str:
    """Schema for *env*: ``LSEG`` for PROD, ``LSEG_CANARY`` otherwise.

    Override with ``KOTL_SECMASTER_SCHEMA``.
    """
    override = os.environ.get("KOTL_SECMASTER_SCHEMA")
    if override:
        return override
    return "LSEG" if env.upper() == "PROD" else "LSEG_CANARY"


def _load_private_key_der(secret_id: str, region: str) -> bytes:
    """Private key PEM from Secrets Manager → DER bytes for the connector.

    Accepts the raw PEM as the SecretString or a JSON object with a
    ``private_key`` field (both conventions exist at kelai).
    """
    import boto3
    from cryptography.hazmat.primitives import serialization

    raw = boto3.client("secretsmanager", region_name=region).get_secret_value(
        SecretId=secret_id
    )["SecretString"]
    pem = raw
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and "private_key" in parsed:
            pem = parsed["private_key"]
    except (json.JSONDecodeError, TypeError):
        pass
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    return key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def connect_snowflake():
    """Snowflake connection with the Airflow pipeline service-account auth."""
    try:
        import snowflake.connector
    except ImportError as exc:  # pragma: no cover — dependency guidance
        raise SecurityMasterError(
            "snowflake-connector-python not installed — pip install "
            "'ki-ops[secmaster]' (or pass --sedol-source none/<csv>)"
        ) from exc

    der = _load_private_key_der(
        os.environ.get("SNOWFLAKE_PRIVATE_KEY_SECRET", DEFAULT_PRIVATE_KEY_SECRET),
        os.environ.get("AWS_REGION", DEFAULT_REGION),
    )
    return snowflake.connector.connect(
        account=os.environ.get("SNOWFLAKE_ACCOUNT", DEFAULT_ACCOUNT),
        user=os.environ.get("SNOWFLAKE_USER", DEFAULT_USER),
        warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE", DEFAULT_WAREHOUSE),
        role=os.environ.get("SNOWFLAKE_ROLE", DEFAULT_ROLE),
        private_key=der,
    )


def _sedol_query(schema: str, infocodes: list[int]) -> str:
    in_list = ",".join(str(code) for code in infocodes)
    # One row per infocode; on the (unexpected) event of duplicates prefer the
    # active, most recently listed row.
    return (
        "SELECT INFOCODE, SEDOL FROM KELAI."
        f"{schema}.{SECURITY_MASTER_TABLE} "
        f"WHERE SEDOL IS NOT NULL AND INFOCODE IN ({in_list}) "
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY INFOCODE "
        "ORDER BY ISACTIVE DESC, LISTINGSTARTDATE DESC) = 1"
    )


def fetch_sedols_by_infocode(
    infocodes: Iterable[int | str],
    *,
    env: str = "UAT",
    schema: str | None = None,
    connection=None,
) -> dict[str, str]:
    """``{infocode: sedol}`` for *infocodes* from the daily security master.

    *connection* is injectable for tests; otherwise one is opened (and closed)
    per call via :func:`connect_snowflake`.
    """
    codes: list[int] = []
    seen: set[int] = set()
    for raw in infocodes:
        code = int(str(raw).strip())
        if code not in seen:
            seen.add(code)
            codes.append(code)
    if not codes:
        return {}

    schema = schema or secmaster_schema(env)
    own_connection = connection is None
    if own_connection:
        connection = connect_snowflake()
    out: dict[str, str] = {}
    try:
        cursor = connection.cursor()
        try:
            for start in range(0, len(codes), QUERY_CHUNK):
                cursor.execute(_sedol_query(schema, codes[start : start + QUERY_CHUNK]))
                for infocode, sedol in cursor.fetchall():
                    if sedol and str(sedol).strip():
                        out[str(infocode)] = str(sedol).strip()
        finally:
            cursor.close()
    except SecurityMasterError:
        raise
    except Exception as exc:
        raise SecurityMasterError(
            f"security master query failed (KELAI.{schema}.{SECURITY_MASTER_TABLE}): {exc}"
        ) from exc
    finally:
        if own_connection:
            connection.close()
    return out


_SEDOL_SHAPE = re.compile(r"^[0-9A-Z]{6,7}$")


def _infocode_query(schema: str, sedols: list[str]) -> str:
    in_list = ",".join(f"'{s}'" for s in sedols)
    # One row per SEDOL; on duplicates prefer the active, most recent listing.
    return (
        "SELECT SEDOL, INFOCODE FROM KELAI."
        f"{schema}.{SECURITY_MASTER_TABLE} "
        f"WHERE INFOCODE IS NOT NULL AND SEDOL IN ({in_list}) "
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY SEDOL "
        "ORDER BY ISACTIVE DESC, LISTINGSTARTDATE DESC) = 1"
    )


def fetch_infocodes_by_sedol(
    sedols: Iterable[str],
    *,
    env: str = "UAT",
    schema: str | None = None,
    connection=None,
) -> dict[str, str]:
    """``{sedol: infocode}`` — the reverse join for Flex→ds2 translation.

    Used by the submit's inverse symbol map for SOD-only live positions (names
    today's target dropped): their Flex-master SEDOL leads back to the ds2
    infocode, and the snapshot's ``infocode_by_ticker`` completes the hop to
    the ds2 ticker. SEDOLs are validated to 6–7 alphanumeric chars (anything
    else is dropped) so the IN-list stays safe.
    """
    wanted: list[str] = []
    seen: set[str] = set()
    for raw in sedols:
        sedol = str(raw).strip().upper()
        if sedol and _SEDOL_SHAPE.match(sedol) and sedol not in seen:
            seen.add(sedol)
            wanted.append(sedol)
    if not wanted:
        return {}

    schema = schema or secmaster_schema(env)
    own_connection = connection is None
    if own_connection:
        connection = connect_snowflake()
    out: dict[str, str] = {}
    try:
        cursor = connection.cursor()
        try:
            for start in range(0, len(wanted), QUERY_CHUNK):
                cursor.execute(_infocode_query(schema, wanted[start : start + QUERY_CHUNK]))
                for sedol, infocode in cursor.fetchall():
                    if infocode is not None:
                        out[str(sedol).strip().upper()] = str(int(str(infocode).strip()))
        finally:
            cursor.close()
    except SecurityMasterError:
        raise
    except Exception as exc:
        raise SecurityMasterError(
            f"security master query failed (KELAI.{schema}.{SECURITY_MASTER_TABLE}): {exc}"
        ) from exc
    finally:
        if own_connection:
            connection.close()
    return out


def fetch_sedols_by_ticker(
    infocode_by_ticker: Mapping[str, str],
    *,
    env: str = "UAT",
    schema: str | None = None,
    connection=None,
) -> dict[str, str]:
    """``{book_ticker: sedol}`` — the map :func:`resolve_flex_symbols` wants.

    *infocode_by_ticker* is the ds2 snapshot's ticker→infocode map (the book's
    own vocabulary), so the join back is exact.
    """
    by_infocode = fetch_sedols_by_infocode(
        infocode_by_ticker.values(), env=env, schema=schema, connection=connection
    )
    return {
        str(ticker).strip().upper(): by_infocode[str(int(str(infocode).strip()))]
        for ticker, infocode in infocode_by_ticker.items()
        if str(int(str(infocode).strip())) in by_infocode
    }
