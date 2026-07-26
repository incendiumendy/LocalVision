"""Prepare idempotent Local Vision Mainsail navigation and Nginx files."""
import argparse
import json
from pathlib import Path


MARKER_START = "    # LOCAL VISION MANAGED START"
MARKER_END = "    # LOCAL VISION MANAGED END"
MANAGED_BLOCK = (
    MARKER_START + "\n"
    "    include /etc/nginx/snippets/local-vision.conf;\n"
    + MARKER_END + "\n")


def merge_navigation(navigation, entry):
    if not isinstance(navigation, list):
        raise ValueError("Mainsail navigation must be a JSON list")
    if (
            not isinstance(entry, dict)
            or entry.get("title") != "Local Vision"
            or entry.get("href") != "/local-vision/"):
        raise ValueError("Local Vision navigation entry is invalid")
    filtered = [
        item for item in navigation
        if not (
            isinstance(item, dict)
            and (
                item.get("title") == "Local Vision"
                or item.get("href") == "/local-vision/"))
    ]
    filtered.append(entry)
    return sorted(
        filtered,
        key=lambda item: (
            item.get("position", 9999)
            if isinstance(item, dict) else 9999))


def add_nginx_include(content):
    if MARKER_START in content:
        start = content.index(MARKER_START)
        end = content.index(MARKER_END, start) + len(MARKER_END)
        return content[:start] + MANAGED_BLOCK.rstrip("\n") + content[end:]
    closing = content.rfind("\n}")
    if closing < 0 or content.count("server {") != 1:
        raise ValueError("Expected one Nginx server block")
    return content[:closing] + "\n" + MANAGED_BLOCK + content[closing:]


def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    navigation_parser = subparsers.add_parser("navigation")
    navigation_parser.add_argument("source")
    navigation_parser.add_argument("entry")
    navigation_parser.add_argument("output")
    nginx_parser = subparsers.add_parser("nginx")
    nginx_parser.add_argument("source")
    nginx_parser.add_argument("output")
    args = parser.parse_args()

    if args.command == "navigation":
        with open(args.source, "r", encoding="utf-8") as handle:
            navigation = json.load(handle)
        with open(args.entry, "r", encoding="utf-8") as handle:
            entry = json.load(handle)
        result = merge_navigation(navigation, entry)
        with open(args.output, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
    else:
        content = Path(args.source).read_text(encoding="utf-8")
        Path(args.output).write_text(
            add_nginx_include(content), encoding="utf-8")


if __name__ == "__main__":
    main()
