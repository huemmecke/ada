"""Ada-Dashboard: serverseitiges HTML (Jinja2) in derselben App wie der MCP-Server.

Geschützt mit DASHBOARD_PASSWORD und einem signierten Session-Cookie (SESSION_SECRET).
Alles, was hier geändert wird, landet im Protokoll mit wer = 'martin'.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

import db

HIER = Path(__file__).parent
COOKIE = "ada_sitzung"
SITZUNG_TAGE = 30

AUFGABEN_SPALTEN = [
    ("offen", "Offen"),
    ("in_arbeit", "In Arbeit"),
    ("delegiert", "Delegiert"),
    ("wartet", "Wartet"),
    ("erledigt", "Erledigt"),
]
ZUSTAENDIG_VORSCHLAEGE = ["martin", "ada", "claude-code", "recherche"]
WOCHENTAGE = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")

jinja = Environment(loader=FileSystemLoader(HIER / "templates"), autoescape=select_autoescape(["html"]))


def _details(s: str) -> str:
    """Protokoll-Details lesbar machen."""
    try:
        d = json.loads(s)
    except Exception:
        return s
    if isinstance(d, dict):
        return " · ".join(f"{k}: {v}" for k, v in d.items() if v not in (None, "", True))
    return str(d)


jinja.filters["details"] = _details
jinja.globals.update(spalten=AUFGABEN_SPALTEN, status_namen=dict(AUFGABEN_SPALTEN))


# ---------------------------------------------------------------- Sitzung / Login
def _passwort() -> str:
    return os.environ.get("DASHBOARD_PASSWORD", "")


def _geheimnis() -> bytes:
    return os.environ.get("SESSION_SECRET", "").encode()


def eingerichtet() -> bool:
    return bool(_passwort()) and len(_geheimnis()) >= 32


def _signatur(wert: str) -> str:
    # Passwort fließt mit ein: Passwortwechsel meldet alle Sitzungen ab
    schluessel = _geheimnis() + b"|" + hashlib.sha256(_passwort().encode()).digest()
    return hmac.new(schluessel, wert.encode(), hashlib.sha256).hexdigest()


def _cookie_wert() -> str:
    ablauf = str(int(time.time()) + SITZUNG_TAGE * 86400)
    return f"{ablauf}.{_signatur(ablauf)}"


def angemeldet(request: Request) -> bool:
    if not eingerichtet():
        return False
    wert = request.cookies.get(COOKIE, "")
    ablauf, _, sig = wert.partition(".")
    if not ablauf.isdigit() or int(ablauf) < time.time():
        return False
    return hmac.compare_digest(sig, _signatur(ablauf))


_fehlversuche: list[float] = []


def _gleiche_herkunft(request: Request) -> bool:
    """Einfacher CSRF-Schutz: POSTs nur von der eigenen Seite (zusätzlich zu SameSite=Lax)."""
    origin = request.headers.get("origin")
    if not origin or origin == "null":
        return origin is None
    host = request.headers.get("host", "")
    return origin.split("://", 1)[-1] == host


def _sicher(resp: Response) -> Response:
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


def seite(request: Request, vorlage: str, **ctx) -> Response:
    ctx.setdefault("aktiv", "")
    html = jinja.get_template(vorlage).render(request=request, **ctx)
    return _sicher(HTMLResponse(html))


def geschuetzt(fn):
    """Nur mit gültiger Sitzung. POSTs zusätzlich nur von derselben Herkunft."""
    async def wrapper(request: Request):
        if not eingerichtet():
            return _sicher(PlainTextResponse(
                "Dashboard nicht eingerichtet: DASHBOARD_PASSWORD und SESSION_SECRET (mind. 32 Zeichen) "
                "in Coolify setzen.", status_code=503))
        if not angemeldet(request):
            if request.method == "GET":
                return RedirectResponse("/login", status_code=303)
            return _sicher(PlainTextResponse("Nicht angemeldet", status_code=401))
        if request.method == "POST" and not _gleiche_herkunft(request):
            return _sicher(PlainTextResponse("Ungültige Herkunft", status_code=403))
        return await fn(request)
    wrapper.__name__ = fn.__name__
    return wrapper


async def login(request: Request):
    if not eingerichtet():
        return _sicher(PlainTextResponse(
            "Dashboard nicht eingerichtet: DASHBOARD_PASSWORD und SESSION_SECRET in Coolify setzen.", status_code=503))
    fehler = ""
    if request.method == "POST":
        jetzt = time.time()
        _fehlversuche[:] = [t for t in _fehlversuche if t > jetzt - 900]
        if len(_fehlversuche) >= 10:
            return seite(request, "login.html", fehler="Zu viele Fehlversuche. Bitte 15 Minuten warten.")
        form = await request.form()
        if hmac.compare_digest(str(form.get("passwort", "")).encode(), _passwort().encode()):
            resp = RedirectResponse("/", status_code=303)
            resp.set_cookie(COOKIE, _cookie_wert(), max_age=SITZUNG_TAGE * 86400,
                            httponly=True, secure=True, samesite="lax", path="/")
            with db.verbindung() as con:
                db.protokollieren(con, "martin", "angemeldet", "")
            return _sicher(resp)
        _fehlversuche.append(jetzt)
        await asyncio.sleep(1)
        fehler = "Passwort falsch."
    elif angemeldet(request):
        return RedirectResponse("/", status_code=303)
    return seite(request, "login.html", fehler=fehler)


async def logout(request: Request):
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE, path="/")
    return _sicher(resp)


# ---------------------------------------------------------------- Helfer
def _zurueck(form, standard: str) -> RedirectResponse:
    ziel = str(form.get("zurueck", "") or "")
    if not ziel.startswith("/") or ziel.startswith("//"):
        ziel = standard
    return RedirectResponse(ziel, status_code=303)


def _int(v, standard=None):
    try:
        return int(v)
    except (TypeError, ValueError):
        return standard


def _projekte(con, nur_aktiv=False):
    """Projekte für Auswahllisten, als Baum sortiert; `label` ist bei Unterprojekten eingerückt."""
    liste = db.projekte_baum(con, "WHERE status != 'fertig'" if nur_aktiv else "")
    for p in liste:
        p["label"] = "\u00a0\u00a0\u00a0" * p["ebene"] + ("↳ " if p["ebene"] else "") + p["name"]
    return liste


def _faellig(s: str) -> str | None:
    s = (s or "").strip().replace("T", " ")
    if not s:
        return None
    for f in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, f).strftime(f)
        except ValueError:
            pass
    return None


AUFGABE_SQL = "SELECT a.*, p.name AS projekt FROM aufgaben a LEFT JOIN projekte p ON p.id = a.projekt_id"


# ---------------------------------------------------------------- Heute
def baue_routen(termine_fn, mails_fn) -> list:
    """termine_fn/mails_fn kommen aus server.py (Kalender/Mail), damit es keine Zirkel-Importe gibt."""

    @geschuetzt
    async def heute(request: Request):
        jetzt = datetime.now(db.TZ)
        with db.verbindung() as con:
            top = db.zeilen(con.execute(
                AUFGABE_SQL + " WHERE a.status NOT IN ('erledigt') "
                "ORDER BY a.prioritaet, a.faellig IS NULL, a.faellig, a.erstellt LIMIT 8"))
            zahlen = {
                "eingang": con.execute("SELECT COUNT(*) FROM eingang WHERE status = 'neu'").fetchone()[0],
                "freigaben": con.execute("SELECT COUNT(*) FROM entwuerfe WHERE status = 'entwurf'").fetchone()[0],
                "offen": con.execute("SELECT COUNT(*) FROM aufgaben WHERE status != 'erledigt'").fetchone()[0],
                "wartet": con.execute("SELECT COUNT(*) FROM aufgaben WHERE status = 'wartet'").fetchone()[0],
            }
            briefing = con.execute(
                "SELECT zeit FROM protokoll WHERE aktion = 'briefing' ORDER BY zeit DESC LIMIT 1").fetchone()
            erledigt = db.zeilen(con.execute(
                AUFGABE_SQL + " WHERE a.status = 'erledigt' AND a.aktualisiert >= ? ORDER BY a.aktualisiert DESC",
                ((jetzt - timedelta(days=1)).strftime("%Y-%m-%d 00:00:00"),)))
        return seite(request, "heute.html", aktiv="heute", top=top, zahlen=zahlen, erledigt=erledigt,
                     briefing=briefing["zeit"] if briefing else None,
                     datum=f"{WOCHENTAGE[jetzt.weekday()]}, {jetzt.strftime('%d.%m.%Y')}",
                     heute=jetzt.strftime("%Y-%m-%d"))

    @geschuetzt
    async def teil_termine(request: Request):
        tage = min(_int(request.query_params.get("tage"), 2), 14)
        ergebnis = await run_in_threadpool(termine_fn, "", tage, "")
        jetzt = datetime.now(db.TZ)
        return seite(request, "_termine.html", termine=ergebnis, heute=jetzt.strftime("%Y-%m-%d"),
                     morgen=(jetzt + timedelta(days=1)).strftime("%Y-%m-%d"))

    @geschuetzt
    async def teil_mails(request: Request):
        ergebnis = await run_in_threadpool(mails_fn, 1, True, "INBOX", 30)
        return seite(request, "_mails.html", mails=ergebnis)

    # ------------------------------------------------------------ Eingang
    @geschuetzt
    async def eingang(request: Request):
        status = request.query_params.get("status", "neu")
        if status not in db.EINGANG_STATUS:
            status = "neu"
        with db.verbindung() as con:
            ideen = db.zeilen(con.execute(
                "SELECT e.*, p.name AS projekt FROM eingang e LEFT JOIN projekte p ON p.id = e.projekt_id "
                "WHERE e.status = ? ORDER BY e.erstellt DESC LIMIT 200", (status,)))
            projekte = _projekte(con, nur_aktiv=True)
        return seite(request, "eingang.html", aktiv="eingang", ideen=ideen, projekte=projekte, status=status,
                     zustaendige=ZUSTAENDIG_VORSCHLAEGE)

    @geschuetzt
    async def eingang_neu(request: Request):
        form = await request.form()
        text = str(form.get("text", "")).strip()
        if text:
            with db.verbindung() as con:
                pid = _int(form.get("projekt_id"))
                cur = con.execute("INSERT INTO eingang (text, quelle, projekt_id, erstellt) VALUES (?,?,?,?)",
                                  (text, "dashboard", pid, db.jetzt()))
                db.protokollieren(con, "martin", "idee_notiert", {"id": cur.lastrowid, "projekt_id": pid, "text": text[:200]})
        return _zurueck(form, "/eingang")

    @geschuetzt
    async def eingang_aktion(request: Request):
        iid = int(request.path_params["id"])
        form = await request.form()
        aktion = form.get("aktion")
        with db.verbindung() as con:
            idee = con.execute("SELECT * FROM eingang WHERE id = ?", (iid,)).fetchone()
            if not idee:
                return _zurueck(form, "/eingang")
            pid = _int(form.get("projekt_id"), idee["projekt_id"])
            if aktion == "zuordnen":
                con.execute("UPDATE eingang SET projekt_id = ?, status = 'sortiert' WHERE id = ?", (pid, iid))
                db.protokollieren(con, "martin", "idee_einsortiert", {"id": iid, "projekt_id": pid})
            elif aktion == "aufgabe":
                t = db.jetzt()
                titel = str(form.get("titel", "")).strip() or idee["text"].strip().split("\n")[0][:120]
                zustaendig = str(form.get("zustaendig", "martin")).strip().lower() or "martin"
                cur = con.execute(
                    "INSERT INTO aufgaben (titel, beschreibung, projekt_id, zustaendig, prioritaet, erstellt, aktualisiert)"
                    " VALUES (?,?,?,?,?,?,?)", (titel, idee["text"], pid, zustaendig, 2, t, t))
                con.execute("UPDATE eingang SET projekt_id = ?, status = 'sortiert' WHERE id = ?", (pid, iid))
                db.protokollieren(con, "martin", "idee_einsortiert",
                                  {"id": iid, "projekt_id": pid, "aufgabe_id": cur.lastrowid})
            elif aktion == "verwerfen":
                con.execute("UPDATE eingang SET status = 'verworfen' WHERE id = ?", (iid,))
                db.protokollieren(con, "martin", "idee_verworfen", {"id": iid, "projekt_id": idee["projekt_id"]})
            elif aktion == "zurueck":
                con.execute("UPDATE eingang SET status = 'neu' WHERE id = ?", (iid,))
                db.protokollieren(con, "martin", "idee_zurueck_in_eingang", {"id": iid, "projekt_id": idee["projekt_id"]})
            elif aktion == "loeschen":
                con.execute("DELETE FROM eingang WHERE id = ?", (iid,))
                db.protokollieren(con, "martin", "idee_geloescht",
                                  {"id": iid, "projekt_id": idee["projekt_id"], "text": idee["text"][:200]})
        return _zurueck(form, "/eingang")

    # ------------------------------------------------------------ Projekte
    @geschuetzt
    async def projekte(request: Request):
        with db.verbindung() as con:
            zahlen = {r["id"]: dict(r) for r in con.execute(
                """SELECT p.id,
                     (SELECT COUNT(*) FROM aufgaben a WHERE a.projekt_id = p.id AND a.status != 'erledigt') AS offen,
                     (SELECT COUNT(*) FROM eingang e WHERE e.projekt_id = p.id AND e.status = 'neu') AS ideen,
                     (SELECT COUNT(*) FROM entwuerfe x WHERE x.projekt_id = p.id AND x.status = 'entwurf') AS entwuerfe
                   FROM projekte p""")}
            baum = db.projekte_baum(con)
        # Hauptprojekte nach Status gruppieren (aktiv, ruht, fertig); Unterprojekte bleiben unter ihrem Oberprojekt
        rang = {"aktiv": 0, "ruht": 1, "fertig": 2}
        gruppen: list[list[dict]] = []
        for p in baum:
            p.update(zahlen.get(p["id"], {}))
            if p["ebene"] == 0:
                gruppen.append([p])
            else:
                gruppen[-1].append(p)
        gruppen.sort(key=lambda g: rang.get(g[0]["status"], 3))
        liste = [p for g in gruppen for p in g]
        return seite(request, "projekte.html", aktiv="projekte", projekte=liste)

    @geschuetzt
    async def projekt_neu(request: Request):
        form = await request.form()
        name = str(form.get("name", "")).strip()
        if not name:
            return _zurueck(form, "/projekte")
        with db.verbindung() as con:
            vorhanden = con.execute("SELECT id FROM projekte WHERE lower(name) = lower(?)", (name,)).fetchone()
            if vorhanden:
                return RedirectResponse(f"/projekte/{vorhanden['id']}", status_code=303)
            oid = _int(form.get("parent_id"))
            if oid and not con.execute("SELECT 1 FROM projekte WHERE id = ?", (oid,)).fetchone():
                oid = None
            cur = con.execute("INSERT INTO projekte (name, beschreibung, parent_id, erstellt) VALUES (?,?,?,?)",
                              (name, str(form.get("beschreibung", "")).strip(), oid, db.jetzt()))
            db.protokollieren(con, "martin", "projekt_angelegt",
                              {"id": cur.lastrowid, "projekt_id": cur.lastrowid, "name": name, "parent_id": oid})
        return RedirectResponse(f"/projekte/{cur.lastrowid}", status_code=303)

    @geschuetzt
    async def projekt(request: Request):
        pid = int(request.path_params["id"])
        with db.verbindung() as con:
            p = con.execute("SELECT * FROM projekte WHERE id = ?", (pid,)).fetchone()
            if not p:
                return RedirectResponse("/projekte", status_code=303)
            aufgaben = db.zeilen(con.execute(
                AUFGABE_SQL + " WHERE a.projekt_id = ? ORDER BY a.status = 'erledigt', a.prioritaet, "
                "a.faellig IS NULL, a.faellig, a.aktualisiert DESC", (pid,)))
            ideen = db.zeilen(con.execute(
                "SELECT * FROM eingang WHERE projekt_id = ? AND status != 'verworfen' ORDER BY erstellt DESC", (pid,)))
            entwuerfe = db.zeilen(con.execute(
                "SELECT * FROM entwuerfe WHERE projekt_id = ? ORDER BY erstellt DESC", (pid,)))
            verlauf = db.zeilen(con.execute(
                "SELECT * FROM protokoll WHERE json_valid(details) AND json_extract(details, '$.projekt_id') = ? "
                "ORDER BY zeit DESC LIMIT 50", (pid,)))
            ober = con.execute("SELECT id, name FROM projekte WHERE id = ?", (p["parent_id"],)).fetchone() \
                if p["parent_id"] else None
            unter = db.zeilen(con.execute(
                """SELECT p.id, p.name, p.status,
                     (SELECT COUNT(*) FROM aufgaben a WHERE a.projekt_id = p.id AND a.status != 'erledigt') AS offen,
                     (SELECT COUNT(*) FROM eingang e WHERE e.projekt_id = p.id AND e.status = 'neu') AS ideen
                   FROM projekte p WHERE p.parent_id = ? ORDER BY p.name""", (pid,)))
            # mögliche Oberprojekte: alle außer dem Projekt selbst und seinen Unterprojekten
            gesperrt = {pid, *db.unterprojekt_ids(con, pid)}
            moegliche_ober = [x for x in _projekte(con) if x["id"] not in gesperrt]
        return seite(request, "projekt.html", aktiv="projekte", p=dict(p), aufgaben=aufgaben, ideen=ideen,
                     entwuerfe=entwuerfe, verlauf=verlauf, projekt_status=db.PROJEKT_STATUS,
                     ober=dict(ober) if ober else None, unter=unter, moegliche_ober=moegliche_ober,
                     zustaendige=ZUSTAENDIG_VORSCHLAEGE)

    @geschuetzt
    async def projekt_aendern(request: Request):
        pid = int(request.path_params["id"])
        form = await request.form()
        with db.verbindung() as con:
            if form.get("aktion") == "loeschen":
                p = con.execute("SELECT name FROM projekte WHERE id = ?", (pid,)).fetchone()
                con.execute("DELETE FROM projekte WHERE id = ?", (pid,))
                db.protokollieren(con, "martin", "projekt_geloescht", {"id": pid, "name": p["name"] if p else ""})
                return RedirectResponse("/projekte", status_code=303)
            name = str(form.get("name", "")).strip()
            status = form.get("status")
            oid = _int(form.get("parent_id"))
            if oid and (oid == pid or oid in db.unterprojekt_ids(con, pid)
                        or not con.execute("SELECT 1 FROM projekte WHERE id = ?", (oid,)).fetchone()):
                oid = None  # kein Kreis, kein unbekanntes Projekt
            if name and status in db.PROJEKT_STATUS:
                try:
                    con.execute("UPDATE projekte SET name = ?, beschreibung = ?, status = ?, parent_id = ? WHERE id = ?",
                                (name, str(form.get("beschreibung", "")).strip(), status, oid, pid))
                except Exception:
                    pass  # Name doppelt: einfach nichts ändern
                else:
                    db.protokollieren(con, "martin", "projekt_geaendert",
                                      {"id": pid, "projekt_id": pid, "name": name, "status": status, "parent_id": oid})
        return RedirectResponse(f"/projekte/{pid}", status_code=303)

    # ------------------------------------------------------------ Aufgaben
    @geschuetzt
    async def aufgaben(request: Request):
        projekt_filter = _int(request.query_params.get("projekt"))
        with db.verbindung() as con:
            sql = AUFGABE_SQL + " WHERE (a.status != 'erledigt' OR a.aktualisiert >= ?)"
            args: list = [(datetime.now(db.TZ) - timedelta(days=14)).strftime("%Y-%m-%d 00:00:00")]
            if projekt_filter:
                # Oberprojekt zeigt auch die Aufgaben seiner Unterprojekte
                ids = [projekt_filter, *db.unterprojekt_ids(con, projekt_filter)]
                sql += f" AND a.projekt_id IN ({','.join('?' * len(ids))})"
                args.extend(ids)
            alle = db.zeilen(con.execute(
                sql + " ORDER BY a.prioritaet, a.faellig IS NULL, a.faellig, a.aktualisiert DESC", args))
            projekte_liste = _projekte(con)
        spalten = {k: [a for a in alle if a["status"] == k] for k, _ in AUFGABEN_SPALTEN}
        return seite(request, "aufgaben.html", aktiv="aufgaben", spalten_daten=spalten, projekte=projekte_liste,
                     projekt_filter=projekt_filter, zustaendige=ZUSTAENDIG_VORSCHLAEGE,
                     heute=datetime.now(db.TZ).strftime("%Y-%m-%d"))

    @geschuetzt
    async def aufgabe_neu(request: Request):
        form = await request.form()
        titel = str(form.get("titel", "")).strip()
        if titel:
            t = db.jetzt()
            prio = _int(form.get("prioritaet"), 2)
            pid = _int(form.get("projekt_id"))
            zustaendig = str(form.get("zustaendig", "martin")).strip().lower() or "martin"
            with db.verbindung() as con:
                cur = con.execute(
                    "INSERT INTO aufgaben (titel, beschreibung, projekt_id, zustaendig, prioritaet, faellig, erstellt, aktualisiert)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (titel, str(form.get("beschreibung", "")).strip(), pid, zustaendig,
                     prio if prio in (1, 2, 3) else 2, _faellig(str(form.get("faellig", ""))), t, t))
                db.protokollieren(con, "martin", "aufgabe_angelegt",
                                  {"id": cur.lastrowid, "projekt_id": pid, "titel": titel, "zustaendig": zustaendig})
        return _zurueck(form, "/aufgaben")

    @geschuetzt
    async def aufgabe(request: Request):
        aid = int(request.path_params["id"])
        with db.verbindung() as con:
            a = con.execute(AUFGABE_SQL + " WHERE a.id = ?", (aid,)).fetchone()
            if not a:
                return RedirectResponse("/aufgaben", status_code=303)
            verlauf = db.zeilen(con.execute(
                "SELECT * FROM protokoll WHERE aktion LIKE 'aufgabe%' AND json_valid(details) "
                "AND json_extract(details, '$.id') = ? ORDER BY zeit DESC LIMIT 30", (aid,)))
            projekte_liste = _projekte(con)
        return seite(request, "aufgabe.html", aktiv="aufgaben", a=dict(a), projekte=projekte_liste,
                     verlauf=verlauf, zustaendige=ZUSTAENDIG_VORSCHLAEGE)

    @geschuetzt
    async def aufgabe_aendern(request: Request):
        aid = int(request.path_params["id"])
        form = await request.form()
        aktion = form.get("aktion") or ("status" if form.get("nur_status") else "speichern")
        with db.verbindung() as con:
            alt = con.execute("SELECT * FROM aufgaben WHERE id = ?", (aid,)).fetchone()
            if not alt:
                return _zurueck(form, "/aufgaben")
            if aktion == "loeschen":
                con.execute("DELETE FROM aufgaben WHERE id = ?", (aid,))
                db.protokollieren(con, "martin", "aufgabe_geloescht",
                                  {"id": aid, "projekt_id": alt["projekt_id"], "titel": alt["titel"]})
                ziel = str(form.get("zurueck", ""))
                if ziel.startswith(f"/aufgaben/{aid}"):
                    form = {"zurueck": "/aufgaben"}
                return _zurueck(form, "/aufgaben")
            neu: dict = {}
            if aktion == "erledigt":
                neu["status"] = "erledigt"
            elif aktion == "status":
                if form.get("status") in db.AUFGABEN_STATUS:
                    neu["status"] = form.get("status")
            else:  # speichern
                titel = str(form.get("titel", "")).strip()
                if titel:
                    neu["titel"] = titel
                neu["beschreibung"] = str(form.get("beschreibung", "")).strip()
                neu["projekt_id"] = _int(form.get("projekt_id"))
                if form.get("status") in db.AUFGABEN_STATUS:
                    neu["status"] = form.get("status")
                neu["zustaendig"] = str(form.get("zustaendig", "")).strip().lower() or "martin"
                prio = _int(form.get("prioritaet"), alt["prioritaet"])
                neu["prioritaet"] = prio if prio in (1, 2, 3) else alt["prioritaet"]
                neu["faellig"] = _faellig(str(form.get("faellig", "")))
                neu["ergebnis"] = str(form.get("ergebnis", "")).strip()
            geaendert = {k: v for k, v in neu.items() if alt[k] != v}
            if geaendert:
                geaendert["aktualisiert"] = db.jetzt()
                con.execute(f"UPDATE aufgaben SET {', '.join(k + ' = ?' for k in geaendert)} WHERE id = ?",
                            [*geaendert.values(), aid])
                log = {k: v for k, v in geaendert.items() if k not in ("aktualisiert", "beschreibung", "ergebnis")}
                db.protokollieren(con, "martin", "aufgabe_aktualisiert",
                                  {"id": aid, "projekt_id": neu.get("projekt_id", alt["projekt_id"]), **log})
        if request.headers.get("x-ada-fetch"):
            return _sicher(JSONResponse({"ok": True}))
        return _zurueck(form, "/aufgaben")

    # ------------------------------------------------------------ Freigaben
    @geschuetzt
    async def freigaben(request: Request):
        with db.verbindung() as con:
            offen = db.zeilen(con.execute(
                "SELECT x.*, p.name AS projekt FROM entwuerfe x LEFT JOIN projekte p ON p.id = x.projekt_id "
                "WHERE x.status = 'entwurf' ORDER BY x.erstellt"))
            erledigt = db.zeilen(con.execute(
                "SELECT x.*, p.name AS projekt FROM entwuerfe x LEFT JOIN projekte p ON p.id = x.projekt_id "
                "WHERE x.status != 'entwurf' ORDER BY x.erstellt DESC LIMIT 30"))
            projekte_liste = _projekte(con)
        for x in offen + erledigt:
            try:
                x["medien_liste"] = json.loads(x["medien"] or "[]")
            except Exception:
                x["medien_liste"] = []
        return seite(request, "freigaben.html", aktiv="freigaben", offen=offen, erledigt=erledigt,
                     projekte=projekte_liste, kanaele=db.ENTWURF_KANAELE)

    @geschuetzt
    async def entwurf_aendern(request: Request):
        eid = int(request.path_params["id"])
        form = await request.form()
        aktion = form.get("aktion")
        with db.verbindung() as con:
            alt = con.execute("SELECT * FROM entwuerfe WHERE id = ?", (eid,)).fetchone()
            if not alt:
                return _zurueck(form, "/freigaben")
            if aktion == "loeschen":
                con.execute("DELETE FROM entwuerfe WHERE id = ?", (eid,))
                db.protokollieren(con, "martin", "entwurf_geloescht",
                                  {"id": eid, "projekt_id": alt["projekt_id"], "kanal": alt["kanal"]})
                return _zurueck(form, "/freigaben")
            inhalt = str(form.get("inhalt", alt["inhalt"])).strip() or alt["inhalt"]
            kanal = form.get("kanal") if form.get("kanal") in db.ENTWURF_KANAELE else alt["kanal"]
            pid = _int(form.get("projekt_id"), None) if "projekt_id" in form else alt["projekt_id"]
            status = {"freigeben": "freigegeben", "ablehnen": "abgelehnt", "zurueck": "entwurf"}.get(aktion, alt["status"])
            # 'veroeffentlicht' setzt erst Phase 5, nach Klick auf „Jetzt veröffentlichen“
            con.execute("UPDATE entwuerfe SET inhalt = ?, kanal = ?, projekt_id = ?, status = ? WHERE id = ?",
                        (inhalt, kanal, pid, status, eid))
            db.protokollieren(con, "martin", f"entwurf_{status if status != alt['status'] else 'bearbeitet'}",
                              {"id": eid, "projekt_id": pid, "kanal": kanal})
        return _zurueck(form, "/freigaben")

    # ------------------------------------------------------------ Protokoll
    @geschuetzt
    async def protokoll(request: Request):
        wer = request.query_params.get("wer", "")
        with db.verbindung() as con:
            if wer:
                eintraege = db.zeilen(con.execute(
                    "SELECT * FROM protokoll WHERE wer = ? ORDER BY zeit DESC, id DESC LIMIT 300", (wer,)))
            else:
                eintraege = db.zeilen(con.execute("SELECT * FROM protokoll ORDER BY zeit DESC, id DESC LIMIT 300"))
        return seite(request, "protokoll.html", aktiv="protokoll", eintraege=eintraege, wer=wer)

    return [
        Route("/", heute),
        Route("/login", login, methods=["GET", "POST"]),
        Route("/logout", logout, methods=["POST"]),
        Route("/teil/termine", teil_termine),
        Route("/teil/mails", teil_mails),
        Route("/eingang", eingang),
        Route("/eingang/neu", eingang_neu, methods=["POST"]),
        Route("/eingang/{id:int}", eingang_aktion, methods=["POST"]),
        Route("/projekte", projekte),
        Route("/projekte/neu", projekt_neu, methods=["POST"]),
        Route("/projekte/{id:int}", projekt, methods=["GET"]),
        Route("/projekte/{id:int}/aendern", projekt_aendern, methods=["POST"]),
        Route("/aufgaben", aufgaben),
        Route("/aufgaben/neu", aufgabe_neu, methods=["POST"]),
        Route("/aufgaben/{id:int}", aufgabe, methods=["GET"]),
        Route("/aufgaben/{id:int}/aendern", aufgabe_aendern, methods=["POST"]),
        Route("/freigaben", freigaben),
        Route("/entwuerfe/{id:int}", entwurf_aendern, methods=["POST"]),
        Route("/protokoll", protokoll),
        Mount("/static", app=StaticFiles(directory=HIER / "static"), name="static"),
    ]
