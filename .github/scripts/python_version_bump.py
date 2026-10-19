"""Checks for a newer STABLE Python patch or next minor series and, if one exists, bumps the existing
workflow files (+ ruff.toml, when Ruff already understands that target) in place.

Used by .github/workflows/python_version_bump.yml. Dependabot cannot do this job: the
Python interpreter version is a `python-version:` string inside workflow YAML, not a
tracked dependency file (see docs/DEPENDABOT.md and docs/PYTHON_VERSION.md).

Source of truth: actions/python-versions' versions-manifest.json — the EXACT list
actions/setup-python itself downloads from. Anything this script proposes is therefore
already installable on the runner the moment the PR opens; nothing is guessed.

Deliberately narrow: the ONLY files it may ever write inside the repository are the allowlisted
workflow YAMLs and ruff.toml (see ALLOWED_WRITES). It never creates, rewrites or even reads
a README or anything under docs/ — a production repo that carries nothing but monitor.yml
must be able to run this unchanged, and it does: every target is skipped when absent.

Writes GitHub Actions outputs (changed, old_version, new_version, ruff_bumped,
changed_files) and a
PR body to $PR_BODY_PATH. Never raises on a network hiccup: it just skips this week's
check (changed=false) so a flaky connection can't turn into a failed scheduled run —
next Monday tries again.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
WORKFLOW_FILES = [
    os.path.join(ROOT, ".github", "workflows", "ci.yml"),
    os.path.join(ROOT, ".github", "workflows", "monitor.yml"),
    os.path.join(ROOT, ".github", "workflows", "python_version_bump.yml"),
]
RUFF_TOML = os.path.join(ROOT, "ruff.toml")
# The complete set of repository files this script is permitted to modify. Nothing else is
# opened for writing, so no README and no doc can ever be rewritten by a bump — the failure
# mode that corrupted dated history entries in a sibling repository.
ALLOWED_WRITES = frozenset({"ci.yml", "monitor.yml", "python_version_bump.yml", "ruff.toml"})
MANIFEST_URL = "https://raw.githubusercontent.com/actions/python-versions/main/versions-manifest.json"
PIN_RE = re.compile(r"(python-version:\s*')(\d+\.\d+\.\d+)(')")
RUFF_TARGET_RE = re.compile(r'(target-version\s*=\s*")py(\d+)(")')


def _present(paths: list[str]) -> list[str]:
    """Only the targets this repository actually has.

    A production repo is a subset of the dev repo: it ships monitor.yml and nothing else from
    this list. Skipping silently is what lets ONE script serve both without a prod-only fork.
    """
    return [p for p in paths if os.path.isfile(p)]


def _out(key: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    # A multi-line value needs the heredoc form; `key=value` would truncate at the newline.
    if "\n" in value:
        delim = "GEBUMPEOF"
        line = f"{key}<<{delim}\n{value}\n{delim}\n"
    else:
        line = f"{key}={value}\n"
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
    else:
        print(line, end="")


def current_pin() -> tuple[int, int, int] | None:
    """Read an exact patch pin from the first available allowlisted workflow."""
    for path in _present(WORKFLOW_FILES):
        with open(path, encoding="utf-8") as fh:
            match = PIN_RE.search(fh.read())
        if match:
            return tuple(int(part) for part in match.group(2).split("."))
    return None


def fetch_manifest() -> list[dict] | None:
    req = urllib.request.Request(MANIFEST_URL, headers={"User-Agent": "Game-Express-python-bump"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        print(f"::warning::could not read {MANIFEST_URL}: {e} — skipping this week's check")
        return None


def next_stable_version(manifest: list[dict], current: tuple[int, int, int]) -> tuple[int, int, int] | None:
    """Prefer a current-series patch, then the next minor; never skip series/major."""
    candidates = []
    if not isinstance(manifest, list):
        return None
    for entry in manifest:
        if not isinstance(entry, dict):
            continue
        version = str(entry.get("version") or "")
        if not entry.get("stable") or not re.fullmatch(r"3\.\d+\.\d+", version):
            continue
        files = entry.get("files")
        if not isinstance(files, list) or not any(
                isinstance(file, dict) and file.get("platform") == "linux" and file.get("arch") == "x64"
                for file in files):
            continue
        candidate = tuple(int(part) for part in version.split("."))
        if candidate > current:
            candidates.append(candidate)
    for series in (current[:2], (current[0], current[1] + 1)):
        eligible = [candidate for candidate in candidates if candidate[:2] == series]
        if eligible:
            return max(eligible)
    return None


def ruff_understands(target: str) -> bool:
    """True when the installed ruff accepts --target-version <target> (it rejects an
    unknown target with a usage error, exit code 2)."""
    try:
        proc = subprocess.run(["ruff", "check", "--target-version", target, "--no-cache", RUFF_TOML],
                              capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"::warning::could not run ruff to validate target-version {target}: {e}")
        return False
    return proc.returncode != 2 and "invalid value" not in (proc.stderr or "").lower()


def bump_workflow_files(new_minor: str) -> list[str]:
    """-> repo-relative paths actually rewritten. Absent files are skipped, not an error."""
    changed: list[str] = []
    for path in _present(WORKFLOW_FILES):
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        new_text = PIN_RE.sub(rf"\g<1>{new_minor}\g<3>", text)
        if new_text != text:
            assert os.path.basename(path) in ALLOWED_WRITES, path
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(new_text)
            changed.append(os.path.relpath(path, ROOT).replace(os.sep, "/"))
    return changed


def bump_ruff_toml(new_major: int, new_minor: int) -> bool:
    if not os.path.isfile(RUFF_TOML):
        print("note: no ruff.toml in this repository — nothing to bump there")
        return False
    target = f"py{new_major}{new_minor}"
    with open(RUFF_TOML, encoding="utf-8") as fh:
        current = RUFF_TARGET_RE.search(fh.read())
    if current and current.group(2) == f"{new_major}{new_minor}":
        return False
    if not ruff_understands(target):
        print(f"::warning::ruff does not recognise --target-version {target} yet — "
              "leaving ruff.toml unchanged; bump it separately once ruff adds support")
        return False
    with open(RUFF_TOML, encoding="utf-8") as fh:
        text = fh.read()
    new_text = RUFF_TARGET_RE.sub(rf"\g<1>py{new_major}{new_minor}\g<3>", text)
    if new_text != text:
        with open(RUFF_TOML, "w", encoding="utf-8") as fh:
            fh.write(new_text)
        return True
    return False


def write_body(old: str, new: str, new_full: str, ruff_bumped: bool) -> None:
    ruff_line = ("Updated to the validated major/minor target." if ruff_bumped else
                 "Unchanged: same target or installed Ruff does not support the new target.")
    body = f"""### Python update: {old} → {new}

Candidate {new_full} is a stable Linux x64 build in the actions/python-versions manifest.
The workflow publishes this PR only after interpreter installation, wheels-only dependency
installation, compilation, validation, all offline runners and preview generation pass.

- Existing allowlisted workflow pins change from {old} to {new}; no historical docs are rewritten.
- Ruff target: {ruff_line}
- Patch and next-minor proposals always wait for attended review/merge, regardless of
  the legacy AUTO_MERGE_PYTHON_BUMP variable. Major migrations are manual work.
- Review affected current-runtime docs before merging. After updating a live workflow,
  confirm its next run executes; GitHub can still require human security approval.
"""
    path = os.environ.get("PR_BODY_PATH", "/tmp/python_bump_body.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)


def main() -> int:
    pin = current_pin()
    if pin is None:
        print("::warning::could not find a python-version pin in ci.yml — skipping")
        _out("changed", "false")
        return 0
    old_str = ".".join(str(part) for part in pin)
    # Known from here on. Emit it now so every run reports the pin — including the
    # "up to date" and manifest-failure paths, where it is the only useful fact.
    _out("old_version", old_str)
    manifest = fetch_manifest()
    if manifest is None:
        _out("changed", "false")
        return 0
    candidate = next_stable_version(manifest, pin)
    if candidate is None:
        print(f"up to date: no eligible stable patch or next minor after {old_str}")
        _out("changed", "false")
        return 0
    new_major, new_minor, _ = candidate
    new_str = new_full = ".".join(str(part) for part in candidate)
    print(f"bumping {old_str} -> {new_str} (newest stable patch: {new_full})")
    changed_files = bump_workflow_files(new_str)
    ruff_bumped = bump_ruff_toml(new_major, new_minor)
    if ruff_bumped:
        changed_files.append("ruff.toml")
    if not changed_files:
        # The pin was found but nothing matched the rewrite - propose nothing rather than
        # open an empty PR.
        print("::warning::no file was actually rewritten - skipping")
        _out("changed", "false")
        return 0
    write_body(old_str, new_str, new_full, ruff_bumped)
    _out("changed", "true")
    _out("new_version", new_str)
    _out("ruff_bumped", "true" if ruff_bumped else "false")
    # Exactly what to stage. Lets ONE workflow serve dev and prod: whatever this repo
    # happens to contain is what gets committed, with no prod-only edit to add-paths.
    _out("changed_files", "\n".join(changed_files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
