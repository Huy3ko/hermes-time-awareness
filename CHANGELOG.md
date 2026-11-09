# Changelog

Alle nennenswerten Änderungen. Format nach [Keep a Changelog](https://keepachangelog.com/de/1.1.0/),
Versionierung nach [SemVer](https://semver.org/lang/de/).

## [2.0.1] — 2026-11-09

### Geändert
- **Aktive Zeit ist als Schätzung gekennzeichnet.** Der Block zeigt `~3 hours active`
  statt `3 hours active`. Die aktive Zeit wird aus den Abständen zwischen
  User-Nachrichten abgeleitet; Arbeit ohne Nachricht (Recherche, lange Tool-Läufe)
  erscheint als Pause. Das README nennt sie jetzt ausdrücklich eine Untergrenze.
- Dokumentiert, dass `LIMIT 400` **nur** die Suche nach dem letzten Austausch
  begrenzt. Session-Start, Turnzahl und aktive Zeit kommen aus einer unbegrenzten
  Aggregat-Abfrage und werden vollständig gemessen.

### Behoben
- Versionsnummer im Manifest stand auf 2.0.0, während der Commit bereits 2.0.1
  auslieferte. Beide stehen jetzt auf 2.0.1.

### Tests
- 51 (ein Test ergänzt: die Markierung muss sichtbar bleiben)

## [2.0.0] — 2026-11-09

### Hinzugefügt
- **Session-Aktivität.** Verstrichene Zeit und aktive Zeit werden getrennt gemessen.
  Abstände über `idle_break_seconds` (Standard 10 min) gelten als Pause, nicht als Arbeit.
- `LIMIT 400` auf der Rückwärtssuche: eine lange Konversation mit vielen Tool-Aufrufen
  kostete 345 ms kalt, 1,4 ms warm.

### Geändert
- **Eigenständige Implementierung.** Die vorherige Fassung enthielt Funktionsrümpfe
  und 23 Testfunktionen aus [time-gap](https://github.com/Randool/time-gap), das ohne
  Lizenz veröffentlicht ist. Eine MIT-Lizenz kann fremden Code nicht mitlizenzieren.
  Diese Fassung teilt das Design, nicht den Code: andere Modulstruktur, andere
  Funktionsgrenzen, Tests aus dem Verhalten statt aus dem Original.

### Entfernt
- `tests/test_time_gap_port.py`, `test_time_awareness.py`, `test_global_time.py`
  (aus der Vorlage abgeleitet) — ersetzt durch `test_clock.py`, `test_history.py`,
  `test_store.py`, `test_hook.py`

### Tests
- 50 (vollständig neu geschrieben)

## [1.1.0] — 2026-11-09

### Hinzugefügt
- Sessionübergreifendes Zeitgefühl über eine eigene Tabelle (eine Zeile pro Session).
- Einmaliger Backfill aus `messages`: 4.190 Sessions in 368 ms über einen Covering Index.
- Modell-Instruktion am Block („never quote or mention").

### Tests
- 41

## [1.0.0] — 2026-11-09

### Hinzugefügt
- Erste Fassung: Uhr, Lücke innerhalb einer Session, ein Block pro Turn.
- Datenbankzugriff `mode=ro` mit WAL-Sicht, `session_id`-indiziert.
- Notification- und Plattform-Filter, Fail-open, Mitternachtswechsel.
