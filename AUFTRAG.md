# Auftrag: „Ada“ – persönliche Assistentin mit Dashboard

Du baust für Martin ein System namens **Ada**. Ada ist seine persönliche Assistentin in der Claude-App. Sie ordnet seine Ideen, kennt seine Mails und Termine, legt Aufgaben an und delegiert sie. Alles, was Ada tut, sieht Martin in einem **Web-Dashboard auf seinem Hetzner-Server** und kann es dort auch selbst bearbeiten.

Arbeite die Phasen **nacheinander** ab. **Halte nach jeder Phase an**, sag Martin knapp, was er testen soll, und warte auf sein OK.

---

## Ausgangslage

- `server/` enthält einen funktionierenden MCP-Server (Python, `mcp` 2.x, `MCPServer`, streamable HTTP) mit diesen Tools:
  - **Mail über GMX/IMAP:** `mails_neueste`, `mails_suchen`, `mail_lesen`, `mail_entwurf`, `mail_ordner`. Mails werden **nie** gesendet, nur Entwürfe abgelegt.
  - **Kalender über iCloud/CalDAV:** `kalender_liste`, `termine`, `termin_eintragen`.
- Der Server ist lokal getestet, aber noch nicht gegen das echte GMX und iCloud.
- **Hosting:** Hetzner vServer mit **Coolify**. Coolify übernimmt Domain, HTTPS und Deployment aus einem Git-Repo. Caddy wird nicht gebraucht und ist schon entfernt.
- **Repo:** privates GitHub-Repo. Coolify deployt aus GitHub.
- **Martins Rechner:** Mac (Apple Silicon, 32 GB). Seine Website ist ein Astro-Projekt, liegt lokal und wird mit Cursor gebaut.

## Grundregeln

- **Sprache:** Code-Kommentare, UI-Texte und Tool-Beschreibungen auf Deutsch.
- **Einfach halten:** Python, SQLite, so wenige Abhängigkeiten wie möglich. Kein Build-Schritt fürs Frontend, wenn es sich vermeiden lässt: serverseitiges HTML mit HTMX oder ein bisschen Vanilla-JS reicht.
- **Geheimnisse:** Zugangsdaten nur in Umgebungsvariablen. In Coolify trägt Martin sie selbst ein. **Niemals** Zugangsdaten in Dateien, ins Repo oder in den Chat schreiben. Martin soll sie auch nicht in den Chat kopieren.
- **Nichts nach außen ohne Freigabe:** Ada sendet keine Mails und postet nichts. Sie legt nur Entwürfe an. Was nach außen geht, gibt Martin im Dashboard per Klick frei.
- **Nichts löschen:** Die MCP-Tools können nichts löschen. Löschen geht nur im Dashboard.

---

## Phase 1 – Mail- und Kalenderserver live bringen (Coolify)

1. Mach aus dem Projekt ein Git-Repo und richte mit Martin ein **privates** GitHub-Repo ein (`gh` CLI, falls vorhanden). `.env` gehört in die `.gitignore`.
2. **Geheimen Pfad in die App verlegen:** Bisher hat Caddy den geheimen Pfad geprüft. Jetzt soll der Server selbst unter `/<MCP_SECRET>/mcp` antworten und auf alles andere mit 404. Ohne gesetztes `MCP_SECRET` startet er gar nicht.
3. Passe `docker-compose.yml` an Coolify an:
   - Es gibt einen Dienst `app` mit Port 8000.
   - Es gibt **keine** `ports:`-Freigabe nach außen, das Routing macht Coolify.
   - Für Phase 2 kommt ein Volume `/data` hinzu.
4. Schreib Martin eine kurze Schritt-für-Schritt-Anleitung für Coolify:
   - neue Ressource aus dem GitHub-Repo anlegen, Build-Typ Docker Compose
   - Domain zuweisen, z. B. `ada.<seine-domain>`
   - Umgebungsvariablen eintragen
   - deployen
5. Erklär ihm die Vorbereitungen:
   - In GMX unter Einstellungen → POP3 & IMAP den Zugriff erlauben. Bei aktiver Zwei-Faktor-Anmeldung braucht er dort ein anwendungsspezifisches Passwort.
   - Auf account.apple.com ein App-spezifisches Passwort erzeugen.
6. Den Connector in Claude hinzufügen: Einstellungen → Connectors → eigener Connector mit der URL `https://ada.<domain>/<MCP_SECRET>/mcp`.
7. **Test:** Martin fragt in der Claude-App „Was steht heute an?“ und bekommt echte Mails und Termine. Bei Fehlern mit IMAP oder CalDAV debuggst du mit ihm anhand der Coolify-Logs.

## Phase 2 – Datenbank und MCP-Werkzeuge für Ada

SQLite unter `/data/ada.db`. Tabellen, gern schlank:

- `projekte`: id, name, beschreibung, status (aktiv/ruht/fertig), erstellt
- `eingang`: id, text, quelle (sprache/chat/mail/dashboard), projekt_id (nullable), status (neu/sortiert/verworfen), erstellt. Das ist der Ort für rohe Ideen.
- `aufgaben`: id, titel, beschreibung, projekt_id, status (offen/in_arbeit/delegiert/wartet/erledigt), zustaendig (martin/ada/claude-code/recherche/…), prioritaet (1–3), faellig, ergebnis (Text, den der Bearbeiter zurückmeldet), erstellt, aktualisiert
- `entwuerfe`: id, kanal (mail/instagram/linkedin/tiktok/website), inhalt, medien (JSON), status (entwurf/freigegeben/veroeffentlicht/abgelehnt), projekt_id, erstellt
- `protokoll`: id, zeit, wer (ada/martin/system/worker), aktion, details. Jede Änderung wird hier eingetragen.

Startdaten für die Projekte (Martin kann sie im Dashboard ändern):

- LoD2-Aufmaß
- Heizkreislauf-Trainer
- Synthesizer (Drift / Annäherung / Kurator 76)
- Zellgenese-Simulation
- Roman „Nyria“
- Schwebinsel-Spiel
- Romanik/Gotik-Planer
- Website
- Energieberatung
- Wyld
- Ada selbst

Neue MCP-Tools, alle mit klaren deutschen Beschreibungen, weil Claude sich danach richtet:

- `idee_notieren(text, projekt?)`: Rohidee in den Eingang
- `eingang_anzeigen(status?)`
- `idee_einsortieren(id, projekt, als_aufgabe?)`
- `projekte_anzeigen()`, `projekt_anlegen(name, beschreibung)`, `projekt_status(id)` mit offenen Aufgaben und letzten Aktivitäten
- `aufgabe_anlegen(titel, beschreibung, projekt, zustaendig, prioritaet?, faellig?)`
- `aufgaben_anzeigen(filter…)`, `aufgabe_aktualisieren(id, status?, ergebnis?, …)`
- `entwurf_anlegen(kanal, inhalt, projekt?)`: Der Entwurf landet im Dashboard zur Freigabe.
- `briefing()`: liefert in einem Aufruf die Termine heute und morgen, ungelesene Mails der letzten 24 h (nur die Kopfzeilen), offene Aufgaben nach Priorität, den unsortierten Eingang, Entwürfe, die auf Freigabe warten, und Aufgaben, die seit gestern erledigt wurden. Das ist die Grundlage für Adas Morgen-Briefing.

**Test:** In der Claude-App eine Idee diktieren. Sie taucht in der DB auf und lässt sich in ein Projekt einsortieren.

## Phase 3 – Dashboard

Das Dashboard läuft in derselben App unter `https://ada.<domain>/`, ist aber **getrennt geschützt**: Login mit Passwort aus `DASHBOARD_PASSWORD` und Session-Cookie (httpOnly, secure). Ohne Login sieht man nichts.

Ansichten:

- **Heute:** Termine, wichtige Mails, Top-Aufgaben, letzter Briefing-Stand
- **Eingang:** Ideen sortieren, einem Projekt zuordnen, in eine Aufgabe umwandeln, verwerfen
- **Projekte:** Liste mit Detailseite (Aufgaben, Ideen, Entwürfe, Verlauf)
- **Aufgaben:** Kanban nach Status (offen / in Arbeit / delegiert / wartet / erledigt). Karten lassen sich per Drag & Drop verschieben, bearbeiten, abhaken und löschen.
- **Freigaben:** Entwürfe ansehen, bearbeiten, freigeben oder ablehnen
- **Protokoll:** Wer hat wann was gemacht

Gestaltung:

- ruhig, typografisch, viel Weißraum, dunkler Modus
- funktioniert auf dem iPhone, Martin schaut morgens mobil rein
- kein Framework-Overkill

**Test:** Martin legt im Dashboard eine Aufgabe an, und Ada sieht sie in der Claude-App. Ada legt eine an, und sie erscheint im Dashboard.

## Phase 4 – Delegation an Claude Code (Tickets)

Ziel: Martin sagt Ada zum Beispiel „Auf der Website soll der Kontaktbutton grün werden“. Ada legt eine Aufgabe mit `zustaendig = claude-code` und dem Projekt „Website“ an. Danach wird die Aufgabe automatisch bearbeitet und das Ergebnis zurückgemeldet.

Umsetzung auf Martins Mac:

1. Die App bekommt eine kleine **Worker-API** (`/api/worker/...`) mit eigenem Token `WORKER_TOKEN`:
   - offene Claude-Code-Aufgaben abholen
   - Status setzen
   - Ergebnis melden
2. Ein Skript `worker/ada-worker.sh` (oder Python) auf dem Mac:
   - fragt alle 5 Minuten neue Aufgaben ab
   - ordnet das Projekt über eine Konfigurationsdatei `worker/projekte.json` einem lokalen Ordner zu
   - startet `claude -p "<Aufgabe>"` in diesem Ordner, nicht interaktiv, mit sinnvoll eingeschränkten Rechten
   - arbeitet **immer auf einem eigenen Git-Branch** `ada/<id>-<slug>` und committet dort
   - pusht nie auf `main`
   - meldet am Ende eine Zusammenfassung und den Branch-Namen zurück und setzt den Status auf `wartet`: Martin prüft und merged selbst.
3. Einrichtung als `launchd`-Agent, damit der Worker nach dem Login läuft. Schreib eine Anleitung dazu.

**Alternative, mit Martin abstimmen:** Liegt ein Projekt auf GitHub, kann Ada stattdessen ein GitHub-Issue anlegen, und die Claude-GitHub-Integration bearbeitet es per Pull Request. Dann muss der Mac nicht laufen.

## Phase 5 – Social Media (später, erst nach Rückfrage)

- Instagram über die Graph API (Business- oder Creator-Konto, mit Facebook-Seite verknüpft) und LinkedIn über die Posts-API fürs eigene Profil. Bei TikTok erst prüfen, ob sich der Prüfprozess lohnt.
- API-Keys und Tokens nur als Umgebungsvariablen in Coolify.
- Veröffentlicht wird **nur**, was im Dashboard auf `freigegeben` steht, und erst nach Klick auf „Jetzt veröffentlichen“.
- Optional: einfache Zahlen (Reichweite, Likes) ins Dashboard holen, wenn die API das hergibt.

## Phase 6 – Ada selbst

- `ada-skill/SKILL.md` enthält Adas Persönlichkeit und ihren Ablauf. Martin fügt das in der Claude-App als Skill hinzu.
- Morgen-Briefing: Martin legt es in der Claude-App als geplante Aufgabe an, werktags morgens. Ada ruft `briefing()` auf und fasst kurz zusammen. Sprechen kann Martin danach im Sprachmodus der App mit ihr.

---

## Umgebungsvariablen (Übersicht)

```
MCP_SECRET=            # openssl rand -hex 32
DASHBOARD_PASSWORD=
SESSION_SECRET=        # openssl rand -hex 32
WORKER_TOKEN=          # openssl rand -hex 32
GMX_USER=
GMX_PASSWORD=
ICLOUD_USER=
ICLOUD_APP_PASSWORD=
TZ_NAME=Europe/Berlin
# Phase 5:
INSTAGRAM_ACCESS_TOKEN=
INSTAGRAM_ACCOUNT_ID=
LINKEDIN_ACCESS_TOKEN=
```

## Am Ende jeder Phase

- Die README ist aktualisiert: Was gibt es neu, wie testet man es?
- Committet und gepusht, damit Coolify neu deployen kann.
- Kurze Meldung an Martin: Was ist fertig, was soll er testen, was ist offen?
