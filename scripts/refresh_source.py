#!/usr/bin/env python3
"""Point every tracked app in source.json at its newest published build.

Two kinds of app live here. Some are rebuilt in their own repository, so the
newest release there is what should be listed. The rest are built by hand and
uploaded to this repository under a tagged release, which works the same way as
long as the tag says which app it belongs to.

Version numbers are the awkward part, because tags do not agree on what they
are: `2.6.173`, `v0.8.9`, `readest-0.12.1`, `apps-20260829`. A `meta.env` asset
settles it when the build publishes one; otherwise the tag is stripped of its
prefix and has to still look like a version, or the entry is left alone rather
than listed as "20260829".

Usage:
    python3 scripts/refresh_source.py [--dry-run]
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SOURCE = Path("source.json")

# bundleId -> where its builds are published.
#   repo       required
#   tag_prefix only releases whose tag starts with this; also stripped for the version
#   asset      substring the .ipa name must contain, when a release carries several
TRACK: dict[str, dict[str, str]] = {
    "net.gapul.keepassium": {"repo": "gapul/keepassium-ios"},
    "net.gapul.blink": {"repo": "gapul/blink-ios"},
    "net.gapul.JPPhoneDirectory": {"repo": "gapul/jp-phone-opendata", "tag_prefix": "apps-"},
    "com.kodjodevf.mangayomi": {"repo": "kodjodevf/mangayomi", "asset": "-ios.ipa"},
    # Built by hand and uploaded here; the tag prefix is what identifies them.
    "net.gapul.shirucafe": {"repo": "gapul/altstore-source", "tag_prefix": "shirucafe-"},
    "com.amgiapp.AmgiApp": {"repo": "gapul/altstore-source", "tag_prefix": "amgi-"},
    "com.bilingify.readest": {"repo": "gapul/altstore-source", "tag_prefix": "readest-"},
    "com.audiobookshelf.app": {"repo": "gapul/altstore-source", "tag_prefix": "abs-"},
}

# Old entries stay so AltStore can show what came before, but not for ever.
KEEP_VERSIONS = 5
NOTES_LIMIT = 800
LOOKS_LIKE_A_VERSION = re.compile(r"^\d+(\.\d+)+")


def gh_json(path: str):
    result = subprocess.run(["gh", "api", path], capture_output=True, text=True)
    return json.loads(result.stdout) if result.returncode == 0 else None


def newest_release(config: dict[str, str]):
    prefix = config.get("tag_prefix")
    if not prefix:
        return gh_json(f"repos/{config['repo']}/releases/latest")

    releases = gh_json(f"repos/{config['repo']}/releases?per_page=100") or []
    matching = [
        release for release in releases
        if release.get("tag_name", "").startswith(prefix)
        and not release.get("draft") and not release.get("prerelease")
    ]
    return max(matching, key=lambda r: r.get("published_at") or "", default=None)


def read_meta(release) -> dict[str, str]:
    asset = next((a for a in release["assets"] if a["name"] == "meta.env"), None)
    if not asset:
        return {}
    try:
        with urllib.request.urlopen(asset["browser_download_url"], timeout=60) as response:
            body = response.read().decode()
    except Exception as error:  # noqa: BLE001 - an unreadable asset is not fatal
        print(f"    meta.env unreadable ({error})")
        return {}
    return dict(
        line.split("=", 1) for line in body.splitlines() if "=" in line
    )


def pick_ipa(release, wanted: str | None):
    candidates = [a for a in release["assets"] if a["name"].lower().endswith(".ipa")]
    if wanted:
        candidates = [a for a in candidates if wanted in a["name"]]
    return candidates[0] if candidates else None


def notes_for(release, fallback: str) -> str:
    """AltStore shows this as the release notes for that one version.

    Carrying the previous version's text forward would keep saying things like
    "0.3.0 cannot fetch the catalogue" long after 0.3.0 stopped being the
    version anyone runs, so the release body is what belongs here.
    """
    body = (release.get("body") or "").strip()
    if not body:
        return fallback
    if len(body) > NOTES_LIMIT:
        body = body[:NOTES_LIMIT].rstrip() + "…"
    return body


def version_for(release, config: dict[str, str], meta: dict[str, str]) -> str | None:
    if meta.get("VERSION"):
        return meta["VERSION"].strip()
    tag = release["tag_name"]
    prefix = config.get("tag_prefix")
    candidate = tag[len(prefix):] if prefix and tag.startswith(prefix) else tag.lstrip("v")
    # A date tag survives the strip as "20260829"; listing that as a version
    # would offer every user a downgrade-shaped update for ever.
    return candidate if LOOKS_LIKE_A_VERSION.match(candidate) else None


def main(dry_run: bool) -> int:
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    changed = False

    for app in source.get("apps", []):
        bundle = app.get("bundleIdentifier")
        config = TRACK.get(bundle)
        if not config:
            continue
        print(f"{app['name']} ({bundle})")

        release = newest_release(config)
        if not release or "assets" not in release:
            print("    no release found — left alone")
            continue

        ipa = pick_ipa(release, config.get("asset"))
        if not ipa:
            print(f"    {release['tag_name']} has no matching .ipa — left alone")
            continue

        meta = read_meta(release)
        version = version_for(release, config, meta)
        if version is None:
            print(f"    cannot tell a version from {release['tag_name']} "
                  "and there is no meta.env — left alone")
            continue

        current = (app.get("versions") or [{}])[0]
        if (current.get("version") == version
                and current.get("downloadURL") == ipa["browser_download_url"]
                and current.get("size") == ipa["size"]):
            print(f"    {version} — already listed")
            continue

        entry = {
            "version": version,
            "buildVersion": meta.get("BUILD", "").strip() or version,
            "date": (release.get("published_at") or "")[:10]
                    or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "localizedDescription": notes_for(release, current.get("localizedDescription", "")),
            "downloadURL": ipa["browser_download_url"],
            "size": ipa["size"],
            "minOSVersion": meta.get("MINOS", "").strip()
                            or current.get("minOSVersion", "17.0"),
        }
        history = [v for v in app.get("versions", []) if v.get("version") != version]
        app["versions"] = [entry] + history[:KEEP_VERSIONS - 1]
        changed = True
        print(f"    {current.get('version', '—')} -> {version} ({ipa['size']} bytes)")

    if not changed:
        print("\nnothing to do")
        return 0
    if dry_run:
        print("\nwould update source.json")
        return 0

    SOURCE.write_text(json.dumps(source, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("\nsource.json updated")
    return 0


if __name__ == "__main__":
    sys.exit(main("--dry-run" in sys.argv))
