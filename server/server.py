"""
Assistentin-MCP: GMX-Mail (IMAP) + iCloud-Kalender (CalDAV) als MCP-Server für Claude.

Mail:     lesen, suchen, Entwürfe anlegen (es wird NIE automatisch gesendet)
Kalender: Kalender auflisten, Termine lesen, Termine eintragen
"""

from __future__ import annotations

import email
import functools
import html
import json
import os
import re
import uuid
from datetime import date, datetime, time, timedelta
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parsedate_to_datetime
from zoneinfo import ZoneInfo

import caldav

import db
from imapclient import IMAPClient

DRAFTS = b"\\Drafts"  # SPECIAL-USE-Flag
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

# ---------------------------------------------------------------- Konfiguration
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Berlin"))

# strip(): Leerzeichen oder Zeilenumbrüche aus dem Coolify-Feld würden den Login scheitern lassen
GMX_USER = os.environ.get("GMX_USER", "").strip()
GMX_PASSWORD = os.environ.get("GMX_PASSWORD", "").strip()
GMX_IMAP_HOST = os.environ.get("GMX_IMAP_HOST", "imap.gmx.net").strip()

ICLOUD_USER = os.environ.get("ICLOUD_USER", "")              # Apple-ID (E-Mail)
ICLOUD_APP_PASSWORD = os.environ.get("ICLOUD_APP_PASSWORD", "")  # App-spezifisches Passwort
ICLOUD_CALDAV_URL = os.environ.get("ICLOUD_CALDAV_URL", "https://caldav.icloud.com")

MAX_BODY_CHARS = 8000

mcp = MCPServer(
    name="assistentin",
    instructions=(
        "Ada: Martins Assistentin. Zugriff auf sein GMX-Postfach, seinen iCloud-Kalender und seine "
        "Projekte, Ideen (Eingang), Aufgaben und Entwürfe. Zeiten sind Europe/Berlin. "
        "Es wird nie etwas gesendet oder veröffentlicht: Mails nur als Entwurf, Posts nur als Entwurf "
        "zur Freigabe im Dashboard. Löschen geht nur im Dashboard."
    ),
)


def _safe(fn):
    """Fehler als lesbare Meldung zurückgeben statt generischem Tool-Fehler."""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as e:
            return {"fehler": f"{type(e).__name__}: {e}"[:500]}
    return wrapper


# ---------------------------------------------------------------- Mail-Helfer
def _imap() -> IMAPClient:
    client = IMAPClient(GMX_IMAP_HOST, ssl=True, timeout=30)
    try:
        if GMX_PASSWORD.isascii():
            client.login(GMX_USER, GMX_PASSWORD)
        else:
            raise ValueError("Nicht-ASCII-Passwort")
    except Exception:
        # Fallback: AUTHENTICATE PLAIN (UTF-8), klappt auch mit Umlauten im Passwort
        client = IMAPClient(GMX_IMAP_HOST, ssl=True, timeout=30)
        client.plain_login(GMX_USER, GMX_PASSWORD)
    return client


def _dec(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _strip_html(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style).*?</\1>", "", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", raw)
    raw = re.sub(r"<[^>]+>", "", raw)
    raw = html.unescape(raw)
    return re.sub(r"\n\s*\n+", "\n\n", raw).strip()


def _body(msg: email.message.Message) -> str:
    plain, htm = None, None
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() == "multipart":
            continue
        if part.get("Content-Disposition", "").lower().startswith("attachment"):
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        text = payload.decode(part.get_content_charset() or "utf-8", "replace")
        if part.get_content_type() == "text/plain" and plain is None:
            plain = text
        elif part.get_content_type() == "text/html" and htm is None:
            htm = text
    body = plain if plain else _strip_html(htm or "")
    return body.strip()


def _attachments(msg: email.message.Message) -> list[str]:
    names = []
    for part in msg.walk():
        fn = part.get_filename()
        if fn:
            names.append(_dec(fn))
    return names


def _summary(uid: int, data: dict) -> dict:
    env = data[b"ENVELOPE"]
    sender = ""
    if env.from_:
        a = env.from_[0]
        name = _dec(a.name) if a.name else ""
        addr = f"{_dec(a.mailbox)}@{_dec(a.host)}"
        sender = f"{name} <{addr}>" if name else addr
    return {
        "uid": uid,
        "datum": env.date.astimezone(TZ).strftime("%Y-%m-%d %H:%M") if env.date else "",
        "von": sender,
        "betreff": _dec(env.subject),
        "gelesen": b"\\Seen" in data.get(b"FLAGS", ()),
    }


# ---------------------------------------------------------------- Mail-Tools
@mcp.tool()
@_safe
def mail_ordner() -> list[str] | dict:
    """Listet alle Mail-Ordner im GMX-Postfach."""
    with _imap() as c:
        return [name for _flags, _delim, name in c.list_folders()]


@mcp.tool()
@_safe
def mails_neueste(tage: int = 1, nur_ungelesen: bool = False, ordner: str = "INBOX", limit: int = 30) -> list[dict] | dict:
    """Mails der letzten `tage` Tage (neueste zuerst). Liefert uid, Datum, Absender, Betreff, gelesen.
    Für den Inhalt danach `mail_lesen(uid)` aufrufen."""
    since = date.today() - timedelta(days=max(tage, 0))
    crit = ["SINCE", since]
    if nur_ungelesen:
        crit.append("UNSEEN")
    with _imap() as c:
        c.select_folder(ordner, readonly=True)
        uids = sorted(c.search(crit), reverse=True)[:limit]
        if not uids:
            return []
        data = c.fetch(uids, ["ENVELOPE", "FLAGS"])
        return [_summary(u, data[u]) for u in uids if u in data]


@mcp.tool()
@_safe
def mails_suchen(suchtext: str = "", absender: str = "", betreff: str = "", tage: int = 90,
                 ordner: str = "INBOX", limit: int = 30) -> list[dict] | dict:
    """Sucht Mails. `suchtext` durchsucht Kopf und Text, `absender`/`betreff` filtern gezielt.
    Zeitraum: letzte `tage` Tage."""
    crit: list = ["SINCE", date.today() - timedelta(days=max(tage, 0))]
    if suchtext:
        crit += ["TEXT", suchtext]
    if absender:
        crit += ["FROM", absender]
    if betreff:
        crit += ["SUBJECT", betreff]
    with _imap() as c:
        c.select_folder(ordner, readonly=True)
        uids = sorted(c.search(crit, charset="UTF-8"), reverse=True)[:limit]
        if not uids:
            return []
        data = c.fetch(uids, ["ENVELOPE", "FLAGS"])
        return [_summary(u, data[u]) for u in uids if u in data]


@mcp.tool()
@_safe
def mail_lesen(uid: int, ordner: str = "INBOX") -> dict:
    """Liest eine Mail vollständig (Text, Empfänger, Anhänge-Namen). Markiert sie NICHT als gelesen."""
    with _imap() as c:
        c.select_folder(ordner, readonly=True)
        data = c.fetch([uid], ["BODY.PEEK[]"])
        if uid not in data:
            return {"fehler": f"Mail {uid} nicht gefunden in {ordner}"}
        msg = email.message_from_bytes(data[uid][b"BODY[]"])
    body = _body(msg)
    try:
        d = parsedate_to_datetime(msg["Date"]).astimezone(TZ).strftime("%Y-%m-%d %H:%M")
    except Exception:
        d = msg.get("Date", "")
    return {
        "uid": uid,
        "datum": d,
        "von": _dec(msg["From"]),
        "an": _dec(msg["To"]),
        "cc": _dec(msg["Cc"]),
        "betreff": _dec(msg["Subject"]),
        "message_id": msg.get("Message-ID", ""),
        "anhaenge": _attachments(msg),
        "text": body[:MAX_BODY_CHARS] + ("\n[… gekürzt]" if len(body) > MAX_BODY_CHARS else ""),
    }


@mcp.tool()
@_safe
def mail_entwurf(an: str, betreff: str, text: str, cc: str = "", antwort_auf_message_id: str = "") -> dict:
    """Legt eine Mail als ENTWURF im GMX-Entwürfe-Ordner ab. Wird nicht gesendet –
    Martin prüft und sendet selbst. Für Antworten die message_id aus `mail_lesen` übergeben."""
    msg = EmailMessage()
    msg["From"] = GMX_USER
    msg["To"] = an
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = betreff
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="gmx.net")
    if antwort_auf_message_id:
        msg["In-Reply-To"] = antwort_auf_message_id
        msg["References"] = antwort_auf_message_id
    msg.set_content(text)
    with _imap() as c:
        drafts = c.find_special_folder(DRAFTS) or "Entwürfe"
        c.append(drafts, msg.as_bytes(), flags=[b"\\Draft"])
    return {"ok": True, "ordner": drafts, "betreff": betreff}


@mcp.tool()
def mail_diagnose() -> dict:
    """Prüft die GMX-Anmeldung und zeigt, woran sie scheitert. Gibt keine Zugangsdaten aus,
    nur Merkmale (Länge, Format, Sonderzeichen) und das Ergebnis je Anmeldeverfahren."""
    raw_user = os.environ.get("GMX_USER", "")
    raw_pw = os.environ.get("GMX_PASSWORD", "")
    user_domain = GMX_USER.rsplit("@", 1)[1] if "@" in GMX_USER else ""
    out: dict = {
        "host": f"{GMX_IMAP_HOST}:993 (SSL)",
        "user_gesetzt": bool(GMX_USER),
        "user_ist_mailadresse": bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]+", GMX_USER, re.I)),
        "user_domain": user_domain,
        "user_hatte_leerzeichen_am_rand": raw_user != raw_user.strip(),
        "passwort_gesetzt": bool(GMX_PASSWORD),
        "passwort_laenge": len(GMX_PASSWORD),
        "passwort_hatte_leerzeichen_am_rand": raw_pw != raw_pw.strip(),
        "passwort_nur_ascii": GMX_PASSWORD.isascii(),
        "passwort_hat_anfuehrungszeichen": any(ch in GMX_PASSWORD for ch in "\"'"),
        "passwort_hat_dollar": "$" in GMX_PASSWORD,
        "passwort_hat_backslash": "\\" in GMX_PASSWORD,
    }
    try:
        c = IMAPClient(GMX_IMAP_HOST, ssl=True, timeout=20)
        out["server_faehigkeiten"] = sorted(x.decode() for x in c.capabilities() if b"AUTH" in x or x == b"IMAP4REV1")
    except Exception as e:
        out["verbindung"] = f"{type(e).__name__}: {e}"[:300]
        return out
    for name, fn in (("LOGIN", "login"), ("AUTH PLAIN", "plain_login")):
        try:
            c = IMAPClient(GMX_IMAP_HOST, ssl=True, timeout=20)
            getattr(c, fn)(GMX_USER, GMX_PASSWORD)
            c.logout()
            out[name] = "ok"
        except Exception as e:
            out[name] = f"{type(e).__name__}: {e}"[:300]
    return out


# ---------------------------------------------------------------- Kalender-Helfer
def _calendars() -> list[caldav.Calendar]:
    client = caldav.DAVClient(url=ICLOUD_CALDAV_URL, username=ICLOUD_USER, password=ICLOUD_APP_PASSWORD)
    return client.principal().calendars()


def _as_dt(v) -> datetime:
    if isinstance(v, datetime):
        return v.astimezone(TZ) if v.tzinfo else v.replace(tzinfo=TZ)
    return datetime.combine(v, time.min, tzinfo=TZ)


def _event_dict(comp, kalender: str) -> dict:
    start = comp.get("DTSTART").dt if comp.get("DTSTART") else None
    end = comp.get("DTEND").dt if comp.get("DTEND") else None
    ganztags = isinstance(start, date) and not isinstance(start, datetime)
    fmt = "%Y-%m-%d" if ganztags else "%Y-%m-%d %H:%M"
    return {
        "kalender": kalender,
        "titel": str(comp.get("SUMMARY", "")),
        "start": _as_dt(start).strftime(fmt) if start else "",
        "ende": _as_dt(end).strftime(fmt) if end else "",
        "ganztags": ganztags,
        "ort": str(comp.get("LOCATION", "")),
        "notiz": str(comp.get("DESCRIPTION", ""))[:1000],
    }


def _parse_when(s: str) -> datetime:
    s = s.strip().replace("T", " ")
    for f in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, f).replace(tzinfo=TZ)
        except ValueError:
            pass
    raise ValueError(f"Datum nicht erkannt: {s!r} (erwartet 'YYYY-MM-DD HH:MM' oder 'YYYY-MM-DD')")


# ---------------------------------------------------------------- Kalender-Tools
@mcp.tool()
@_safe
def kalender_liste() -> list[str] | dict:
    """Listet die Namen aller iCloud-Kalender."""
    return [c.name for c in _calendars()]


@mcp.tool()
@_safe
def termine(ab: str = "", tage: int = 1, kalender: str = "") -> list[dict] | dict:
    """Termine ab Datum `ab` ('YYYY-MM-DD', leer = heute) für `tage` Tage, chronologisch.
    Optional nur ein bestimmter `kalender` (Name aus `kalender_liste`)."""
    start = _parse_when(ab) if ab else datetime.combine(date.today(), time.min, tzinfo=TZ)
    start = start.replace(hour=0, minute=0)
    end = start + timedelta(days=max(tage, 1))
    out = []
    for cal in _calendars():
        if kalender and cal.name != kalender:
            continue
        try:
            found = cal.search(start=start, end=end, event=True, expand=True)
        except Exception as e:  # einzelne Kalender (z. B. Abos) können zicken
            out.append({"kalender": cal.name, "fehler": str(e)[:200]})
            continue
        for ev in found:
            for comp in ev.icalendar_instance.walk("VEVENT"):
                out.append(_event_dict(comp, cal.name))
    return sorted(out, key=lambda e: e.get("start", ""))


@mcp.tool()
@_safe
def termin_eintragen(titel: str, start: str, ende: str = "", kalender: str = "",
                     ort: str = "", notiz: str = "", ganztags: bool = False) -> dict:
    """Trägt einen Termin in den iCloud-Kalender ein.
    start/ende: 'YYYY-MM-DD HH:MM' (oder 'YYYY-MM-DD' bei ganztags). Ohne ende: 1 Stunde.
    Ohne kalender: erster beschreibbarer Kalender."""
    cals = _calendars()
    cal = next((c for c in cals if c.name == kalender), None) if kalender else None
    if kalender and cal is None:
        return {"fehler": f"Kalender {kalender!r} nicht gefunden", "verfuegbar": [c.name for c in cals]}
    cal = cal or cals[0]
    s = _parse_when(start)
    if ganztags:
        dtstart = s.date()
        dtend = (_parse_when(ende).date() if ende else dtstart) + timedelta(days=1)
    else:
        dtstart = s
        dtend = _parse_when(ende) if ende else s + timedelta(hours=1)
    kwargs = dict(dtstart=dtstart, dtend=dtend, summary=titel, uid=str(uuid.uuid4()))
    if ort:
        kwargs["location"] = ort
    if notiz:
        kwargs["description"] = notiz
    cal.save_event(**kwargs)
    return {"ok": True, "kalender": cal.name, "titel": titel, "start": str(dtstart), "ende": str(dtend)}


# ---------------------------------------------------------------- Ada: Helfer
def _projekt_id(con, projekt) -> int | None:
    """Projekt per ID oder Name finden (Groß/klein egal, eindeutiger Teil reicht). Leer = None."""
    if projekt in (None, "", 0):
        return None
    if isinstance(projekt, int) or str(projekt).strip().isdigit():
        row = con.execute("SELECT id FROM projekte WHERE id = ?", (int(projekt),)).fetchone()
        if row:
            return row["id"]
        raise ValueError(f"Projekt mit ID {projekt} gibt es nicht.")
    name = str(projekt).strip()
    row = con.execute("SELECT id FROM projekte WHERE lower(name) = lower(?)", (name,)).fetchone()
    if row:
        return row["id"]
    treffer = con.execute("SELECT id, name FROM projekte WHERE lower(name) LIKE lower(?)",
                          (f"%{name}%",)).fetchall()
    if len(treffer) == 1:
        return treffer[0]["id"]
    alle = [r["name"] for r in con.execute("SELECT name FROM projekte ORDER BY name")]
    if treffer:
        raise ValueError(f"Projekt {name!r} ist nicht eindeutig: {[r['name'] for r in treffer]}")
    raise ValueError(f"Projekt {name!r} nicht gefunden. Vorhanden: {alle}")


def _pruefe(wert, erlaubt: tuple, feld: str):
    if wert not in erlaubt:
        raise ValueError(f"{feld} muss einer dieser Werte sein: {', '.join(erlaubt)}")


def _faellig(s: str) -> str | None:
    s = (s or "").strip()
    if not s:
        return None
    return _parse_when(s).strftime("%Y-%m-%d %H:%M" if " " in s or "T" in s else "%Y-%m-%d")


AUFGABE_SQL = """SELECT a.*, p.name AS projekt FROM aufgaben a
                 LEFT JOIN projekte p ON p.id = a.projekt_id"""


# ---------------------------------------------------------------- Ada: Eingang
@mcp.tool()
@_safe
def idee_notieren(text: str, projekt: str = "", quelle: str = "chat") -> dict:
    """Notiert eine rohe Idee in Martins Eingang. Nutze das, sobald Martin einen Gedanken,
    Einfall oder Wunsch äußert, der nicht sofort erledigt wird. `projekt` (Name oder ID) nur
    angeben, wenn klar ist, wozu die Idee gehört. `quelle`: sprache, chat, mail oder dashboard."""
    _pruefe(quelle, db.EINGANG_QUELLEN, "quelle")
    with db.verbindung() as con:
        pid = _projekt_id(con, projekt)
        cur = con.execute("INSERT INTO eingang (text, quelle, projekt_id, erstellt) VALUES (?,?,?,?)",
                          (text.strip(), quelle, pid, db.jetzt()))
        db.protokollieren(con, "ada", "idee_notiert", {"id": cur.lastrowid, "projekt_id": pid, "text": text[:200]})
        return {"ok": True, "id": cur.lastrowid, "projekt_id": pid}


@mcp.tool()
@_safe
def eingang_anzeigen(status: str = "neu", limit: int = 50) -> list[dict] | dict:
    """Zeigt Ideen im Eingang. `status`: neu (Standard, noch unsortiert), sortiert, verworfen
    oder 'alle'. Neueste zuerst."""
    with db.verbindung() as con:
        sql = "SELECT e.*, p.name AS projekt FROM eingang e LEFT JOIN projekte p ON p.id = e.projekt_id"
        args: list = []
        if status != "alle":
            _pruefe(status, db.EINGANG_STATUS, "status")
            sql += " WHERE e.status = ?"
            args.append(status)
        sql += " ORDER BY e.erstellt DESC LIMIT ?"
        args.append(limit)
        return db.zeilen(con.execute(sql, args))


@mcp.tool()
@_safe
def idee_einsortieren(id: int, projekt: str = "", als_aufgabe: bool = False, aufgabe_titel: str = "",
                      zustaendig: str = "martin", prioritaet: int = 2, verwerfen: bool = False) -> dict:
    """Sortiert eine Idee aus dem Eingang ein: einem Projekt (Name oder ID) zuordnen und
    optional gleich als Aufgabe anlegen (`als_aufgabe`, Titel sonst aus dem Ideentext).
    Mit `verwerfen=True` wird die Idee als verworfen markiert (nicht gelöscht)."""
    with db.verbindung() as con:
        idee = con.execute("SELECT * FROM eingang WHERE id = ?", (id,)).fetchone()
        if not idee:
            return {"fehler": f"Idee {id} gibt es nicht."}
        if verwerfen:
            con.execute("UPDATE eingang SET status = 'verworfen' WHERE id = ?", (id,))
            db.protokollieren(con, "ada", "idee_verworfen", {"id": id})
            return {"ok": True, "id": id, "status": "verworfen"}
        pid = _projekt_id(con, projekt) if projekt else idee["projekt_id"]
        con.execute("UPDATE eingang SET projekt_id = ?, status = 'sortiert' WHERE id = ?", (pid, id))
        out = {"ok": True, "id": id, "projekt_id": pid, "status": "sortiert"}
        if als_aufgabe:
            t = db.jetzt()
            titel = aufgabe_titel.strip() or idee["text"].strip().split("\n")[0][:120]
            cur = con.execute(
                "INSERT INTO aufgaben (titel, beschreibung, projekt_id, zustaendig, prioritaet, erstellt, aktualisiert)"
                " VALUES (?,?,?,?,?,?,?)",
                (titel, idee["text"], pid, zustaendig, prioritaet, t, t))
            out["aufgabe_id"] = cur.lastrowid
        db.protokollieren(con, "ada", "idee_einsortiert", out)
        return out


# ---------------------------------------------------------------- Ada: Projekte
@mcp.tool()
@_safe
def projekte_anzeigen(status: str = "") -> list[dict] | dict:
    """Listet Martins Projekte mit Zahl der offenen Aufgaben und unsortierten Ideen.
    Optional nach `status` filtern: aktiv, ruht, fertig."""
    sql = """SELECT p.id, p.name, p.beschreibung, p.status,
               (SELECT COUNT(*) FROM aufgaben a WHERE a.projekt_id = p.id AND a.status != 'erledigt') AS offene_aufgaben,
               (SELECT COUNT(*) FROM eingang e WHERE e.projekt_id = p.id AND e.status = 'neu') AS neue_ideen
             FROM projekte p"""
    args: list = []
    if status:
        _pruefe(status, db.PROJEKT_STATUS, "status")
        sql += " WHERE p.status = ?"
        args.append(status)
    with db.verbindung() as con:
        return db.zeilen(con.execute(sql + " ORDER BY p.status, p.name", args))


@mcp.tool()
@_safe
def projekt_anlegen(name: str, beschreibung: str = "") -> dict:
    """Legt ein neues Projekt an. Vorher mit `projekte_anzeigen` prüfen, ob es schon existiert."""
    with db.verbindung() as con:
        if con.execute("SELECT 1 FROM projekte WHERE lower(name) = lower(?)", (name.strip(),)).fetchone():
            return {"fehler": f"Projekt {name!r} gibt es schon."}
        cur = con.execute("INSERT INTO projekte (name, beschreibung, erstellt) VALUES (?,?,?)",
                          (name.strip(), beschreibung.strip(), db.jetzt()))
        db.protokollieren(con, "ada", "projekt_angelegt", {"id": cur.lastrowid, "name": name})
        return {"ok": True, "id": cur.lastrowid, "name": name}


@mcp.tool()
@_safe
def projekt_status(projekt: str) -> dict:
    """Überblick zu einem Projekt (Name oder ID): offene Aufgaben, neue Ideen,
    Entwürfe und die letzten Aktivitäten aus dem Protokoll."""
    with db.verbindung() as con:
        pid = _projekt_id(con, projekt)
        if pid is None:
            return {"fehler": "Bitte ein Projekt angeben."}
        p = dict(con.execute("SELECT * FROM projekte WHERE id = ?", (pid,)).fetchone())
        p["offene_aufgaben"] = db.zeilen(con.execute(
            AUFGABE_SQL + " WHERE a.projekt_id = ? AND a.status != 'erledigt' ORDER BY a.prioritaet, a.faellig IS NULL, a.faellig",
            (pid,)))
        p["neue_ideen"] = db.zeilen(con.execute(
            "SELECT id, text, erstellt FROM eingang WHERE projekt_id = ? AND status = 'neu' ORDER BY erstellt DESC", (pid,)))
        p["entwuerfe"] = db.zeilen(con.execute(
            "SELECT id, kanal, status, substr(inhalt, 1, 200) AS inhalt, erstellt FROM entwuerfe"
            " WHERE projekt_id = ? AND status IN ('entwurf', 'freigegeben') ORDER BY erstellt DESC", (pid,)))
        p["zuletzt_erledigt"] = db.zeilen(con.execute(
            "SELECT id, titel, ergebnis, aktualisiert FROM aufgaben WHERE projekt_id = ? AND status = 'erledigt'"
            " ORDER BY aktualisiert DESC LIMIT 5", (pid,)))
        p["letzte_aktivitaeten"] = db.zeilen(con.execute(
            "SELECT zeit, wer, aktion, details FROM protokoll"
            " WHERE json_valid(details) AND json_extract(details, '$.projekt_id') = ?"
            " ORDER BY zeit DESC LIMIT 10", (pid,)))
        return p


# ---------------------------------------------------------------- Ada: Aufgaben
@mcp.tool()
@_safe
def aufgabe_anlegen(titel: str, beschreibung: str = "", projekt: str = "", zustaendig: str = "martin",
                    prioritaet: int = 2, faellig: str = "") -> dict:
    """Legt eine Aufgabe an. `zustaendig`: martin, ada, claude-code, recherche o. ä.
    (claude-code = wird später automatisch von Claude Code bearbeitet).
    `prioritaet`: 1 = hoch, 2 = normal, 3 = niedrig. `faellig`: 'YYYY-MM-DD' oder 'YYYY-MM-DD HH:MM'.
    Die Beschreibung so formulieren, dass der Zuständige ohne Rückfrage loslegen kann."""
    if prioritaet not in (1, 2, 3):
        return {"fehler": "prioritaet muss 1, 2 oder 3 sein."}
    with db.verbindung() as con:
        pid = _projekt_id(con, projekt)
        t = db.jetzt()
        status = "offen"
        cur = con.execute(
            "INSERT INTO aufgaben (titel, beschreibung, projekt_id, status, zustaendig, prioritaet, faellig, erstellt, aktualisiert)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (titel.strip(), beschreibung.strip(), pid, status, zustaendig.strip().lower(), prioritaet,
             _faellig(faellig), t, t))
        db.protokollieren(con, "ada", "aufgabe_angelegt",
                          {"id": cur.lastrowid, "projekt_id": pid, "titel": titel, "zustaendig": zustaendig})
        return {"ok": True, "id": cur.lastrowid, "projekt_id": pid, "status": status}


@mcp.tool()
@_safe
def aufgaben_anzeigen(status: str = "offen_alle", projekt: str = "", zustaendig: str = "",
                      prioritaet: int = 0, faellig_bis: str = "", limit: int = 50) -> list[dict] | dict:
    """Listet Aufgaben, sortiert nach Priorität und Fälligkeit.
    `status`: offen_alle (Standard: alles außer erledigt), alle, oder einer von
    offen, in_arbeit, delegiert, wartet, erledigt. Weitere Filter: `projekt` (Name/ID),
    `zustaendig`, `prioritaet` (1–3), `faellig_bis` ('YYYY-MM-DD')."""
    sql, args, wo = AUFGABE_SQL, [], []
    with db.verbindung() as con:
        if status == "offen_alle":
            wo.append("a.status != 'erledigt'")
        elif status != "alle":
            _pruefe(status, db.AUFGABEN_STATUS, "status")
            wo.append("a.status = ?"); args.append(status)
        if projekt:
            wo.append("a.projekt_id = ?"); args.append(_projekt_id(con, projekt))
        if zustaendig:
            wo.append("a.zustaendig = ?"); args.append(zustaendig.strip().lower())
        if prioritaet:
            wo.append("a.prioritaet = ?"); args.append(prioritaet)
        if faellig_bis:
            wo.append("a.faellig IS NOT NULL AND a.faellig <= ?"); args.append(faellig_bis.strip() + " 99")
        if wo:
            sql += " WHERE " + " AND ".join(wo)
        sql += " ORDER BY a.prioritaet, a.faellig IS NULL, a.faellig, a.erstellt LIMIT ?"
        args.append(limit)
        return db.zeilen(con.execute(sql, args))


@mcp.tool()
@_safe
def aufgabe_aktualisieren(id: int, status: str = "", ergebnis: str = "", titel: str = "", beschreibung: str = "",
                          projekt: str = "", zustaendig: str = "", prioritaet: int = 0, faellig: str = "") -> dict:
    """Ändert eine Aufgabe. Nur angegebene Felder werden geändert.
    `status`: offen, in_arbeit, delegiert, wartet, erledigt. `ergebnis`: Rückmeldung des Bearbeiters
    (wird angehängt, nicht überschrieben). Löschen ist hier nicht möglich, nur im Dashboard."""
    with db.verbindung() as con:
        alt = con.execute("SELECT * FROM aufgaben WHERE id = ?", (id,)).fetchone()
        if not alt:
            return {"fehler": f"Aufgabe {id} gibt es nicht."}
        neu: dict = {}
        if status:
            _pruefe(status, db.AUFGABEN_STATUS, "status"); neu["status"] = status
        if ergebnis:
            neu["ergebnis"] = (alt["ergebnis"] + "\n\n" if alt["ergebnis"] else "") + f"[{db.jetzt()[:16]}] {ergebnis.strip()}"
        if titel:
            neu["titel"] = titel.strip()
        if beschreibung:
            neu["beschreibung"] = beschreibung.strip()
        if projekt:
            neu["projekt_id"] = _projekt_id(con, projekt)
        if zustaendig:
            neu["zustaendig"] = zustaendig.strip().lower()
        if prioritaet:
            if prioritaet not in (1, 2, 3):
                return {"fehler": "prioritaet muss 1, 2 oder 3 sein."}
            neu["prioritaet"] = prioritaet
        if faellig:
            neu["faellig"] = _faellig(faellig)
        if not neu:
            return {"fehler": "Nichts zu ändern angegeben."}
        neu["aktualisiert"] = db.jetzt()
        con.execute(f"UPDATE aufgaben SET {', '.join(k + ' = ?' for k in neu)} WHERE id = ?", [*neu.values(), id])
        db.protokollieren(con, "ada", "aufgabe_aktualisiert",
                          {"id": id, "projekt_id": neu.get("projekt_id", alt["projekt_id"]),
                           **{k: v for k, v in neu.items() if k not in ("aktualisiert", "ergebnis")},
                           **({"ergebnis": ergebnis[:200]} if ergebnis else {})})
        return {"ok": True, "id": id, "geaendert": [k for k in neu if k != "aktualisiert"]}


# ---------------------------------------------------------------- Ada: Entwürfe
@mcp.tool()
@_safe
def entwurf_anlegen(kanal: str, inhalt: str, projekt: str = "", medien: list[str] | None = None) -> dict:
    """Legt einen Entwurf zur Freigabe im Dashboard an. Es wird NICHTS veröffentlicht oder gesendet;
    Martin prüft und gibt im Dashboard frei. `kanal`: mail, instagram, linkedin, tiktok, website.
    `medien`: optionale Liste von Bild-/Video-Links. (Für Mail-Entwürfe direkt im Postfach: `mail_entwurf`.)"""
    _pruefe(kanal, db.ENTWURF_KANAELE, "kanal")
    with db.verbindung() as con:
        pid = _projekt_id(con, projekt)
        cur = con.execute("INSERT INTO entwuerfe (kanal, inhalt, medien, projekt_id, erstellt) VALUES (?,?,?,?,?)",
                          (kanal, inhalt.strip(), json.dumps(medien or [], ensure_ascii=False), pid, db.jetzt()))
        db.protokollieren(con, "ada", "entwurf_angelegt", {"id": cur.lastrowid, "projekt_id": pid, "kanal": kanal})
        return {"ok": True, "id": cur.lastrowid, "status": "entwurf", "hinweis": "Wartet auf Freigabe im Dashboard."}


# ---------------------------------------------------------------- Ada: Briefing
WOCHENTAGE = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")


@mcp.tool()
@_safe
def briefing() -> dict:
    """Alles für Martins Morgen-Briefing in einem Aufruf: Termine heute und morgen,
    ungelesene Mails der letzten 24 h (nur Kopfzeilen), offene Aufgaben nach Priorität,
    unsortierter Eingang, Entwürfe, die auf Freigabe warten, und seit gestern erledigte Aufgaben.
    Fasse das knapp zusammen: Wichtigstes zuerst, nichts erfinden."""
    jetzt = datetime.now(TZ)
    out: dict = {"stand": jetzt.strftime("%Y-%m-%d %H:%M"), "wochentag": WOCHENTAGE[jetzt.weekday()]}
    out["termine_heute_morgen"] = termine(tage=2)
    mails = mails_neueste(tage=1, nur_ungelesen=True, limit=50)
    if isinstance(mails, list):
        grenze = (jetzt - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M")
        mails = [m for m in mails if m.get("datum", "") >= grenze]
    out["ungelesene_mails_24h"] = mails
    gestern = (jetzt - timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")
    with db.verbindung() as con:
        out["offene_aufgaben"] = db.zeilen(con.execute(
            AUFGABE_SQL + " WHERE a.status != 'erledigt'"
            " ORDER BY a.prioritaet, a.faellig IS NULL, a.faellig LIMIT 30"))
        out["eingang_unsortiert"] = db.zeilen(con.execute(
            "SELECT e.id, e.text, e.quelle, p.name AS projekt, e.erstellt FROM eingang e"
            " LEFT JOIN projekte p ON p.id = e.projekt_id WHERE e.status = 'neu' ORDER BY e.erstellt DESC LIMIT 30"))
        out["entwuerfe_zur_freigabe"] = db.zeilen(con.execute(
            "SELECT x.id, x.kanal, substr(x.inhalt, 1, 200) AS inhalt, p.name AS projekt, x.erstellt FROM entwuerfe x"
            " LEFT JOIN projekte p ON p.id = x.projekt_id WHERE x.status = 'entwurf' ORDER BY x.erstellt"))
        out["erledigt_seit_gestern"] = db.zeilen(con.execute(
            AUFGABE_SQL + " WHERE a.status = 'erledigt' AND a.aktualisiert >= ? ORDER BY a.aktualisiert DESC",
            (gestern,)))
        db.protokollieren(con, "ada", "briefing", "")
    return out


# ---------------------------------------------------------------- Start
def _app():
    """Baut die ASGI-App. Antwortet nur unter /<MCP_SECRET>/mcp, sonst 404."""
    secret = os.environ.get("MCP_SECRET", "").strip()
    if len(secret) < 32 or not re.fullmatch(r"[A-Za-z0-9_-]+", secret):
        raise SystemExit("MCP_SECRET fehlt oder ist zu kurz (mind. 32 Zeichen, z. B. openssl rand -hex 32).")
    db.init()
    allowed = [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()]
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(allowed),
        allowed_hosts=allowed,
        allowed_origins=["https://claude.ai", "https://claude.com"],
    )
    return mcp.streamable_http_app(
        streamable_http_path=f"/{secret}/mcp",
        stateless_http=True,
        transport_security=security,
        host="0.0.0.0",
    )


if __name__ == "__main__":
    import uvicorn

    # access_log aus, damit der geheime Pfad nicht in den Coolify-Logs landet
    uvicorn.run(_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), access_log=False)
