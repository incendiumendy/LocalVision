# Local Vision for Klipper

Local Vision is an independent, local-first vision and analysis companion for
Klipper/Moonraker printers. It is deliberately separate from AutoPA and does
not require load-cell or accelerometer hardware.

## Features

- configure an OpenAI-compatible local LLM server;
- verify text and real image understanding;
- discover Moonraker snapshot cameras;
- store or estimate the camera viewpoint;
- compare a camera snapshot with a rendered view of the current G-code layer;
- explain completed AutoPA result files as an optional read-only integration;
- monitor prints with conservative consecutive-detection gates.

The web console never sends printer commands. The optional monitor keeps
printer actions disabled unless both command-line interlocks are supplied.

## Install on RatOS or generic Klipper

```sh
cd ~/LocalVision
sh scripts/install.sh
printf '%s\n' '<sudo-password>' | sh scripts/install-mainsail-integration.sh
```

Direct URL:

```text
http://<printer-ip>:7127/
```

Mainsail URL:

```text
http://<printer-ip>/local-vision/
```

## Test

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

See [console documentation](docs/LOCAL_VISION_CONSOLE.md) and
[monitor documentation](docs/VISION_MONITOR.md).
