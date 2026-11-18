"""Pre-publication scan: does the staged tree contain anything private?

Run before any push, not just the first one:

    python tools/preflight.py

It reads the live values out of .env and looks for them in exactly the files git
would publish (`git ls-files`), so it checks reality rather than intent. It prints
findings and never prints a secret. Exit status is 1 if anything turns up, so it
can sit in a pre-push hook.

What it looks for:
  * the three live credentials - HenrikDev key, Twitch client id, Twitch secret;
  * the Riot name and tag, which are personal data and belong only in .env;
  * the git email, in case it was pasted into a comment;
  * secret-shaped strings generally, so a credential this script has never heard
    of still trips it.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

CREDENTIALS = ("HENRIK_API_KEY", "TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET")
PERSONAL = ("RIOT_NAME", "RIOT_TAG")

PATTERNS = {
    "HDEV- key": re.compile(r"HDEV-[0-9a-f]{8}-[0-9a-f]{4}-", re.I),
    "30-char token": re.compile(r"(?<![A-Za-z0-9])[a-z0-9]{30}(?![A-Za-z0-9])"),
    "32+ hex blob": re.compile(r"(?<![A-Za-z0-9])[0-9a-f]{32,}(?![A-Za-z0-9])", re.I),
    "bearer/oauth": re.compile(r"(Bearer|oauth:)\s*[A-Za-z0-9._-]{12,}"),
    "assigned secret": re.compile(
        r"(client_secret|api[_-]?key|password)\s*[=:]\s*['\"][A-Za-z0-9_-]{8,}", re.I
    ),
    "email address": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
}


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True
    ).stdout.strip()


def _env() -> dict[str, str]:
    path = REPO_ROOT / ".env"
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            key, _, value = line.partition("=")
            if value.strip():
                out[key.strip()] = value.strip()
    return out


def main() -> int:
    tracked = [p for p in _git("ls-files").splitlines() if p]
    if not tracked:
        print("No files staged or tracked - nothing to check.")
        return 0
    texts = {}
    for name in tracked:
        path = REPO_ROOT / name
        if path.exists():
            texts[name] = path.read_text(encoding="utf-8", errors="replace")

    env = _env()
    print(f"scanning {len(texts)} tracked file(s)\n")
    failures: list[str] = []

    print("credentials:")
    for key in CREDENTIALS:
        value = env.get(key, "")
        if not value:
            print(f"  {key:<22} not set locally - nothing to check")
            continue
        hits = [name for name, text in texts.items() if value in text]
        print(f"  {key:<22} {'ABSENT - good' if not hits else 'LEAKED: ' + ', '.join(hits)}")
        failures += hits

    print("\npersonal identifiers:")
    for key in PERSONAL:
        value = env.get(key, "")
        if not value:
            continue
        word = re.compile(rf"(?<![A-Za-z0-9]){re.escape(value)}(?![A-Za-z0-9])")
        hits = [name for name, text in texts.items() if word.search(text)]
        print(f"  {key:<22} {'absent - good' if not hits else 'FOUND: ' + ', '.join(hits)}")
        failures += hits

    email = _git("config", "user.email")
    if email:
        hits = [name for name, text in texts.items() if email in text]
        print(f"  {'git email':<22} {'absent - good' if not hits else 'FOUND: ' + ', '.join(hits)}")
        failures += hits

    print("\nsecret-shaped strings:")
    for label, pattern in PATTERNS.items():
        found = [
            (name, match.group(0))
            for name, text in texts.items()
            for match in pattern.finditer(text)
        ]
        print(f"  {label:<17} {len(found)} hit(s)")
        for name, raw in found[:8]:
            # An email is shown in full because seeing which one it is, is the point.
            print(f"      {name}: {raw if label == 'email address' else raw[:12] + '...'}")
            if label != "email address":
                failures.append(name)

    print("\n" + "=" * 62)
    if failures:
        print("BLOCKED - review the hits above before pushing")
        return 1
    print("CLEAN - nothing sensitive in the tracked tree")
    return 0


if __name__ == "__main__":
    sys.exit(main())
