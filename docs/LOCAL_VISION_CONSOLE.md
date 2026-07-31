# Local Vision Console

Die Local Vision Console ist ein eigenständiges Hilfswerkzeug für lokale,
OpenAI-kompatible KI-Server. Sie läuft separat von AutoPA auf Port `7127`.
Nur die ausdrücklich bestätigte Kamerakalibrierung darf über Moonraker homen
und langsame Messfahrten ausführen; alle Fähigkeitstests bleiben ohne
Druckeraktion.

Während einer Kalibrierung protokolliert Local Vision Homing, jeden Messpunkt,
die Vision-Konfidenz sowie Erfolg oder Abbruch im eigenen Dienstprotokoll. Wenn
Klipper `[respond]` geladen hat, erscheinen dieselben Meldungen zusätzlich in
der Mainsail-Konsole. Ein Fehler bei einer reinen Konsolenmeldung beeinflusst
die Kalibrierungslogik nicht.

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

## Kontrollierter Spaghetti-Test

Die Local-Vision-Kachel bietet einen sicheren Zweibild-Test mit echtem losem
Filament. Zuerst wird bei sauberem Druckbett ein Referenzbild aufgenommen.
Danach legt der Benutzer Filament-Spaghetti von Hand in den sichtbaren Bereich
und startet die zweite Aufnahme. OpenCV bestimmt die geänderten Bildbereiche;
das Vision-Modell bewertet anschließend nur das aktuelle Einzelbild mit diesem
Bereich als Hinweis.

Beide Schritte prüfen Klipper unmittelbar vor der Aufnahme:

- Druckstatus nur `standby`, `complete` oder `cancelled`;
- Klipper `ready` und gemeldete Geschwindigkeit höchstens 0,1 mm/s;
- Düsen- und Bett-Zieltemperatur genau 0 °C;
- Düse und Bett jeweils unter 45 °C;
- keine G-Code-, Homing-, Heiz- oder Bewegungsbefehle aus diesem Ablauf.

Referenz, Prüfbild und Ergebnis liegen ausschließlich im privaten Ordner
`~/.config/local-vision-console/spaghetti-tests/<UTC-Zeit>-<ID>/`. Die
vorbereitete Referenz verfällt nach 20 Minuten. Ein LLM wird erst bei der
zweiten Aufnahme geladen und danach wieder entladen beziehungsweise dem
llama.cpp-Idle-Schlaf überlassen.

## Kamerablick

Der Kamerablick kann manuell als vorne, hinten, links, rechts, eine der vier
diagonalen Ansichten oder Draufsicht gespeichert werden. Alternativ analysiert
das konfigurierte Vision-Modell ein aktuelles Snapshot und schlägt Blickrichtung
und Konfidenz vor. Dieser Vorschlag wird erst nach manueller Bestätigung
gespeichert.

Für eine geometrisch ausgerichtete Überlagerung muss zusätzlich einmalig die
Position bekannter Bettkoordinaten im Kamerabild bestimmt werden. Die folgende
automatische Kalibrierung berechnet daraus eine Homographie. Ohne erfolgreich
gespeicherte Kalibrierung darf das System nur grobe Formabweichungen melden.

## Automatische geometrische Kamerakalibrierung

Die Webkonsole kann die Achsgrenzen live aus Klippers `toolhead`-Objekt lesen.
Nach ausdrücklicher Bestätigung führt der Ablauf ein normales `G28` ohne
Heizen aus. Anschließend wird ein sicherer Z-Abstand angefahren und der
Druckkopf langsam zu vier eingerückten Eckpunkten sowie einer unabhängigen
Kontrollposition bewegt.

Das Vision-Modell lokalisiert die Druckkopf-Verkleidung einmal im
Ausgangsbild. Aus diesem bestätigten Begrenzungsrahmen lernt Local Vision mit
OpenCV selbstständig deren unterscheidbare Farbe. Die eigentliche Messfahrt
verfolgt anschließend den zusammenhängenden Farbbereich deterministisch und
ohne weitere LLM-Schätzungen. So werden sowohl vermischte Mehrbildantworten
als auch halluzinierte Druckköpfe im leeren Kamerabild vermieden.

Vor der Messung prüft Local Vision die vier geplanten Eckpunkte. Ist der
Druckkopf außerhalb des Kamerabilds oder am Rand abgeschnitten, wird der
betroffene Punkt in sicheren Schritten eingerückt. Hintere Punkte werden
gleichzeitig in X zur Bettmitte und in Y zur bereits sichtbaren Vorderkante
gezogen. So entsteht ein kleineres, korrekt geordnetes Trapez statt weiterhin
die volle Bettbreite zu verwenden. Jeder der fünf danach sichtbaren
Bettpunkte wird mit drei
unabhängig aufgenommenen Bildern analysiert. Local Vision bildet den Median
der normierten Rahmenmittelpunkte und verwirft starke Streuung, fehlende
sichtbare Bewegung sowie doppelte Bildpositionen frühzeitig.

Aus den vier bekannten XY-Punkten und den stabilisierten Bildkoordinaten wird
eine Homographie berechnet. Der fünfte Punkt prüft die Projektion; bei zu
großer Abweichung wird nichts gespeichert.

Jeder Lauf erhält unter
`~/.config/local-vision-console/calibration-runs/<UTC-Zeit>-<ID>/` einen
privaten Diagnoseordner. Er enthält das initiale Kamerabild, drei Bilder je
Messpunkt sowie `metadata.json` mit Rohantworten, Koordinaten, Konfidenz,
Streuung, Bewegungsabstand und dem endgültigen Erfolg oder Fehler. Dateien und
Verzeichnisse sind nur für den Benutzer lesbar. In der Mainsail-Konsole
erscheinen dieselben Koordinaten kompakt, damit ein Modell, das stets die
Bildmitte schätzt, sofort erkennbar ist.

Vor dem Homing sendet Local Vision einen kleinen multimodalen Warm-up mit
einem Kalibrier-Timeout von mindestens 180 Sekunden. Erst nach einer
bestätigten Modellantwort beginnt `G28`. Router-fähige llama.cpp-Server werden
am Ende direkt entladen. Ein fest gestarteter Einzelmodell-Server sollte mit
`--sleep-idle-seconds 60` laufen; dann entlädt llama.cpp das Modell nach einer
Minute Leerlauf und lädt es beim nächsten Warm-up automatisch wieder.

Sicherheitsregeln:

- nur bei Klipper-Zustand `ready` und Druckstatus `standby`, `complete` oder
  `cancelled`;
- Achsgrenzen werden unmittelbar vor Homing und Messfahrt erneut geprüft;
- 20 Prozent Sicherheitsrand an X und Y, mindestens 30 mm;
- normales `G28`, keine Heizung und kein Filamentbefehl;
- langsame Messfahrt mit 50 mm/s und sicherem Z-Abstand;
- automatische Einrückung nicht sichtbarer oder abgeschnittener Punkte;
- zusätzliche kleinste Fallback-Stufe bei zehn Prozent des jeweiligen
  Achsabstands;
- drei unabhängige Farbmessungen je Messpunkt mit Median und Streuungsgrenze;
- Vision-Warm-up vor der ersten Druckerbewegung;
- keine Kalibrierung bei unsicherer, unbewegter oder doppelter
  Druckkopferkennung;
- Start ausschließlich unter Aufsicht und nach UI-Bestätigung.
