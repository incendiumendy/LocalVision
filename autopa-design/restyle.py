"""Restyle the AutoPA dashboard globals.css copy to the RatOS/Mainsail look.

Applies deterministic value substitutions only; layout and selectors stay
untouched so the dashboard markup keeps working unchanged.
"""

from pathlib import Path

path = Path(__file__).parent / "dashboard" / "app" / "globals.css"
css = path.read_text(encoding="utf-8")

replacements = [
    # --- palette: teal -> RatOS lime, purple -> Mainsail blue,
    # --- soft red -> Vuetify error red, warm amber -> Vuetify amber
    ("#58dbc2", "#99f321"),
    ("88, 219, 194", "153, 243, 33"),
    ("#b98cff", "#2196f3"),
    ("185, 140, 255", "33, 150, 243"),
    ("#ff6f79", "#ff5252"),
    ("255, 112, 112", "255, 82, 82"),
    ("#ffbd69", "#ffb300"),
    ("255, 189, 105", "255, 179, 0"),
    # --- surfaces: Mainsail/Vuetify dark
    ("--bg: #0b0d11;", "--bg: #121212;"),
    ("--surface: #12151b;", "--surface: #1e1e1e;"),
    ("--surface-soft: #171b22;", "--surface-soft: #272727;"),
    ("--line: rgba(255, 255, 255, 0.085);",
     "--line: rgba(255, 255, 255, 0.12);"),
    ("--text: #f4f5f7;", "--text: rgba(255, 255, 255, 0.87);"),
    ("--muted: #8d95a3;", "--muted: rgba(255, 255, 255, 0.6);"),
    ("#8d95a3", "rgba(255, 255, 255, 0.6)"),
    ("#737c89", "#9aa0a8"),
    ("#68707c", "rgba(255, 255, 255, 0.38)"),
    ("#626a77", "rgba(255, 255, 255, 0.38)"),
    # --- typography: Mainsail Roboto stack
    (
        "    Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, \"Segoe UI\",\n"
        "    sans-serif;",
        "    Roboto, \"Segoe UI\", ui-sans-serif, system-ui, -apple-system,\n"
        "    BlinkMacSystemFont, sans-serif;",
    ),
    # --- flat body instead of radial gradients
    (
        "  background:\n"
        "    radial-gradient(circle at 8% -20%, rgba(153, 243, 33, 0.1), transparent 34rem),\n"
        "    radial-gradient(circle at 92% 18%, rgba(33, 150, 243, 0.08), transparent 30rem),\n"
        "    var(--bg);",
        "  background: var(--bg);",
    ),
    # --- cards: flat Vuetify surface + elevation, Mainsail radii
    (
        "  background: linear-gradient(150deg, rgba(23, 27, 34, 0.94), rgba(15, 18, 23, 0.96));\n"
        "  box-shadow: 0 20px 70px rgba(0, 0, 0, 0.12);",
        "  background: var(--surface);\n"
        "  box-shadow: 0 3px 1px -2px rgba(0, 0, 0, 0.2), 0 2px 2px 0 rgba(0, 0, 0, 0.14), 0 1px 5px 0 rgba(0, 0, 0, 0.12);",
    ),
    (
        "  background: linear-gradient(150deg, rgba(23, 27, 34, 0.96), rgba(15, 18, 23, 0.98));",
        "  background: var(--surface);",
    ),
    ("  border-radius: 16px;", "  border-radius: 8px;"),
    ("  border-radius: 18px;", "  border-radius: 8px;"),
    # --- metric strip surface
    ("  background: rgba(18, 21, 27, 0.75);", "  background: var(--surface);"),
    # --- inputs: Vuetify filled style
    (
        "  border: 1px solid var(--line);\n"
        "  border-radius: 9px;\n"
        "  background: #0f1217;",
        "  border: 1px solid transparent;\n"
        "  border-bottom-color: rgba(255, 255, 255, 0.24);\n"
        "  border-radius: 4px 4px 0 0;\n"
        "  background: rgba(255, 255, 255, 0.06);",
    ),
    (
        "  border-color: rgba(153, 243, 33, 0.45);\n"
        "  box-shadow: 0 0 0 3px rgba(153, 243, 33, 0.07);",
        "  border-color: var(--green);\n"
        "  border-bottom-width: 2px;\n"
        "  box-shadow: none;",
    ),
    # --- primary action: filled RatOS lime with dark ink
    (
        ".primary-button {\n"
        "  width: 100%;\n"
        "  border: 1px solid rgba(153, 243, 33, 0.32);\n"
        "  border-radius: 10px;\n"
        "  padding: 11px 14px;\n"
        "  background: rgba(153, 243, 33, 0.12);\n"
        "  color: var(--green);",
        ".primary-button {\n"
        "  width: 100%;\n"
        "  border: 0;\n"
        "  border-radius: 4px;\n"
        "  padding: 12px 16px;\n"
        "  background: var(--green);\n"
        "  color: #16210a;",
    ),
    (
        ".primary-button:hover,\n"
        ".primary-button:focus-visible {\n"
        "  background: rgba(153, 243, 33, 0.2);\n"
        "  outline: none;\n"
        "}",
        ".primary-button:hover,\n"
        ".primary-button:focus-visible {\n"
        "  filter: brightness(1.1);\n"
        "  outline: none;\n"
        "}",
    ),
    # --- topbar: flat Mainsail app-bar
    (
        ".topbar {\n"
        "  display: flex;\n"
        "  min-height: 82px;\n"
        "  align-items: center;\n"
        "  justify-content: space-between;\n"
        "  border-bottom: 1px solid var(--line);\n"
        "}",
        ".topbar {\n"
        "  display: flex;\n"
        "  min-height: 64px;\n"
        "  align-items: center;\n"
        "  justify-content: space-between;\n"
        "  border-bottom: 1px solid var(--line);\n"
        "  background: var(--surface);\n"
        "  box-shadow: 0 2px 4px -1px rgba(0, 0, 0, 0.2), 0 4px 5px 0 rgba(0, 0, 0, 0.14), 0 1px 10px 0 rgba(0, 0, 0, 0.12);\n"
        "}",
    ),
    # --- smaller interactive elements: Mainsail 4-6px radii
    ("  border-radius: 10px;", "  border-radius: 4px;"),
    ("  border-radius: 11px;", "  border-radius: 6px;"),
    ("  border-radius: 12px;", "  border-radius: 6px;"),
    ("  border-radius: 9px;", "  border-radius: 6px;"),
    ("  border-radius: 8px;\n  padding: 7px 10px;",
     "  border-radius: 4px;\n  padding: 7px 10px;"),
]

for old, new in replacements:
    count = css.count(old)
    if count == 0:
        print("WARN not found: %r" % old[:70])
    css = css.replace(old, new)

path.write_text(css, encoding="utf-8")
print("done, %d lines" % len(css.splitlines()))
