# Changelog / Änderungsprotokoll

## v0.1.0-alpha.1 - 2026-07-26

### Deutsch

Erste öffentliche Alpha-Version von LocalVision als eigenständige,
lokale KI- und Bildanalyse-Erweiterung für Klipper/Moonraker.

#### Enthalten

- Konfiguration eines lokalen OpenAI-kompatiblen KI-Servers;
- getrennte Prüfungen für Textfähigkeit und echte Bildverarbeitung;
- Erkennung der in Moonraker eingerichteten Kameras;
- gespeicherter oder automatisch vorgeschlagener Kamerawinkel;
- Vergleich eines Kamera-Snapshots mit einer gerenderten Ansicht des aktuell
  gedruckten G-Code-Layers;
- optionale, rein lesende Auswertung abgeschlossener AutoPA-Ergebnisse;
- konservative Drucküberwachung mit mehreren aufeinanderfolgenden
  Erkennungen;
- getrennt konfigurierbare Aktionen zum Warnen, Pausieren oder Abbrechen;
- eigenständige Weboberfläche und Mainsail-Integration;
- Schutz gegen öffentliche KI-Endpunkte und gegen die Ausgabe gespeicherter
  API-Schlüssel.

#### Bekannte Grenzen

- Vision-Ergebnisse sind probabilistisch und können falsch positiv oder
  falsch negativ sein.
- Die Webkonsole sendet keine Druckerbefehle. Aktionen des optionalen Monitors
  bleiben ohne beide ausdrücklichen Kommandozeilen-Freigaben deaktiviert.
- Vor Pausen- oder Abbruchaktionen müssen Kamera, Modell, Schwellwerte und
  zeitliche Regeln am eigenen Drucker im Warnmodus validiert werden.
- Es werden ausschließlich lokale oder private KI-Endpunkte akzeptiert.

### English

First public alpha release of LocalVision as an independent, local-first AI
and image-analysis companion for Klipper/Moonraker.

#### Included

- configuration of a local OpenAI-compatible AI server;
- separate tests for text capability and genuine image understanding;
- discovery of cameras configured in Moonraker;
- stored or automatically suggested camera viewpoint;
- comparison of a camera snapshot with a rendered view of the currently
  printed G-code layer;
- optional read-only analysis of completed AutoPA results;
- conservative print monitoring with consecutive-detection gates;
- separately configurable warning, pause and cancel policies;
- a standalone web console and Mainsail integration;
- protection against public AI endpoints and exposure of stored API keys.

#### Known limitations

- Vision results are probabilistic and may be false positive or false
  negative.
- The web console sends no printer commands. Optional monitor actions remain
  disabled unless both explicit command-line interlocks are supplied.
- Validate the camera, model, thresholds and timing rules on the target
  printer in warning mode before enabling pause or cancel actions.
- Only local or private AI endpoints are accepted.
