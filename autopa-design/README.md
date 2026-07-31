# AutoPA-Dashboard im RatOS-/Mainsail-Look

Diese Dateien bringen das AutoPA-Live-Dashboard auf dieselbe Designsprache
wie LocalVision: RatOS-Primärfarbe, Mainsail-Dark-Surfaces, Roboto und
Vuetify-ähnliche Karten- und Button-Formen.

## Dateien

- `dashboard/app/globals.css` — ersetzt `dashboard/app/globals.css` im
  AutoPA-Projekt vollständig. Palette, Surfaces und Radien sind auf
  RatOS/Mainsail umgestellt; Layout, Selektoren und Klassennamen bleiben
  unverändert, das React-Markup muss nicht angepasst werden.
- `dashboard/public/theme.js` — liest die in Mainsail/RatOS eingestellte
  Primärfarbe live aus der Moonraker-Datenbank
  (`namespace mainsail`, `key uiSettings.primary`) und setzt die
  CSS-Variablen `--primary`, `--primary-rgb` und `--primary-ink`.
  Ohne Moonraker-Antwort bleibt die RatOS-Standardfarbe `#99f321` aktiv.

## Einbindung von theme.js

In `dashboard/app/layout.tsx` im `<body>` vor `{children}` einfügen:

```tsx
<html lang="de">
  <body>
    <script src="/theme.js" />
    {children}
  </body>
</html>
```

Danach das Dashboard wie gewohnt neu bauen bzw. den Dev-Server neu starten.

## Verhalten

- Farbwechsel in Mainsail (**Interface Settings → UI-Settings → Primary
  Color**) wirkt nach dem nächsten Seitenaufruf automatisch in AutoPA und
  LocalVision — beide lesen denselben Moonraker-Datenbankwert.
- Helle Primärfarben bekommen dunklen Schaltflächentext, dunkle weißen
  (Luminanz-Schwelle 0,6, wie in LocalVision).
