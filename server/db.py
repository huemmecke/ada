"""SQLite-Datenbank für Ada: Projekte, Eingang (Ideen), Aufgaben, Entwürfe, Protokoll.

Liegt unter /data/ada.db (Volume in Coolify). Pfad über ADA_DB änderbar.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Berlin"))
DB_PATH = os.environ.get("ADA_DB", "/data/ada.db")

PROJEKT_STATUS = ("aktiv", "ruht", "fertig")
EINGANG_QUELLEN = ("sprache", "chat", "mail", "dashboard")
EINGANG_STATUS = ("neu", "sortiert", "verworfen")
AUFGABEN_STATUS = ("offen", "in_arbeit", "delegiert", "wartet", "erledigt")
ENTWURF_KANAELE = ("mail", "instagram", "linkedin", "tiktok", "website")
ENTWURF_STATUS = ("entwurf", "freigegeben", "veroeffentlicht", "abgelehnt")

START_PROJEKTE = [
    "LoD2-Aufmaß",
    "Heizkreislauf-Trainer",
    "Synthesizer (Drift / Annäherung / Kurator 76)",
    "Zellgenese-Simulation",
    "Roman „Nyria“",
    "Schwebinsel-Spiel",
    "Romanik/Gotik-Planer",
    "Website",
    "Energieberatung",
    "Wyld",
    "Ada selbst",
]

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS projekte (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    beschreibung TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'aktiv' CHECK (status IN {PROJEKT_STATUS}),
    erstellt TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eingang (
    id INTEGER PRIMARY KEY,
    text TEXT NOT NULL,
    quelle TEXT NOT NULL DEFAULT 'chat' CHECK (quelle IN {EINGANG_QUELLEN}),
    projekt_id INTEGER REFERENCES projekte(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'neu' CHECK (status IN {EINGANG_STATUS}),
    erstellt TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS aufgaben (
    id INTEGER PRIMARY KEY,
    titel TEXT NOT NULL,
    beschreibung TEXT NOT NULL DEFAULT '',
    projekt_id INTEGER REFERENCES projekte(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'offen' CHECK (status IN {AUFGABEN_STATUS}),
    zustaendig TEXT NOT NULL DEFAULT 'martin',
    prioritaet INTEGER NOT NULL DEFAULT 2 CHECK (prioritaet BETWEEN 1 AND 3),
    faellig TEXT,
    ergebnis TEXT NOT NULL DEFAULT '',
    erstellt TEXT NOT NULL,
    aktualisiert TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entwuerfe (
    id INTEGER PRIMARY KEY,
    kanal TEXT NOT NULL CHECK (kanal IN {ENTWURF_KANAELE}),
    inhalt TEXT NOT NULL,
    medien TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'entwurf' CHECK (status IN {ENTWURF_STATUS}),
    projekt_id INTEGER REFERENCES projekte(id) ON DELETE SET NULL,
    erstellt TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS protokoll (
    id INTEGER PRIMARY KEY,
    zeit TEXT NOT NULL,
    wer TEXT NOT NULL,
    aktion TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_aufgaben_status ON aufgaben(status);
CREATE INDEX IF NOT EXISTS ix_eingang_status ON eingang(status);
CREATE INDEX IF NOT EXISTS ix_protokoll_zeit ON protokoll(zeit);
"""


def jetzt() -> str:
    """Aktuelle Zeit als 'YYYY-MM-DD HH:MM:SS' in Martins Zeitzone (sortierbar)."""
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    return con


@contextmanager
def verbindung():
    """Verbindung mit automatischem Commit bzw. Rollback."""
    con = _connect()
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def protokollieren(con: sqlite3.Connection, wer: str, aktion: str, details) -> None:
    if not isinstance(details, str):
        details = json.dumps(details, ensure_ascii=False)
    con.execute("INSERT INTO protokoll (zeit, wer, aktion, details) VALUES (?,?,?,?)",
                (jetzt(), wer, aktion, details))


def init() -> None:
    """Legt Tabellen an und füllt beim ersten Start die Projekte."""
    d = os.path.dirname(DB_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    with verbindung() as con:
        con.executescript(SCHEMA)
        if con.execute("SELECT COUNT(*) FROM projekte").fetchone()[0] == 0:
            t = jetzt()
            con.executemany("INSERT INTO projekte (name, erstellt) VALUES (?, ?)",
                            [(n, t) for n in START_PROJEKTE])
            protokollieren(con, "system", "startdaten", f"{len(START_PROJEKTE)} Projekte angelegt")


def zeilen(rows) -> list[dict]:
    return [dict(r) for r in rows]
