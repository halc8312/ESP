from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import MetaData, create_engine, func, inspect, select, text
from sqlalchemy.engine import Connection, Engine, make_url

from database import Base, redact_database_url


DEFAULT_EXISTING_WEB_SQLITE_URL = "sqlite:////var/data/mercari.db"
DEFAULT_MIGRATION_BATCH_SIZE = 500


def _get_backend(database_url: str) -> str:
    try:
        driver_name = make_url(str(database_url or "").strip()).drivername.lower()
    except Exception:
        return "unknown"

    if "sqlite" in driver_name:
        return "sqlite"
    if driver_name.startswith("postgresql") or driver_name.startswith("postgres"):
        return "postgresql"
    return driver_name


def _repo_table_order() -> list[str]:
    return [table.name for table in Base.metadata.sorted_tables]


def _normalize_table_names(table_names: Sequence[str] | None) -> list[str]:
    repo_table_order = _repo_table_order()
    if not table_names:
        return repo_table_order

    normalized = []
    seen = set()
    for raw_name in table_names:
        name = str(raw_name or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        normalized.append(name)

    unknown_tables = sorted(set(normalized) - set(repo_table_order))
    if unknown_tables:
        raise ValueError(f"Unsupported table selection: {', '.join(unknown_tables)}")

    selected = set(normalized)
    return [table_name for table_name in repo_table_order if table_name in selected]


def _count_rows(connection: Connection, table_name: str) -> int:
    return int(connection.execute(text(f'SELECT COUNT(*) FROM "{table_name}"')).scalar_one())


def _get_source_and_target_columns(source_inspector, target_inspector, table_name: str) -> tuple[set[str], set[str]]:
    source_columns = {
        column["name"]
        for column in source_inspector.get_columns(table_name)
    }
    target_columns = {
        column["name"]
        for column in target_inspector.get_columns(table_name)
    }
    return source_columns, target_columns


def _get_repo_columns(table_name: str) -> set[str]:
    return {column.name for column in Base.metadata.tables[table_name].columns}


def _get_required_missing_source_columns(table_name: str, missing_columns: Sequence[str]) -> list[str]:
    repo_table = Base.metadata.tables[table_name]
    required = []
    for column_name in missing_columns:
        column = repo_table.columns[column_name]
        if column.primary_key:
            continue
        if column.nullable:
            continue
        if column.default is not None or column.server_default is not None:
            continue
        required.append(column_name)
    return required


def _sync_postgres_sequences(
    target_connection: Connection,
    target_metadata: MetaData,
    table_names: Sequence[str],
) -> list[dict[str, object]]:
    synced_sequences = []

    for table_name in table_names:
        table = target_metadata.tables.get(table_name)
        if table is None:
            continue

        primary_keys = list(table.primary_key.columns)
        if len(primary_keys) != 1:
            continue

        primary_key = primary_keys[0]
        try:
            python_type = primary_key.type.python_type
        except Exception:
            python_type = None
        if python_type is not int:
            continue

        sequence_name = target_connection.execute(
            text("SELECT pg_get_serial_sequence(:table_name, :column_name)"),
            {"table_name": table_name, "column_name": primary_key.name},
        ).scalar()
        if not sequence_name:
            continue

        max_value = target_connection.execute(select(func.max(table.c[primary_key.name]))).scalar()
        if max_value is None:
            continue

        target_connection.execute(
            text(f"SELECT setval('{sequence_name}', :value, true)"),
            {"value": int(max_value)},
        )
        synced_sequences.append(
            {
                "table": table_name,
                "column": primary_key.name,
                "sequence": sequence_name,
                "value": int(max_value),
            }
        )

    return synced_sequences


def run_existing_web_database_migration(
    source_url: str,
    destination_url: str,
    *,
    dry_run: bool = False,
    verify_only: bool = False,
    batch_size: int = DEFAULT_MIGRATION_BATCH_SIZE,
    table_names: Sequence[str] | None = None,
    enforce_backend_contract: bool = True,
) -> dict[str, object]:
    normalized_source_url = str(source_url or "").strip()
    normalized_destination_url = str(destination_url or "").strip()
    selected_tables = _normalize_table_names(table_names)

    blockers: list[str] = []
    warnings: list[str] = []
    table_results: list[dict[str, object]] = []
    source_missing_tables: list[str] = []
    destination_missing_tables: list[str] = []
    source_missing_columns: dict[str, list[str]] = {}
    destination_missing_columns: dict[str, list[str]] = {}
    required_missing_source_columns: dict[str, list[str]] = {}
    sequence_sync: list[dict[str, object]] = []
    migration_error = None

    mode = "verify-only" if verify_only else "dry-run" if dry_run else "migrate"
    source_backend = _get_backend(normalized_source_url)
    destination_backend = _get_backend(normalized_destination_url)

    if verify_only and dry_run:
        blockers.append("verify_only_and_dry_run_are_mutually_exclusive")
    if not normalized_source_url:
        blockers.append("source_url_required")
    if not normalized_destination_url:
        blockers.append("destination_url_required")
    if enforce_backend_contract and source_backend != "sqlite":
        blockers.append(f"source_backend_must_be_sqlite:{source_backend}")
    if enforce_backend_contract and destination_backend != "postgresql":
        blockers.append(f"destination_backend_must_be_postgresql:{destination_backend}")
    if batch_size < 1:
        blockers.append("batch_size_must_be_positive")

    if blockers:
        return {
            "ready": False,
            "mode": mode,
            "source_backend": source_backend,
            "destination_backend": destination_backend,
            "source_url": redact_database_url(normalized_source_url),
            "destination_url": redact_database_url(normalized_destination_url),
            "selected_tables": selected_tables,
            "table_results": table_results,
            "sequence_sync": sequence_sync,
            "source_missing_tables": source_missing_tables,
            "destination_missing_tables": destination_missing_tables,
            "source_missing_columns": source_missing_columns,
            "destination_missing_columns": destination_missing_columns,
            "required_missing_source_columns": required_missing_source_columns,
            "blockers": blockers,
            "warnings": warnings,
            "migration_error": migration_error,
        }

    source_engine = create_engine(normalized_source_url, echo=False)
    target_engine = create_engine(normalized_destination_url, echo=False)

    try:
        with source_engine.connect() as source_connection, target_engine.connect() as target_connection:
            source_inspector = inspect(source_connection)
            target_inspector = inspect(target_connection)
            source_tables = set(source_inspector.get_table_names())
            target_tables = set(target_inspector.get_table_names())

            source_metadata = MetaData()
            target_metadata = MetaData()
            if source_tables:
                source_metadata.reflect(
                    bind=source_connection,
                    only=[table_name for table_name in selected_tables if table_name in source_tables],
                )
            if target_tables:
                target_metadata.reflect(
                    bind=target_connection,
                    only=[table_name for table_name in selected_tables if table_name in target_tables],
                )

            for table_name in selected_tables:
                source_exists = table_name in source_tables
                target_exists = table_name in target_tables
                source_count = _count_rows(source_connection, table_name) if source_exists else 0
                destination_before_count = _count_rows(target_connection, table_name) if target_exists else None

                result = {
                    "table": table_name,
                    "source_exists": source_exists,
                    "destination_exists": target_exists,
                    "source_count": source_count,
                    "destination_before_count": destination_before_count,
                    "destination_after_count": destination_before_count,
                    "copied_rows": 0,
                    "mode": mode,
                }

                if not source_exists:
                    source_missing_tables.append(table_name)
                    warnings.append(f"source_table_missing:{table_name}")
                if not target_exists:
                    destination_missing_tables.append(table_name)
                    blockers.append(f"destination_table_missing:{table_name}")
                    table_results.append(result)
                    continue

                repo_columns = _get_repo_columns(table_name)
                source_columns, target_columns = _get_source_and_target_columns(
                    source_inspector,
                    target_inspector,
                    table_name,
                )

                missing_source = sorted(repo_columns - source_columns)
                missing_target = sorted(repo_columns - target_columns)
                if missing_source:
                    source_missing_columns[table_name] = missing_source
                    warnings.append(f"source_columns_missing:{table_name}:{','.join(missing_source)}")
                    missing_required = _get_required_missing_source_columns(table_name, missing_source)
                    if missing_required:
                        required_missing_source_columns[table_name] = missing_required
                        blockers.append(
                            f"source_required_columns_missing:{table_name}:{','.join(missing_required)}"
                        )
                if missing_target:
                    destination_missing_columns[table_name] = missing_target
                    blockers.append(
                        f"destination_columns_missing:{table_name}:{','.join(missing_target)}"
                    )

                if destination_before_count and not verify_only and not dry_run:
                    blockers.append(f"destination_table_not_empty:{table_name}:{destination_before_count}")

                copy_columns = [
                    column.name
                    for column in Base.metadata.tables[table_name].columns
                    if column.name in source_columns and column.name in target_columns
                ]
                result["copy_columns"] = copy_columns
                result["missing_source_columns"] = list(source_missing_columns.get(table_name, []))
                result["missing_destination_columns"] = list(destination_missing_columns.get(table_name, []))
                table_results.append(result)

            if blockers:
                return {
                    "ready": False,
                    "mode": mode,
                    "source_backend": source_backend,
                    "destination_backend": destination_backend,
                    "source_url": redact_database_url(normalized_source_url),
                    "destination_url": redact_database_url(normalized_destination_url),
                    "selected_tables": selected_tables,
                    "table_results": table_results,
                    "sequence_sync": sequence_sync,
                    "source_missing_tables": source_missing_tables,
                    "destination_missing_tables": destination_missing_tables,
                    "source_missing_columns": source_missing_columns,
                    "destination_missing_columns": destination_missing_columns,
                    "required_missing_source_columns": required_missing_source_columns,
                    "blockers": blockers,
                    "warnings": warnings,
                    "migration_error": migration_error,
                }

            if verify_only:
                for result in table_results:
                    source_count = int(result["source_count"] or 0)
                    destination_count = int(result["destination_before_count"] or 0)
                    if source_count != destination_count:
                        blockers.append(
                            f"row_count_mismatch:{result['table']}:{source_count}:{destination_count}"
                        )
                return {
                    "ready": not blockers,
                    "mode": mode,
                    "source_backend": source_backend,
                    "destination_backend": destination_backend,
                    "source_url": redact_database_url(normalized_source_url),
                    "destination_url": redact_database_url(normalized_destination_url),
                    "selected_tables": selected_tables,
                    "table_results": table_results,
                    "sequence_sync": sequence_sync,
                    "source_missing_tables": source_missing_tables,
                    "destination_missing_tables": destination_missing_tables,
                    "source_missing_columns": source_missing_columns,
                    "destination_missing_columns": destination_missing_columns,
                    "required_missing_source_columns": required_missing_source_columns,
                    "blockers": blockers,
                    "warnings": warnings,
                    "migration_error": migration_error,
                }

            if dry_run:
                return {
                    "ready": True,
                    "mode": mode,
                    "source_backend": source_backend,
                    "destination_backend": destination_backend,
                    "source_url": redact_database_url(normalized_source_url),
                    "destination_url": redact_database_url(normalized_destination_url),
                    "selected_tables": selected_tables,
                    "table_results": table_results,
                    "sequence_sync": sequence_sync,
                    "source_missing_tables": source_missing_tables,
                    "destination_missing_tables": destination_missing_tables,
                    "source_missing_columns": source_missing_columns,
                    "destination_missing_columns": destination_missing_columns,
                    "required_missing_source_columns": required_missing_source_columns,
                    "blockers": blockers,
                    "warnings": warnings,
                    "migration_error": migration_error,
                }

        with source_engine.connect() as source_connection, target_engine.begin() as target_connection:
            source_metadata = MetaData()
            target_metadata = MetaData()
            source_metadata.reflect(
                bind=source_connection,
                only=[table_name for table_name in selected_tables if table_name not in source_missing_tables],
            )
            target_metadata.reflect(
                bind=target_connection,
                only=selected_tables,
            )

            results_by_table = {result["table"]: result for result in table_results}
            for table_name in selected_tables:
                if table_name in source_missing_tables:
                    continue

                source_table = source_metadata.tables[table_name]
                target_table = target_metadata.tables[table_name]
                result = results_by_table[table_name]
                copy_columns = list(result.get("copy_columns") or [])
                if not copy_columns:
                    continue

                query = select(*(source_table.c[column_name] for column_name in copy_columns))
                source_result = source_connection.execute(query).mappings()
                copied_rows = 0
                while True:
                    batch = source_result.fetchmany(batch_size)
                    if not batch:
                        break
                    payload = [{column_name: row[column_name] for column_name in copy_columns} for row in batch]
                    target_connection.execute(target_table.insert(), payload)
                    copied_rows += len(payload)

                result["copied_rows"] = copied_rows
                result["destination_after_count"] = _count_rows(target_connection, table_name)
                expected_count = int(result["source_count"] or 0)
                if copied_rows != expected_count or int(result["destination_after_count"] or 0) != expected_count:
                    raise RuntimeError(f"row_count_mismatch_after_copy:{table_name}")

            if destination_backend == "postgresql":
                sequence_sync = _sync_postgres_sequences(target_connection, target_metadata, selected_tables)

        with target_engine.connect() as target_connection:
            for result in table_results:
                if result["destination_exists"]:
                    result["destination_after_count"] = _count_rows(target_connection, result["table"])
                    if int(result["destination_after_count"] or 0) != int(result["source_count"] or 0):
                        blockers.append(
                            f"row_count_mismatch_after_commit:{result['table']}:{result['source_count']}:{result['destination_after_count']}"
                        )
    except Exception as exc:
        migration_error = str(exc)
        blockers.append("migration_failed")
    finally:
        source_engine.dispose()
        target_engine.dispose()

    return {
        "ready": not blockers,
        "mode": mode,
        "source_backend": source_backend,
        "destination_backend": destination_backend,
        "source_url": redact_database_url(normalized_source_url),
        "destination_url": redact_database_url(normalized_destination_url),
        "selected_tables": selected_tables,
        "table_results": table_results,
        "sequence_sync": sequence_sync,
        "source_missing_tables": source_missing_tables,
        "destination_missing_tables": destination_missing_tables,
        "source_missing_columns": source_missing_columns,
        "destination_missing_columns": destination_missing_columns,
        "required_missing_source_columns": required_missing_source_columns,
        "blockers": blockers,
        "warnings": warnings,
        "migration_error": migration_error,
    }
