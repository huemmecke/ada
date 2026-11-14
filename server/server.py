"""
Assistentin-MCP: GMX-Mail (IMAP) + iCloud-Kalender (CalDAV) als MCP-Server für Claude.

Mail:     lesen, suchen, Entwürfe anlegen (es wird NIE automatisch gesendet)
Kalender: Kalender auflisten, Termine lesen, Termine eintragen
"""

from __future__ import annotations

import email
import functools
import html
import os
import re
import uuid
from datetime import date, datetime, time, timedelta
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parsedate_to_datetime
from zoneinfo import ZoneInfo

import caldav
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
        "Zugriff auf Martins GMX-Postfach und seinen iCloud-Kalender. "
        "Zeiten sind Europe/Berlin. Mails werden nie gesendet, nur als Entwurf abgelegt."
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


# ---------------------------------------------------------------- Start
def _app():
    """Baut die ASGI-App. Antwortet nur unter /<MCP_SECRET>/mcp, sonst 404."""
    secret = os.environ.get("MCP_SECRET", "").strip()
    if len(secret) < 32 or not re.fullmatch(r"[A-Za-z0-9_-]+", secret):
        raise SystemExit("MCP_SECRET fehlt oder ist zu kurz (mind. 32 Zeichen, z. B. openssl rand -hex 32).")
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
