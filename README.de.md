# Local Vision für Klipper

[English](README.md) | [Deutsch](README.de.md)

Local Vision ist ein unabhängiges, lokal betriebenes Vision- und
Analysewerkzeug für Klipper-/Moonraker-Drucker. Es ist bewusst von AutoPA
getrennt und benötigt weder Wägezelle noch Beschleunigungssensor.

![Local-Vision-Dashboard für lokale LLM- und Kameraprüfungen](docs/images/local-vision-dashboard.png)

## Funktionen

- OpenAI-kompatiblen lokalen LLM-Server konfigurieren
- Textverständnis und echte Bildverarbeitung getrennt prüfen
- Moonraker-Snapshot-Kameras automatisch erkennen
- Kamerablick speichern oder durch das Vision-Modell vorschlagen lassen
- Kamerabild mit einer gerenderten Ansicht der aktuellen G-Code-Schicht
  vergleichen
- abgeschlossene AutoPA-Ergebnisse optional und nur lesend erklären lassen
- Drucke mit konservativen Grenzwerten und mehreren aufeinanderfolgenden
  Erkennungen überwachen

Die Webkonsole sendet niemals Druckerbefehle. Beim optionalen Monitor bleiben
Druckeraktionen deaktiviert, solange nicht beide Kommandozeilen-Sperren
ausdrücklich freigegeben wurden.

## Installation unter RatOS oder gewöhnlichem Klipper

```sh
cd ~/LocalVision
sh scripts/install.sh
printf '%s\n' '<sudo-passwort>' | sh scripts/install-mainsail-integration.sh
```

Direkte Adresse:

```text
http://<drucker-ip>:7127/
```

Mainsail-Adresse:

```text
http://<drucker-ip>/local-vision/
```

## Tests

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Weitere Informationen stehen in der
[Konsolendokumentation](docs/LOCAL_VISION_CONSOLE.md) und
[Monitordokumentation](docs/VISION_MONITOR.md).
