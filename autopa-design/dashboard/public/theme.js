/*
 * AutoPA × RatOS/Mainsail theme bridge.
 * Reads the live primary color from the Moonraker database
 * (namespace "mainsail", key "uiSettings.primary") and applies it to the
 * CSS variables --primary / --primary-rgb / --primary-ink.
 * Falls back to the RatOS default #99f321 when Moonraker is unreachable.
 */
(function () {
  "use strict";

  function applyThemeColor(color) {
    var match = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(color || "");
    if (!match) return;
    var hex = match[1].toLowerCase();
    if (hex.length === 3) {
      hex = hex
        .split("")
        .map(function (char) {
          return char + char;
        })
        .join("");
    }
    var red = parseInt(hex.slice(0, 2), 16);
    var green = parseInt(hex.slice(2, 4), 16);
    var blue = parseInt(hex.slice(4, 6), 16);
    var root = document.documentElement.style;
    root.setProperty("--primary", "#" + hex);
    root.setProperty("--primary-rgb", red + ", " + green + ", " + blue);
    var luminance = (0.299 * red + 0.587 * green + 0.114 * blue) / 255;
    root.setProperty("--primary-ink", luminance > 0.6 ? "#16210a" : "#ffffff");
  }

  var query = "/server/database/item?namespace=mainsail&key=uiSettings.primary";
  var bases = [window.location.origin];
  if (window.location.port !== "7125") {
    bases.push(window.location.protocol + "//" + window.location.hostname + ":7125");
  }

  (function tryNext(index) {
    if (index >= bases.length) return;
    fetch(bases[index] + query, { credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (payload) {
        var value = payload && payload.result && payload.result.value;
        if (typeof value === "string") applyThemeColor(value);
      })
      .catch(function () {
        tryNext(index + 1);
      });
  })(0);
})();
