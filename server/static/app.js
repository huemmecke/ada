// Ada-Dashboard: kleine Helfer, kein Framework.
document.addEventListener("DOMContentLoaded", () => {
  // Bereiche nachladen (Termine, Mails), damit die Seite sofort da ist
  document.querySelectorAll("[data-laden]").forEach(async (el) => {
    try {
      const r = await fetch(el.dataset.laden, { credentials: "same-origin" });
      el.innerHTML = r.ok ? await r.text() : '<p class="fehler">Konnte nicht geladen werden.</p>';
    } catch (e) {
      el.innerHTML = '<p class="fehler">Keine Verbindung.</p>';
    }
    el.classList.remove("laedt");
  });

  // Sicherheitsabfrage vor dem Löschen
  document.addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-bestaetigen]");
    if (b && !confirm(b.dataset.bestaetigen)) ev.preventDefault();
  });

  // Auswahlfelder, die sofort absenden
  document.addEventListener("change", (ev) => {
    const el = ev.target;
    if (el.matches(".status-wahl, [data-auto-absenden]")) el.form.requestSubmit();
  });

  // Kanban: Drag & Drop
  const board = document.querySelector(".board");
  if (!board) return;
  let gezogen = null;
  board.addEventListener("dragstart", (ev) => {
    gezogen = ev.target.closest(".karte");
    if (!gezogen) return;
    gezogen.classList.add("zieht");
    ev.dataTransfer.effectAllowed = "move";
    ev.dataTransfer.setData("text/plain", gezogen.dataset.id);
  });
  board.addEventListener("dragend", () => {
    if (gezogen) gezogen.classList.remove("zieht");
    board.querySelectorAll(".drueber").forEach((s) => s.classList.remove("drueber"));
  });
  board.querySelectorAll(".spalte").forEach((spalte) => {
    spalte.addEventListener("dragover", (ev) => { ev.preventDefault(); spalte.classList.add("drueber"); });
    spalte.addEventListener("dragleave", (ev) => {
      if (!spalte.contains(ev.relatedTarget)) spalte.classList.remove("drueber");
    });
    spalte.addEventListener("drop", async (ev) => {
      ev.preventDefault();
      spalte.classList.remove("drueber");
      const karte = gezogen;
      if (!karte) return;
      const alt = karte.closest(".spalte");
      if (alt === spalte) return;
      const status = spalte.dataset.status;
      const daten = new FormData();
      daten.append("nur_status", "1");
      daten.append("status", status);
      spalte.querySelector(".spalte-inhalt").prepend(karte);
      zaehlen();
      try {
        const r = await fetch(`/aufgaben/${karte.dataset.id}/aendern`, {
          method: "POST", body: daten, credentials: "same-origin", headers: { "X-Ada-Fetch": "1" },
        });
        if (!r.ok) throw new Error(r.status);
        const wahl = karte.querySelector(".status-wahl");
        if (wahl) wahl.value = status;
      } catch (e) {
        alt.querySelector(".spalte-inhalt").prepend(karte);
        zaehlen();
        alert("Verschieben hat nicht geklappt.");
      }
    });
  });
  function zaehlen() {
    board.querySelectorAll(".spalte").forEach((s) => {
      s.querySelector(".anzahl").textContent = s.querySelectorAll(".karte").length;
    });
  }
});
