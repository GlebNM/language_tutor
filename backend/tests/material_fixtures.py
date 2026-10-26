"""Restore actual legacy table layouts when fixtures downgrade a schema version."""

import sqlite3

from backend.app.materials import SCHEMA


def restore_timed_material_schema(db: sqlite3.Connection) -> None:
    """Keep linked video rows while undoing later text-material schema changes."""
    assert all(row[0] == "video" for row in db.execute("SELECT kind FROM materials"))
    db.execute("DROP TABLE IF EXISTS reading_positions")
    for table in ("materials", "cues"):
        definition = next(
            statement.strip()
            for statement in SCHEMA.split(";")
            if statement.strip().startswith(f"CREATE TABLE {table}(")
        )
        db.execute(definition.replace(f"CREATE TABLE {table}(", f"CREATE TABLE {table}_legacy("))
        columns = ",".join(row[1] for row in db.execute(f"PRAGMA table_info({table}_legacy)"))
        db.execute(f"INSERT INTO {table}_legacy ({columns}) SELECT {columns} FROM {table}")
    db.execute("DROP TABLE cues")
    db.execute("DROP TABLE materials")
    db.execute("ALTER TABLE materials_legacy RENAME TO materials")
    db.execute("ALTER TABLE cues_legacy RENAME TO cues")
    db.execute("CREATE INDEX cues_material ON cues(material_id,position)")
