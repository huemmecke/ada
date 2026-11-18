# Ada

Persönliche Assistentin für Martin: MCP-Server für Claude (Mail, Kalender; später Ideen, Aufgaben, Dashboard). Läuft auf Hetzner mit Coolify. Den ganzen Plan findest du in `AUFTRAG.md`.

## Stand: Phase 3 – Dashboard

Das Dashboard läuft in derselben App unter `https://ada.<deine-domain>/`. Es ist getrennt vom MCP-Pfad geschützt: Login mit `DASHBOARD_PASSWORD`, danach ein signiertes Session-Cookie (httpOnly, secure, 30 Tage). Wenn du das Passwort änderst, sind alle Sitzungen abgemeldet. Nach 10 Fehlversuchen in 15 Minuten ist der Login kurz gesperrt.

**Neue Umgebungsvariablen in Coolify:**
- `DASHBOARD_PASSWORD` – dein Dashboard-Passwort
- `SESSION_SECRET` – `openssl rand -hex 32`

Fehlen sie, antwortet das Dashboard mit „nicht eingerichtet“. Der MCP-Server läuft trotzdem.

| Ansicht | Inhalt |
|---|---|
| Heute | Termine heute/morgen, ungelesene Mails, wichtigste Aufgaben, Zähler, letztes Briefing, gestern Erledigtes |
| Eingang | Ideen notieren, einem Projekt zuordnen, als Aufgabe anlegen, verwerfen; Verworfenes zurückholen oder löschen |
| Projekte | Liste mit Zählern, Unterprojekte eingerückt unter ihrem Oberprojekt; Detailseite mit Unterprojekten, Aufgaben, Ideen, Entwürfen, Verlauf; bearbeiten (auch „Gehört zu“), löschen |
| Aufgaben | Kanban (offen / in Arbeit / delegiert / wartet / erledigt), Drag & Drop am Rechner, Status-Auswahl und Haken auf jeder Karte (auch am iPhone), Detailseite zum Bearbeiten und Löschen |
| Freigaben | Entwürfe bearbeiten, freigeben, ablehnen. Freigeben veröffentlicht noch nichts (das kommt in Phase 5). |
| Protokoll | Wer hat wann was gemacht, filterbar nach Ada / Martin / Worker / System |

Am iPhone: Seite in Safari öffnen → Teilen → „Zum Home-Bildschirm“.

**Test Phase 3:** Im Dashboard eine Aufgabe anlegen und in Claude fragen „Welche Aufgaben sind offen?“. Umgekehrt Ada eine Aufgabe anlegen lassen und im Dashboard nachsehen.

## Phase 2 – Datenbank und Ada-Werkzeuge

Neu: SQLite unter `/data/ada.db` (Volume `ada-data`, bleibt bei Redeploys erhalten). Beim ersten Start werden die Tabellen angelegt und die 11 Startprojekte eingetragen. Jede Änderung landet in der Tabelle `protokoll`.

| Tool | Funktion |
|---|---|
| `idee_notieren` | Rohe Idee in den Eingang, optional mit Projekt |
| `eingang_anzeigen` | Ideen nach Status (neu / sortiert / verworfen / alle) |
| `idee_einsortieren` | Idee einem Projekt zuordnen, optional gleich als Aufgabe anlegen oder verwerfen |
| `projekte_anzeigen` | Projekte als Baum (Ober- und Unterprojekte) mit Zahl der offenen Aufgaben und neuen Ideen |
| `projekt_aendern` | Projekt umbenennen, Beschreibung/Status ändern, unter ein Oberprojekt hängen (`oberprojekt="-"` löst es wieder) |
| `projekt_anlegen` | Neues Projekt, optional mit `oberprojekt` |
| `projekt_status` | Ein Projekt: offene Aufgaben, Ideen, Entwürfe, letzte Aktivitäten |
| `aufgabe_anlegen` | Aufgabe mit Projekt, Zuständigkeit, Priorität 1–3, Fälligkeit |
| `aufgaben_anzeigen` | Aufgaben filtern nach Status, Projekt, Zuständig, Priorität, Fälligkeit |
| `aufgabe_aktualisieren` | Status, Ergebnis (wird angehängt) und andere Felder ändern |
| `entwurf_anlegen` | Entwurf für mail/instagram/linkedin/tiktok/website, wartet auf Freigabe |
| `briefing` | Termine heute und morgen, ungelesene Mails (24 h), Aufgaben, Eingang, Freigaben, gestern Erledigtes |

Projekte kann man per Name oder ID angeben. Ein eindeutiger Teil des Namens reicht, z. B. „nyria“ oder „synth“. Die Tools können nichts löschen.

**Test Phase 2:** In der Claude-App eine Idee diktieren, z. B. „Notier mir: Der Kontaktbutton auf der Website soll grün werden.“ Dann „Zeig mir den Eingang“ und „Sortier die Idee ins Projekt Website ein und mach eine Aufgabe draus.“ Zum Schluss „briefing“ aufrufen lassen.

## Phase 1 – Mail und Kalender

**Ergänzung:** `mail_verschieben(uids, ziel)` verschiebt Mails in einen bestehenden Ordner, mit `ordner_anlegen=True` auch in einen neuen (z. B. „Erledigt“), oder per `papierkorb` / `archiv`. Endgültig gelöscht wird nie. Höchstens 50 Mails pro Aufruf, jede Verschiebung steht im Protokoll.

| Tool | Funktion |
|---|---|
| `mails_neueste` | Mails der letzten Tage, optional nur ungelesene |
| `mails_suchen` | Suche nach Text, Absender, Betreff |
| `mail_lesen` | Eine Mail komplett lesen. Sie wird dabei **nicht** als gelesen markiert. |
| `mail_entwurf` | Legt einen Entwurf in GMX ab. **Gesendet wird nie.** |
| `mail_ordner` | Ordner auflisten |
| `kalender_liste` | iCloud-Kalender auflisten |
| `termine` | Termine für einen Zeitraum |
| `termin_eintragen` | Termin in iCloud eintragen |

Der Server antwortet nur unter `/<MCP_SECRET>/mcp`, auf alles andere mit 404. Ohne `MCP_SECRET` (mind. 32 Zeichen) startet er nicht. Das Zugriffs-Log ist abgeschaltet, damit der geheime Pfad nicht in den Logs steht.

---

## 1. Zugänge vorbereiten

**GMX**
- In GMX unter Einstellungen → *POP3 & IMAP* den Zugriff über E-Mail-Programme **erlauben**.
- Bei aktiver Zwei-Faktor-Anmeldung dort ein anwendungsspezifisches Passwort erzeugen. Das normale Passwort funktioniert dann nicht.

**iCloud**
- Auf account.apple.com → *Anmelden und Sicherheit* → *App-spezifische Passwörter* ein neues erzeugen, Name z. B. „Ada“.
- Dazu gehört deine Apple-ID (E-Mail-Adresse). Dein echtes Apple-Passwort brauchst du nicht.

**Domain**
- A-Record für `ada.<deine-domain>` auf die IP des Hetzner-Servers.

## 2. In Coolify anlegen

1. *Projects* → Projekt wählen → *+ New* → *Private Repository (with GitHub App)*. Falls die GitHub-App noch nicht eingerichtet ist, führt Coolify dich durch die Verbindung mit GitHub. Repo `ada` auswählen, Branch `main`.
2. *Build Pack*: **Docker Compose**. *Base Directory* `/`, *Docker Compose Location* `/docker-compose.yml`. Speichern.
3. Beim Dienst **app** unter *Domains* eintragen: `https://ada.<deine-domain>:8000`. Das `:8000` sagt Coolify nur, auf welchen Container-Port es weiterleitet. Von außen bleibt es normales HTTPS auf 443.
4. *Environment Variables*: Coolify zeigt die Variablen aus der Compose-Datei schon an. Werte eintragen:
   - `MCP_SECRET` – im Mac-Terminal `openssl rand -hex 32` ausführen und das Ergebnis einfügen
   - `ALLOWED_HOSTS` – `ada.<deine-domain>`
   - `GMX_USER`, `GMX_PASSWORD`
   - `ICLOUD_USER`, `ICLOUD_APP_PASSWORD`
   - `TZ_NAME` – `Europe/Berlin` (Standard)
5. *Deploy*. In den Logs sollte am Ende `Application startup complete.` stehen.

Die Werte stehen nur in Coolify, nie im Repo und nie im Chat.

## 3. Prüfen

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://ada.<deine-domain>/      # 404
curl -s -o /dev/null -w "%{http_code}\n" https://ada.<deine-domain>/mcp   # 404
```

## 4. In Claude verbinden

Claude → *Einstellungen* → *Connectors* → *Benutzerdefinierten Connector hinzufügen*, Name „Ada“, URL:

```
https://ada.<deine-domain>/<MCP_SECRET>/mcp
```

Dann im Chat fragen: „Was steht heute an?“

## Fehlersuche

- Coolify → Ressource → *Logs*. Die Tools geben Fehler als `{"fehler": "..."}` zurück, Claude zeigt sie dir an.
- `LoginError` / `authentication failed` bei Mail: In Claude „Führe mail_diagnose aus“ sagen. Das Tool zeigt Format und Merkmale von Benutzer und Passwort (nicht die Werte selbst) und das Ergebnis je Anmeldeverfahren. Häufige Ursachen: GMX_USER ist nicht die volle Adresse, IMAP in GMX nicht erlaubt, bei Zwei-Faktor fehlt das anwendungsspezifische Passwort.
- `401 Unauthorized` beim Kalender: Apple-ID oder App-spezifisches Passwort falsch.
- `421 Invalid Host header`: `ALLOWED_HOSTS` passt nicht zur Domain.

## Sicherheit

- Die Connector-URL ist der Schlüssel. Wer sie kennt, kann deine Mails lesen. Behandle sie wie ein Passwort.
- Wenn sie doch nach außen gelangt: in Coolify neues `MCP_SECRET` setzen, neu deployen, Connector in Claude neu anlegen.
- Der Server sendet keine Mails und löscht nichts. Er liest Mails, legt Entwürfe an und trägt Termine ein.
