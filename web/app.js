const $ = (selector) => document.querySelector(selector);

const elements = {
  baseUrl: $("#base-url"),
  model: $("#model"),
  models: $("#models"),
  apiKey: $("#api-key"),
  timeout: $("#timeout"),
  clearKey: $("#clear-key"),
  keyState: $("#key-state"),
  serviceStatus: $("#service-status"),
  configResult: $("#config-result"),
  testResult: $("#test-result"),
  save: $("#save"),
  loadModels: $("#load-models"),
  testText: $("#test-text"),
  testVision: $("#test-vision"),
  dataset: $("#dataset"),
  loadDatasets: $("#load-datasets"),
  analyze: $("#analyze"),
  analysisResult: $("#analysis-result"),
  moonrakerUrl: $("#moonraker-url"),
  camera: $("#camera"),
  cameraView: $("#camera-view"),
  loadCameras: $("#load-cameras"),
  detectView: $("#detect-view"),
  compareGcode: $("#compare-gcode"),
  referenceResult: $("#reference-result"),
  homeAssistantWebhook: $("#home-assistant-webhook"),
  homeAssistantEnabled: $("#home-assistant-enabled"),
  homeAssistantCooldown: $("#home-assistant-cooldown"),
  clearHomeAssistantWebhook: $("#clear-home-assistant-webhook"),
  homeAssistantState: $("#home-assistant-state"),
  homeAssistantResult: $("#home-assistant-result"),
  testHomeAssistant: $("#test-home-assistant"),
  cameraCalibrationState: $("#camera-calibration-state"),
  calibrationMotionConfirm: $("#calibration-motion-confirm"),
  autoCalibrateCamera: $("#auto-calibrate-camera"),
  cameraCalibrationResult: $("#camera-calibration-result"),
  spaghettiState: $("#spaghetti-state"),
  spaghettiColdConfirm: $("#spaghetti-cold-confirm"),
  spaghettiPrepare: $("#spaghetti-prepare"),
  spaghettiAnalyze: $("#spaghetti-analyze"),
  spaghettiCancel: $("#spaghetti-cancel"),
  spaghettiResult: $("#spaghetti-result"),
};

function klipperUrl() {
  const url = new URL(window.location.href);
  if (url.port === "7127") url.port = "";
  url.pathname = "/";
  url.search = "";
  url.hash = "";
  return url.toString();
}

function apiUrl(path) {
  const prefix = window.location.pathname.startsWith("/local-vision/")
    ? "/local-vision"
    : "";
  return `${prefix}${path}`;
}

async function request(path, options = {}) {
  const response = await fetch(apiUrl(path), {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });
  const payload = await response.json().catch(() => ({
    ok: false,
    error: `Ungültige Serverantwort (HTTP ${response.status})`,
  }));
  if (!response.ok || payload.error) {
    throw new Error(payload.error || `HTTP ${response.status}`);
  }
  return payload;
}

function setBusy(button, busy, label) {
  if (busy) {
    button.dataset.label = button.textContent;
    button.textContent = label;
  } else if (button.dataset.label) {
    button.textContent = button.dataset.label;
  }
  button.disabled = busy;
}

function showResult(ok, title, text) {
  elements.testResult.className = `result ${ok ? "ok" : "error"}`;
  elements.testResult.innerHTML = "";
  const strong = document.createElement("strong");
  const paragraph = document.createElement("p");
  strong.textContent = title;
  paragraph.textContent = text;
  elements.testResult.append(strong, paragraph);
}

async function loadConfig() {
  try {
    const config = await request("/api/config");
    elements.baseUrl.value = config.baseUrl;
    elements.model.value = config.model;
    elements.timeout.value = config.timeoutSeconds;
    elements.moonrakerUrl.value = config.moonrakerUrl;
    elements.camera.dataset.selected = config.cameraUid || "";
    elements.cameraView.value = config.cameraView || "unknown";
    elements.homeAssistantEnabled.checked =
      config.homeAssistantEnabled === true;
    elements.homeAssistantCooldown.value =
      config.homeAssistantCooldownMinutes || 15;
    elements.homeAssistantState.textContent =
      config.homeAssistantWebhookConfigured
        ? (config.homeAssistantEnabled ? "Alarm aktiv" : "Webhook gespeichert")
        : "Nicht eingerichtet";
    elements.cameraCalibrationState.textContent =
      config.cameraCalibrationConfigured
        ? "Geometrisch kalibriert"
        : "Nicht kalibriert";
    elements.keyState.textContent = config.apiKeyConfigured
      ? "API-Key gespeichert"
      : "Kein API-Key";
    elements.serviceStatus.textContent = "Dienst bereit";
    elements.serviceStatus.className = "status ok";
    return true;
  } catch (error) {
    elements.serviceStatus.textContent = "Dienstfehler";
    elements.serviceStatus.className = "status error";
    elements.configResult.textContent = error.message;
    return false;
  }
}

async function saveConfig(quiet = false) {
  setBusy(elements.save, true, "Speichere …");
  elements.configResult.textContent = "";
  try {
    const config = await request("/api/config", {
      method: "POST",
      body: JSON.stringify({
        baseUrl: elements.baseUrl.value,
        model: elements.model.value,
        apiKey: elements.apiKey.value,
        clearApiKey: elements.clearKey.checked,
        timeoutSeconds: Number(elements.timeout.value),
        moonrakerUrl: elements.moonrakerUrl.value,
        cameraUid: elements.camera.value,
        cameraView: elements.cameraView.value,
        homeAssistantWebhookUrl: elements.homeAssistantWebhook.value,
        homeAssistantEnabled: elements.homeAssistantEnabled.checked,
        homeAssistantCooldownMinutes:
          Number(elements.homeAssistantCooldown.value),
        clearHomeAssistantWebhook:
          elements.clearHomeAssistantWebhook.checked,
      }),
    });
    elements.apiKey.value = "";
    elements.clearKey.checked = false;
    elements.homeAssistantWebhook.value = "";
    elements.clearHomeAssistantWebhook.checked = false;
    elements.keyState.textContent = config.apiKeyConfigured
      ? "API-Key gespeichert"
      : "Kein API-Key";
    elements.homeAssistantState.textContent =
      config.homeAssistantWebhookConfigured
        ? (config.homeAssistantEnabled ? "Alarm aktiv" : "Webhook gespeichert")
        : "Nicht eingerichtet";
    if (!quiet) {
      elements.configResult.textContent = "Konfiguration sicher gespeichert.";
    }
    return true;
  } catch (error) {
    elements.configResult.textContent = error.message;
    return false;
  } finally {
    setBusy(elements.save, false);
  }
}

async function testHomeAssistant() {
  setBusy(elements.testHomeAssistant, true, "Sende …");
  elements.homeAssistantResult.textContent = "";
  try {
    if (!await saveConfig(true)) return;
    const result = await request("/api/home-assistant/test", {
      method: "POST",
      body: "{}",
    });
    elements.homeAssistantResult.textContent = result.delivered
      ? "Testalarm an Home Assistant zugestellt."
      : "Home Assistant hat den Testalarm nicht bestätigt.";
  } catch (error) {
    elements.homeAssistantResult.textContent = error.message;
  } finally {
    setBusy(elements.testHomeAssistant, false);
  }
}

async function autoCalibrateCamera() {
  if (!elements.calibrationMotionConfirm.checked) {
    elements.cameraCalibrationResult.textContent =
      "Bitte zuerst Homing und Druckkopfbewegungen ausdrücklich bestätigen.";
    return;
  }
  setBusy(elements.autoCalibrateCamera, true, "Prüfe Grenzen …");
  elements.cameraCalibrationResult.textContent =
    "Klipper-Zustand und Achsgrenzen werden gelesen …";
  try {
    if (!await saveConfig(true)) return;
    const preview = await request("/api/camera/calibration/plan");
    const plan = preview.plan;
    const points = plan.points
      .map((point) => `${point.name}: X${point.x} Y${point.y}`)
      .join("\n");
    const confirmed = window.confirm(
      `Auto-Kalibrierung startet jetzt normales G28 ohne Heizen.\n\n`
      + `Bewegungsraum: ${plan.bedWidth} × ${plan.bedDepth} mm\n`
      + `Sicherer Z-Abstand: ${plan.safeZ} mm\n`
      + `Messpunkte:\n${points}\n\n`
      + "Der Drucker muss leer und beaufsichtigt sein. Jetzt starten?",
    );
    if (!confirmed) {
      elements.cameraCalibrationResult.textContent =
        "Auto-Kalibrierung wurde vor jeder Bewegung abgebrochen.";
      return;
    }
    setBusy(elements.autoCalibrateCamera, true, "Home & kalibriere …");
    const prepared = await request("/api/camera/calibration/prepare", {
      method: "POST",
      body: JSON.stringify({ motionConfirmation: "HOME_AND_MOVE" }),
    });
    elements.cameraCalibrationResult.textContent = prepared.consoleMessages
      ? "Kalibrierung läuft. Jeder Schritt erscheint auch in der Mainsail-Konsole …"
      : "Kalibrierung läuft. Jeder Schritt erscheint im Local-Vision-Dienstprotokoll …";
    const result = await request("/api/camera/calibration/run", {
      method: "POST",
      body: JSON.stringify({
        sessionToken: prepared.sessionToken,
        motionConfirmation: "HOME_AND_MOVE",
      }),
    });
    elements.cameraCalibrationState.textContent = "Geometrisch kalibriert";
    elements.cameraCalibrationResult.textContent =
      `Kalibrierung gespeichert. Kontrollabweichung: `
      + `${(result.reprojectionError * 100).toFixed(1)} % der Bilddiagonale, `
      + `minimale Erkennungskonfidenz: `
      + `${Math.round(result.minimumConfidence * 100)} %.`;
    elements.calibrationMotionConfirm.checked = false;
  } catch (error) {
    elements.cameraCalibrationResult.textContent = error.message;
  } finally {
    setBusy(elements.autoCalibrateCamera, false);
  }
}

function setSpaghettiUi(phase) {
  const waiting = phase === "awaiting_spaghetti";
  elements.spaghettiPrepare.hidden = waiting;
  elements.spaghettiColdConfirm.closest("label").hidden = waiting;
  elements.spaghettiAnalyze.hidden = !waiting;
  elements.spaghettiCancel.hidden = !waiting;
  elements.spaghettiState.textContent = waiting
    ? "Referenz wartet auf Filament"
    : "Bereit";
  if (!waiting) {
    elements.spaghettiColdConfirm.checked = false;
  }
}

let spaghettiSessionToken = null;

async function refreshSpaghettiStatus() {
  try {
    const status = await request("/api/spaghetti/status");
    if (status.state === "awaiting_spaghetti") {
      spaghettiSessionToken = status.sessionToken;
      setSpaghettiUi("awaiting_spaghetti");
      elements.spaghettiResult.textContent =
        "Referenz gespeichert. Filament-Spaghetti auf das kalte Bett legen "
        + "und danach die Prüfung starten.";
    } else {
      spaghettiSessionToken = null;
      setSpaghettiUi("idle");
    }
  } catch (error) {
    elements.spaghettiResult.textContent = error.message;
  }
}

async function prepareSpaghettiTest() {
  if (!elements.spaghettiColdConfirm.checked) {
    elements.spaghettiResult.textContent =
      "Bitte zuerst ausdrücklich bestätigen, dass der Drucker kalt, "
      + "stillstehend und das Bett leer ist.";
    return;
  }
  setBusy(elements.spaghettiPrepare, true, "Prüfe Drucker …");
  elements.spaghettiResult.textContent =
    "Temperatur, Bewegung und Druckstatus werden live geprüft …";
  try {
    if (!await saveConfig(true)) return;
    const confirmed = window.confirm(
      "Der Spaghetti-Test nimmt jetzt ein Referenzbild des leeren, kalten "
      + "Druckbetts auf.\n\n"
      + "Es werden keine Heiz-, Homing- oder Bewegungsbefehle gesendet. "
      + "Jetzt starten?",
    );
    if (!confirmed) {
      elements.spaghettiResult.textContent =
        "Spaghetti-Test wurde vor der Referenzaufnahme abgebrochen.";
      return;
    }
    const prepared = await request("/api/spaghetti/prepare", {
      method: "POST",
      body: JSON.stringify({ confirmation: "COLD_IDLE_REFERENCE" }),
    });
    spaghettiSessionToken = prepared.sessionToken;
    setSpaghettiUi("awaiting_spaghetti");
    elements.spaghettiResult.textContent = prepared.message;
  } catch (error) {
    elements.spaghettiResult.textContent = error.message;
  } finally {
    setBusy(elements.spaghettiPrepare, false);
  }
}

async function analyzeSpaghettiTest() {
  if (!spaghettiSessionToken) {
    elements.spaghettiResult.textContent =
      "Keine vorbereitete Referenz gefunden. Bitte zuerst eine Referenz "
      + "aufnehmen.";
    return;
  }
  setBusy(elements.spaghettiAnalyze, true, "Analysiere …");
  elements.spaghettiResult.textContent =
    "Kälte und Stillstand werden erneut geprüft, danach bewertet das "
    + "Vision-Modell das aktuelle Bild …";
  try {
    const result = await request("/api/spaghetti/analyze", {
      method: "POST",
      body: JSON.stringify({ sessionToken: spaghettiSessionToken }),
    });
    spaghettiSessionToken = null;
    setSpaghettiUi("idle");
    elements.spaghettiResult.textContent = result.spaghettiDetected
      ? `Spaghetti erkannt (${Math.round(result.confidence * 100)} %). `
        + result.description
      : `Kein Spaghetti erkannt (${Math.round(result.confidence * 100)} %). `
        + result.description;
  } catch (error) {
    elements.spaghettiResult.textContent = error.message;
  } finally {
    setBusy(elements.spaghettiAnalyze, false);
  }
}

async function cancelSpaghettiTest() {
  if (!spaghettiSessionToken) return;
  setBusy(elements.spaghettiCancel, true, "Breche ab …");
  try {
    await request("/api/spaghetti/cancel", {
      method: "POST",
      body: JSON.stringify({ sessionToken: spaghettiSessionToken }),
    });
    spaghettiSessionToken = null;
    setSpaghettiUi("idle");
    elements.spaghettiResult.textContent = "Spaghetti-Test abgebrochen.";
  } catch (error) {
    elements.spaghettiResult.textContent = error.message;
  } finally {
    setBusy(elements.spaghettiCancel, false);
  }
}

function setReferenceResult(title, text) {
  elements.referenceResult.innerHTML = "";
  const strong = document.createElement("strong");
  const paragraph = document.createElement("p");
  strong.textContent = title;
  paragraph.textContent = text;
  elements.referenceResult.append(strong, paragraph);
}

async function loadCameras(saveFirst = false) {
  setBusy(elements.loadCameras, true, "Lade …");
  try {
    if (saveFirst && !await saveConfig(true)) return;
    const payload = await request("/api/cameras");
    const selected = elements.camera.value
      || elements.camera.dataset.selected
      || "";
    elements.camera.innerHTML = "";
    if (!payload.cameras.length) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "Keine Snapshot-Kamera gefunden";
      elements.camera.append(option);
      return;
    }
    const activeCamera = payload.cameras.find((camera) => camera.enabled);
    const automatic = document.createElement("option");
    automatic.value = "";
    automatic.textContent = activeCamera
      ? `Automatisch · ${activeCamera.name} (aktiv)`
      : "Automatisch · keine aktive Kamera";
    elements.camera.append(automatic);
    for (const camera of payload.cameras) {
      const option = document.createElement("option");
      option.value = camera.uid;
      option.textContent =
        `${camera.name}${camera.enabled ? "" : " · deaktiviert"}`;
      elements.camera.append(option);
    }
    if ([...elements.camera.options].some((option) => option.value === selected)) {
      elements.camera.value = selected;
    }
    if (saveFirst) {
      elements.configResult.textContent =
        `${payload.cameras.length} Kamera(s) gefunden. Auswahl bitte speichern.`;
    }
  } catch (error) {
    setReferenceResult("Kameras nicht verfügbar", error.message);
  } finally {
    setBusy(elements.loadCameras, false);
  }
}

async function detectView() {
  setBusy(elements.detectView, true, "Analysiere …");
  try {
    if (!await saveConfig(true)) return;
    const result = await request("/api/camera/detect-view", {
      method: "POST",
      body: "{}",
    });
    elements.cameraView.value = result.suggestedView;
    setReferenceResult(
      `Vorschlag: ${result.suggestedView} · ${Math.round(result.confidence * 100)}%`,
      `${result.note} Sichtbare Bett-Ecken: ${result.bedCornersVisible}/4. Bitte prüfen und anschließend Konfiguration speichern.`,
    );
  } catch (error) {
    setReferenceResult("Winkelerkennung fehlgeschlagen", error.message);
  } finally {
    setBusy(elements.detectView, false);
  }
}

async function compareGcode() {
  setBusy(elements.compareGcode, true, "Vergleiche …");
  try {
    if (!await saveConfig(true)) return;
    setReferenceResult(
      "Soll-/Ist-Vergleich läuft",
      "Kamerabild und erwartete G-Code-Schicht werden vorbereitet …",
    );
    const result = await request("/api/reference/compare", {
      method: "POST",
      body: "{}",
    });
    setReferenceResult(
      `${result.filename} · Schicht ${result.currentLayer}`,
      `${result.answer} · ${result.latencyMs} ms`,
    );
  } catch (error) {
    setReferenceResult("Vergleich nicht möglich", error.message);
  } finally {
    setBusy(elements.compareGcode, false);
  }
}

async function loadModels() {
  setBusy(elements.loadModels, true, "Lade …");
  elements.configResult.textContent =
    "Die gespeicherte Verbindung wird abgefragt …";
  try {
    const payload = await request("/api/models");
    elements.models.innerHTML = "";
    for (const model of payload.models) {
      const option = document.createElement("option");
      option.value = model.id;
      option.label = model.metadataSuggestsVision
        ? `${model.id} · Metadaten: Vision`
        : model.id;
      elements.models.append(option);
    }
    elements.configResult.textContent =
      `${payload.models.length} Modell(e) gefunden.`;
  } catch (error) {
    elements.configResult.textContent = error.message;
  } finally {
    setBusy(elements.loadModels, false);
  }
}

async function runTest(kind) {
  const button = kind === "vision" ? elements.testVision : elements.testText;
  setBusy(button, true, "Prüfe …");
  elements.testResult.className = "result neutral";
  elements.testResult.innerHTML =
    "<strong>Test läuft</strong><p>Das lokale Modell antwortet …</p>";
  try {
    const result = await request(`/api/test/${kind}`, {
      method: "POST",
      body: "{}",
    });
    if (kind === "vision") {
      showResult(
        result.ok,
        result.ok ? "Vision bestätigt" : "Vision nicht bestätigt",
        `${result.explanation} Antwort: ${result.answer} · ${result.latencyMs} ms`,
      );
    } else {
      showResult(
        result.ok,
        result.ok ? "Text-Verbindung funktioniert" : "Texttest fehlgeschlagen",
        `Antwort: ${result.answer} · ${result.latencyMs} ms`,
      );
    }
  } catch (error) {
    showResult(false, "Test fehlgeschlagen", error.message);
  } finally {
    setBusy(button, false);
  }
}

async function loadDatasets() {
  setBusy(elements.loadDatasets, true, "Lade …");
  try {
    const payload = await request("/api/datasets");
    elements.dataset.innerHTML = "";
    if (!payload.datasets.length) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "Noch keine Datensätze gefunden";
      elements.dataset.append(option);
      return;
    }
    for (const dataset of payload.datasets) {
      const option = document.createElement("option");
      option.value = dataset.id;
      option.textContent = dataset.readyForLlm
        ? dataset.id
        : `${dataset.id} · Analyse fehlt`;
      option.disabled = !dataset.readyForLlm;
      elements.dataset.append(option);
    }
  } catch (error) {
    elements.analysisResult.innerHTML = "";
    const strong = document.createElement("strong");
    const paragraph = document.createElement("p");
    strong.textContent = "Datensätze nicht verfügbar";
    paragraph.textContent = error.message;
    elements.analysisResult.append(strong, paragraph);
  } finally {
    setBusy(elements.loadDatasets, false);
  }
}

async function analyzeDataset() {
  if (!elements.dataset.value) {
    elements.analysisResult.innerHTML =
      "<strong>Kein Datensatz ausgewählt</strong><p>Bitte zuerst einen abgeschlossenen Datensatz auswählen.</p>";
    return;
  }
  setBusy(elements.analyze, true, "Analysiere …");
  elements.analysisResult.innerHTML =
    "<strong>Lokale KI wertet aus</strong><p>Die kompakten Ergebnisdateien werden gelesen …</p>";
  try {
    const result = await request("/api/analyze", {
      method: "POST",
      body: JSON.stringify({ dataset: elements.dataset.value }),
    });
    elements.analysisResult.innerHTML = "";
    const strong = document.createElement("strong");
    const paragraph = document.createElement("p");
    strong.textContent =
      `KI-Bewertung · ${result.dataset} · ${result.latencyMs} ms`;
    paragraph.textContent = result.answer;
    elements.analysisResult.append(strong, paragraph);
  } catch (error) {
    elements.analysisResult.innerHTML = "";
    const strong = document.createElement("strong");
    const paragraph = document.createElement("p");
    strong.textContent = "Bewertung fehlgeschlagen";
    paragraph.textContent = error.message;
    elements.analysisResult.append(strong, paragraph);
  } finally {
    setBusy(elements.analyze, false);
  }
}

$("#back").addEventListener("click", () => {
  window.location.assign(klipperUrl());
});
elements.save.addEventListener("click", () => saveConfig(false));
elements.loadModels.addEventListener("click", loadModels);
elements.testText.addEventListener("click", () => runTest("text"));
elements.testVision.addEventListener("click", () => runTest("vision"));
elements.loadDatasets.addEventListener("click", loadDatasets);
elements.analyze.addEventListener("click", analyzeDataset);
elements.loadCameras.addEventListener("click", () => loadCameras(true));
elements.detectView.addEventListener("click", detectView);
elements.compareGcode.addEventListener("click", compareGcode);
elements.testHomeAssistant.addEventListener("click", testHomeAssistant);
elements.autoCalibrateCamera.addEventListener("click", autoCalibrateCamera);
elements.spaghettiPrepare.addEventListener("click", prepareSpaghettiTest);
elements.spaghettiAnalyze.addEventListener("click", analyzeSpaghettiTest);
elements.spaghettiCancel.addEventListener("click", cancelSpaghettiTest);

function applyThemeColor(color) {
  const match = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(color || "");
  if (!match) return;
  let hex = match[1].toLowerCase();
  if (hex.length === 3) {
    hex = hex.split("").map((char) => char + char).join("");
  }
  const red = parseInt(hex.slice(0, 2), 16);
  const green = parseInt(hex.slice(2, 4), 16);
  const blue = parseInt(hex.slice(4, 6), 16);
  const root = document.documentElement.style;
  root.setProperty("--primary", `#${hex}`);
  root.setProperty("--primary-rgb", `${red}, ${green}, ${blue}`);
  const luminance = (0.299 * red + 0.587 * green + 0.114 * blue) / 255;
  root.setProperty("--primary-ink", luminance > 0.6 ? "#16210a" : "#ffffff");
}

async function loadTheme() {
  try {
    const theme = await request("/api/theme");
    if (theme.primary) applyThemeColor(theme.primary);
  } catch (error) {
    // Ohne Moonraker-Antwort bleibt die RatOS-Standardfarbe #99f321 aktiv.
  }
}

async function initialize() {
  if (await loadConfig()) {
    await Promise.all([loadDatasets(), loadCameras(false), loadTheme()]);
  } else {
    await loadTheme();
  }
  await refreshSpaghettiStatus();
}

initialize();
