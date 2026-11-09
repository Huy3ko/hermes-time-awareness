# Roadmap — Ideen, keine Zusagen

Diese Datei hält fest, **was denkbar wäre** und **was dagegen spricht**. Nichts hier ist
implementiert. Die Reihenfolge ist eine Einschätzung, keine Verpflichtung.

Die ausführliche Begründung steht in den Dokumenten des Projektordners
(`ARCHITEKTUR.md`, `FRESHNESS.md`, `ENTSCHEIDUNGSVORLAGE.md`, `SPEZIFIKATION.md`,
`ARCHITEKTUR-FREIGABE.md`).

---

## 1. Freshness Registry — *geprüft, freigegeben, nicht gebaut*

**Idee.** Jede Messung trägt ihr Alter. Bevor ein Wert als aktuell in eine Antwort geht,
steht daneben, wie alt er ist und wie lange er gilt.

**Warum.** Belegter Fehler: Am 09.11.2026 stand eine Plattenzahl von 15:40 um 18:00 im
Bericht, ohne Datum. Die Registry hätte gesagt: *„82 %, gemessen vor 2 h 20 min,
Gültigkeit 1 h — veraltet, neu messen."*

**Offene Fragen.**
- Wer schreibt die Einträge zuverlässig? Eigene Disziplin ist belegt unzuverlässig.
- Ist der Nutzen groß genug für ein Subsystem? Bisher **ein** belegter Fall.

**Empfohlener Zuschnitt, falls gebaut:** drei Subjects (`disk_usage`, `mem_available`,
`gateway_state`), ein stündlicher Probe-Job, kein `post_tool_call` in v1.

---

## 2. Task Timeline — *Idee*

**Idee.** Wann wurde eine Aufgabe begonnen, wann zuletzt bearbeitet, wie lange liegt sie
unbearbeitet.

**Was fehlt.** Eine maschinenlesbare Quelle. Die Projektakten sind Markdown-Tabellen,
von Hand gepflegt; ein fehlender Eintrag ist von „nichts passiert" nicht zu unterscheiden.

**Möglicher erster Schritt.** `journal.jsonl` neben jedem `PROJEKT.md`, aus derselben
Zeile erzeugt. Erst danach ist die Timeline trivial.

**Offene Frage.** `kanban.db` hat die Struktur, aber 0 Tasks. Soll die Quelle dort liegen
oder in den Projektakten?

---

## 3. Event Timeline — *Idee, Daten vorhanden*

**Idee.** „Was geschah vor oder nach dem Deployment?"

**Was da ist.** `cron/executions.db`: 1.000 Läufe mit `started_at`, `finished_at`,
`status`. Exakte Zeitstempel, maschinell.

**Was fehlt.** Die Frage kommt selten — und ist eine SQL-Abfrage mit Zeitfenster.
Ein Plugin, das Logs korreliert, wäre ein eigenes Projekt mit eigener Fehlerfläche.

**Einschätzung:** eher ein Werkzeug als ein Plugin-Baustein.

---

## 4. Temporal Reasoning — *geprüft, umbenannt, nicht gebaut*

**Idee.** Nicht „ein Modell deutet Zeit", sondern **Regeln über Zeitstempeln**:

| Regel | Frage |
|---|---|
| R1 Veraltet | Gilt der Wert noch? |
| R2 Widerspruch | Welche von zwei Zahlen gilt? |
| R3 Zyklus | Läuft es noch? |
| R4 Frist | Ist es überfällig? |

Jede ist eine reine Funktion — Eingabe Zeitstempel, Ausgabe Urteil. Nachprüfbar, ohne Prompt.

**Abhängigkeit.** R1 und R2 brauchen die Freshness Registry. R3 und R4 brauchen nur
Zeitstempel aus Cron und Zertifikaten und könnten früher laufen.

**Gegenprobe, bereits gerechnet:** R3 auf das Backup (letzter Erfolg 07.11., Rhythmus
täglich) meldet `OVERDUE, 1 fehlende Periode` — genau der Befund, der am 09.11. nur
durch Zufall auffiel. R3 auf den Mnemosyne-Nachtlauf schweigt (pünktlich). Die Regel
erkennt den echten Fall und produziert keinen Fehlalarm.

---

## Was ausdrücklich **nicht** geplant ist

- Automatische Task- und Event-Zeilen in jedem Turn — nach zwei Tagen Rauschen.
- Ein globaler Terminal-Mithörer — er kann Antwort nicht von Zwischenausgabe unterscheiden.
- „Reasoning" über Zeitbezüge im Modell — widerspricht der deterministischen Konstruktion.
- Aufgaben-Zeitstempel aus Prosa raten.
