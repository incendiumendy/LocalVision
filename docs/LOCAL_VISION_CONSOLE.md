# Local Vision Console

Die Local Vision Console ist ein eigenständiges Hilfswerkzeug für lokale,
OpenAI-kompatible KI-Server. Sie läuft separat von AutoPA auf Port `7127` und
hat keine Verbindung zu Klipper, Moonraker oder Druckeraktionen.

## Installation

```sh
cd ~/LocalVision
chmod +x scripts/install.sh
./scripts/install.sh
```

Danach ist die Oberfläche unter
`http://<IP-des-Raspberry-Pi>:7127/` erreichbar. Die Verbindungskonfiguration
wird mit Dateimodus `0600` unter
`~/.config/local-vision-console/config.json` gespeichert.

Der Mainsail-Integrationsinstaller fügt zusätzlich einen eigenen
`Local Vision`-Eintrag hinzu und stellt die Console unter
`http://<IP-des-Raspberry-Pi>/local-vision/` bereit. Der Schalter oben links
führt direkt zurück zur Klipper-/Mainsail-Startseite, nicht zu AutoPA.

## Unterstützte Server

Die Console verwendet die OpenAI-kompatiblen Endpunkte:

- `GET /v1/models`
- `POST /v1/chat/completions`

Beispiel für LM Studio:

```text
http://192.168.1.111:1234/v1
```

Aus Sicherheitsgründen akzeptiert das Werkzeug ausschließlich Loopback-,
Link-Local- und private LAN-Adressen. Ein API-Key ist optional und wird nie an
den Browser zurückgegeben.

## Vision-Test

Eine Modellbezeichnung oder Metadaten sind kein verlässlicher Nachweis für
Bildverständnis. Deshalb erzeugt der Server für jeden Test ein neues PNG mit
zwei zufälligen Farbfeldern und sendet es per `image_url` an das ausgewählte
Modell. Nur wenn das Modell beide Farben in der richtigen Reihenfolge erkennt,
wird `Vision bestätigt` angezeigt.

Der Test:

- aktiviert keine Kamera,
- liest keine AutoPA-Sensordaten,
- sendet keine G-Code-, Pause- oder Abbruchbefehle,
- schaltet keine spätere Überwachungsaktion frei.

## Optionale Bewertung nach dem Druck

Die Console kann abgeschlossene Datensätze aus
`~/printer_data/autopa` auflisten. Für eine KI-Bewertung werden ausschließlich
kompakte JSON-Ergebnisse wie `quality.json`, `analysis.json` und
`filament_analysis.json` gelesen. Die umfangreichen Rohdaten werden nicht an
das Modell geschickt.

Die KI soll verständlich zusammenfassen:

- ob die Mess- und Synchronisationsqualität eine Empfehlung überhaupt erlaubt;
- welcher PA-Kandidat aus der deterministischen Analyse hervorgeht;
- ob Temperatur, Extrusion oder Bewegung auffällig waren;
- welcher kurze, beaufsichtigte Folgetest den größten Erkenntnisgewinn bringt.

Diese Funktion ist ein optionaler, nur lesender Konsument von AutoPA-Dateien.
AutoPA selbst hängt nicht von der Console oder einem LLM ab. Die Antwort bleibt
Beratung und enthält ausdrücklich keine automatische Druckeraktion.

## Aktuellen G-Code als Bildreferenz verwenden

Die Console kann den aktuell geladenen Dateinamen, die Schichtnummer und den
Dateifortschritt über Moonraker lesen. Sie lädt die G-Code-Datei ausschließlich
lesend, extrahiert die Extrusionsbahnen und rendert daraus eine Draufsicht:

- frühere Schichten werden grau dargestellt;
- die erwartete aktuelle Schicht wird türkis dargestellt;
- das Kamerabild und diese Referenz werden gemeinsam an das Vision-Modell
  übergeben.

Damit kann das Modell zusätzlich zu Spaghetti unter anderem auf ein abgelöstes
Bauteil, Layer-Shift, fehlende Konturen, starke Überextrusion oder einen Druck
außerhalb der erwarteten Form achten. Der Vergleich bleibt semantisch, solange
keine geometrische Kamerakalibrierung vorliegt.

## Kamerablick

Der Kamerablick kann manuell als vorne, hinten, links, rechts, eine der vier
diagonalen Ansichten oder Draufsicht gespeichert werden. Alternativ analysiert
das konfigurierte Vision-Modell ein aktuelles Snapshot und schlägt Blickrichtung
und Konfidenz vor. Dieser Vorschlag wird erst nach manueller Bestätigung
gespeichert.

Für eine millimetergenaue Überlagerung muss zusätzlich einmalig die Position der
vier Druckbett-Ecken im Kamerabild markiert werden. Daraus kann eine Homographie
berechnet werden. Bis diese Vierpunkt-Kalibrierung umgesetzt und geprüft ist,
darf das System nur grobe Formabweichungen melden und keine Druckeraktion
auslösen.
