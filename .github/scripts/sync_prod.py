"""Synchronize the production runtime subset from uesu/Game-Express to the live monitor repo.

Used by .github/workflows/sync_prod.yml whenever commits land on main (or via workflow_dispatch).

Safety guarantees:
  1. Live state protection: `state/state.json` is NEVER created, overwritten, staged, or deleted
     in the target production repository.
  2. Program announcement seed union: `config/program_announcements.json` preserves any tweet IDs
     discovered live by the production monitor while adding/updating entries from the dev repo.
  3. Dev-only exclusion: `docs/`, `README.md`, `AGENTS.md`, `PRIVACY_POLICY.md`,
     `TERMS_OF_SERVICE.md`, `.env.example`, `previews/`, `archive/`, and
     `.github/workflows/sync_prod.yml` are never copied into the production repository.
  4. Concurrent monitor commit resilience: if the 5-minute production monitor pushes a state
     commit while a sync is in flight, the push retry fetches the new remote tip, resets to
     `FETCH_HEAD`, and re-applies the deterministic tree sync on top of the fresh state.
  5. Workflow-scope fallback: if `.github/workflows/*` changed and the configured token lacks
     `workflow` permission, the script automatically falls back to pushing all non-workflow code,
     config, and test updates and emits a clear warning naming the skipped workflow files.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE_REPO = "uesu/Game-Express"
DEFAULT_TARGET_REPOS = ("uesu/8mqzv4k2n7c",)
DEFAULT_BRANCH = "main"

SYNC_DIRS = (
    "gamexpress",
    "tests",
    ".github/scripts",
)

SYNC_WORKFLOW_FILES = (
    ".github/workflows/ci.yml",
    ".github/workflows/monitor.yml",
    ".github/workflows/python_version_bump.yml",
)

DEV_ONLY_WORKFLOW_FILES = (
    ".github/workflows/sync_prod.yml",
)

SYNC_SINGLE_FILES = (
    ".github/dependabot.yml",
    ".github/zizmor.yml",
    ".gitignore",
    "requirements.txt",
    "ruff.toml",
    "config/games.json",
    "config/overrides.json",
    "state/.gitkeep",
)

SEMANTIC_MERGE_FILES = (
    "config/program_announcements.json",
)

PROTECTED_PROD_FILES = (
    "state/state.json",
)

EXCLUDED_DEV_PATHS = (
    "docs",
    "README.md",
    "AGENTS.md",
    "PRIVACY_POLICY.md",
    "TERMS_OF_SERVICE.md",
    ".env.example",
    "previews",
    "archive",
    ".github/workflows/sync_prod.yml",
)

IGNORED_NAMES = frozenset({"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".DS_Store"})
IGNORED_SUFFIXES = (".pyc", ".pyo")

WORKFLOW_SCOPE_RE = re.compile(
    r"(without\s+[`'\"]?workflow[`'\"]?\s+scope|refusing to allow.*workflow)",
    re.IGNORECASE,
)
AUTH_ERROR_RE = re.compile(
    r"(Authentication failed|could not read Username|Write access to repository not granted|"
    r"Permission to .* denied|The requested URL returned error: 40[134]|Repository not found)",
    re.IGNORECASE,
)


def _redact(text: str, token: str) -> str:
    if not text:
        return ""
    out = text
    if token:
        out = out.replace(token, "***")
    return re.sub(r"x-access-token:[^@\s]+@", "x-access-token:***@", out)


def _should_ignore(rel_parts: tuple[str, ...]) -> bool:
    if any(part in IGNORED_NAMES for part in rel_parts):
        return True
    return bool(rel_parts and rel_parts[-1].endswith(IGNORED_SUFFIXES))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def merge_program_announcements_bytes(src_bytes: bytes, dst_bytes: bytes | None) -> bytes:
    """Union-merge `config/program_announcements.json` so live-discovered prod seeds survive."""
    if not dst_bytes:
        return src_bytes
    try:
        src_data = json.loads(src_bytes.decode("utf-8"))
        dst_data = json.loads(dst_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return src_bytes
    if not isinstance(src_data, dict) or not isinstance(dst_data, dict):
        return src_bytes

    extra_prod_entries: list[tuple[str, str, dict]] = []
    for game_key, dst_versions in dst_data.items():
        if str(game_key).startswith("_") or not isinstance(dst_versions, dict):
            continue
        src_versions = src_data.get(game_key)
        if not isinstance(src_versions, dict):
            src_versions = {}
        for ver, entry in dst_versions.items():
            if not isinstance(entry, dict) or not entry.get("id"):
                continue
            if ver not in src_versions:
                extra_prod_entries.append((game_key, ver, entry))

    if not extra_prod_entries:
        return src_bytes

    merged = json.loads(src_bytes.decode("utf-8"))
    for game_key, ver, entry in extra_prod_entries:
        per_game = merged.setdefault(game_key, {})
        if isinstance(per_game, dict) and ver not in per_game:
            per_game[ver] = entry
    return (json.dumps(merged, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def sync_tree(
    src_root: Path,
    dst_root: Path,
    *,
    include_workflows: bool = True,
) -> dict[str, list[str]]:
    """Synchronize the allowed runtime subset from `src_root` into `dst_root`."""
    src_root = src_root.resolve()
    dst_root = dst_root.resolve()
    if src_root == dst_root:
        raise ValueError("Refusing to sync a repository directory onto itself.")

    protected_before: dict[str, bytes | None] = {}
    for rel in PROTECTED_PROD_FILES:
        target = dst_root / rel
        protected_before[rel] = target.read_bytes() if target.is_file() else None

    updated: list[str] = []
    removed: list[str] = []
    preserved: list[str] = [rel for rel, data in protected_before.items() if data is not None]

    for rel_dir in SYNC_DIRS:
        sdir = src_root / rel_dir
        ddir = dst_root / rel_dir
        if not sdir.is_dir():
            continue
        expected_rel_files: set[Path] = set()
        for spath in sorted(sdir.rglob("*")):
            if not spath.is_file():
                continue
            rel_inside = spath.relative_to(sdir)
            if _should_ignore(rel_inside.parts):
                continue
            expected_rel_files.add(rel_inside)
            dpath = ddir / rel_inside
            rel_posix = (Path(rel_dir) / rel_inside).as_posix()
            sbytes = spath.read_bytes()
            if not dpath.is_file() or dpath.read_bytes() != sbytes:
                dpath.parent.mkdir(parents=True, exist_ok=True)
                dpath.write_bytes(sbytes)
                updated.append(rel_posix)

        if ddir.is_dir():
            for dpath in sorted(ddir.rglob("*"), reverse=True):
                if dpath.is_file():
                    rel_inside = dpath.relative_to(ddir)
                    if _should_ignore(rel_inside.parts) or rel_inside not in expected_rel_files:
                        rel_posix = (Path(rel_dir) / rel_inside).as_posix()
                        dpath.unlink()
                        if not _should_ignore(rel_inside.parts):
                            removed.append(rel_posix)
                elif dpath.is_dir():
                    try:
                        dpath.rmdir()
                    except OSError:
                        pass

    for rel_file in SYNC_SINGLE_FILES:
        spath = src_root / rel_file
        dpath = dst_root / rel_file
        if not spath.is_file():
            continue
        sbytes = spath.read_bytes()
        if not dpath.is_file() or dpath.read_bytes() != sbytes:
            dpath.parent.mkdir(parents=True, exist_ok=True)
            dpath.write_bytes(sbytes)
            updated.append(rel_file)

    if include_workflows:
        for rel_wf in SYNC_WORKFLOW_FILES:
            spath = src_root / rel_wf
            dpath = dst_root / rel_wf
            if not spath.is_file():
                continue
            sbytes = spath.read_bytes()
            if not dpath.is_file() or dpath.read_bytes() != sbytes:
                dpath.parent.mkdir(parents=True, exist_ok=True)
                dpath.write_bytes(sbytes)
                updated.append(rel_wf)
        for dev_wf in DEV_ONLY_WORKFLOW_FILES:
            dpath = dst_root / dev_wf
            if dpath.is_file():
                dpath.unlink()
                removed.append(dev_wf)

    for rel_merge in SEMANTIC_MERGE_FILES:
        spath = src_root / rel_merge
        dpath = dst_root / rel_merge
        if not spath.is_file():
            continue
        sbytes = spath.read_bytes()
        dbytes = dpath.read_bytes() if dpath.is_file() else None
        merged_bytes = merge_program_announcements_bytes(sbytes, dbytes)
        if dbytes != merged_bytes:
            dpath.parent.mkdir(parents=True, exist_ok=True)
            dpath.write_bytes(merged_bytes)
            updated.append(rel_merge)

    for rel, before_bytes in protected_before.items():
        target = dst_root / rel
        after_bytes = target.read_bytes() if target.is_file() else None
        if after_bytes != before_bytes:
            if before_bytes is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(before_bytes)
            raise RuntimeError(f"Protected production file {rel} was modified during sync_tree!")

    return {
        "updated": sorted(set(updated)),
        "removed": sorted(set(removed)),
        "preserved": sorted(set(preserved)),
    }


def _run_git(
    args: list[str],
    *,
    cwd: Path,
    token: str = "",
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
    }
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and proc.returncode != 0:
        stderr = _redact(proc.stderr.strip() or proc.stdout.strip(), token)
        raise RuntimeError(f"git {' '.join(args[:2])} failed ({proc.returncode}): {stderr}")
    return proc


def _stage_and_verify(clone_dir: Path, token: str = "") -> list[str]:
    _run_git(["add", "-A"], cwd=clone_dir, token=token)
    for rel in (*PROTECTED_PROD_FILES, *DEV_ONLY_WORKFLOW_FILES):
        _run_git(["reset", "-q", "HEAD", "--", rel], cwd=clone_dir, token=token, check=False)
        _run_git(["checkout", "--", rel], cwd=clone_dir, token=token, check=False)
    diff = _run_git(["diff", "--cached", "--name-only"], cwd=clone_dir, token=token)
    staged = [line.strip() for line in diff.stdout.splitlines() if line.strip()]
    forbidden = set(staged).intersection({*PROTECTED_PROD_FILES, *EXCLUDED_DEV_PATHS})
    if forbidden:
        raise RuntimeError(f"Forbidden path(s) staged for production sync: {sorted(forbidden)}")
    return staged


def parse_target_repos(raw: str | None) -> list[str]:
    if not raw or not raw.strip():
        return list(DEFAULT_TARGET_REPOS)
    repos: list[str] = []
    for part in re.split(r"[\s,]+", raw.strip()):
        cleaned = part.strip().strip("/")
        if cleaned and cleaned not in repos:
            repos.append(cleaned)
    return repos or list(DEFAULT_TARGET_REPOS)


def sync_remote_repo(
    *,
    src_root: Path,
    target_repo: str,
    token: str,
    branch: str = DEFAULT_BRANCH,
    source_repo: str = DEFAULT_SOURCE_REPO,
    source_sha: str = "",
    commit_subject: str = "",
    skip_ci: bool = False,
    dry_run: bool = False,
    strict_auth: bool = False,
) -> dict[str, object]:
    """Clone `target_repo`, apply `sync_tree`, and push atomically with retry on race."""
    if target_repo.strip().lower() == source_repo.strip().lower():
        raise ValueError(f"Refusing to sync source repository {source_repo!r} onto itself.")

    if not token and not dry_run:
        msg = (
            f"No SYNC_PAT / BUMP_PAT secret available to push to {target_repo}. "
            "Configure PROD_SYNC_PAT or grant BUMP_PAT Contents+Workflows write access."
        )
        if strict_auth:
            raise RuntimeError(msg)
        print(f"::warning::{msg}")
        return {"repo": target_repo, "status": "skipped-no-token", "files": [], "message": msg}

    short_sha = (source_sha or "HEAD")[:7]
    subject_suffix = f": {commit_subject.strip()}" if commit_subject.strip() else ""
    skip_suffix = " [skip ci]" if skip_ci else ""
    commit_msg = f"sync: update from {source_repo}@{short_sha}{subject_suffix}{skip_suffix}"
    if len(commit_msg) > 240:
        commit_msg = commit_msg[:237] + "..."

    remote_url = (
        f"https://x-access-token:{token}@github.com/{target_repo}.git"
        if token
        else f"https://github.com/{target_repo}.git"
    )

    with tempfile.TemporaryDirectory(prefix="ge-prod-sync-") as tmp:
        clone_dir = Path(tmp) / "prod"
        clone_proc = _run_git(
            ["clone", "--depth", "1", "--branch", branch, remote_url, str(clone_dir)],
            cwd=Path(tmp),
            token=token,
            check=False,
        )
        if clone_proc.returncode != 0:
            err = _redact(clone_proc.stderr.strip() or clone_proc.stdout.strip(), token)
            if AUTH_ERROR_RE.search(err) and not strict_auth:
                warn = (
                    f"Could not clone {target_repo} ({err}). Ensure BUMP_PAT or PROD_SYNC_PAT "
                    f"has repository access to {target_repo}."
                )
                print(f"::warning::{warn}")
                return {"repo": target_repo, "status": "skipped-auth", "files": [], "message": warn}
            raise RuntimeError(f"Failed to clone {target_repo}: {err}")

        _run_git(["config", "user.name", "github-actions[bot]"], cwd=clone_dir, token=token)
        _run_git(
            ["config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"],
            cwd=clone_dir,
            token=token,
        )

        include_workflows = True
        skipped_workflows: list[str] = []

        for attempt in range(1, 6):
            sync_tree(src_root, clone_dir, include_workflows=include_workflows)
            staged = _stage_and_verify(clone_dir, token=token)
            if not staged:
                msg = (
                    f"{target_repo} is already up to date with {source_repo}@{short_sha}"
                    + (
                        f" (workflow files skipped: {', '.join(skipped_workflows)})"
                        if skipped_workflows
                        else ""
                    )
                )
                return {
                    "repo": target_repo,
                    "status": "up-to-date" if not skipped_workflows else "skipped-workflows",
                    "files": [],
                    "skipped_workflows": skipped_workflows,
                    "message": msg,
                }

            if dry_run:
                return {
                    "repo": target_repo,
                    "status": "dry-run",
                    "files": staged,
                    "skipped_workflows": skipped_workflows,
                    "message": f"Dry run: {len(staged)} file(s) would be synced to {target_repo}.",
                }

            _run_git(["commit", "-m", commit_msg], cwd=clone_dir, token=token)
            push_proc = _run_git(
                ["push", "origin", f"HEAD:{branch}"],
                cwd=clone_dir,
                token=token,
                check=False,
            )
            if push_proc.returncode == 0:
                head_sha = _run_git(
                    ["rev-parse", "--short", "HEAD"], cwd=clone_dir, token=token
                ).stdout.strip()
                return {
                    "repo": target_repo,
                    "status": "synced",
                    "commit": head_sha,
                    "files": staged,
                    "skipped_workflows": skipped_workflows,
                    "message": f"Synced {len(staged)} file(s) to {target_repo}@{head_sha}.",
                }

            err = _redact(push_proc.stderr.strip() or push_proc.stdout.strip(), token)
            if include_workflows and WORKFLOW_SCOPE_RE.search(err):
                skipped_workflows = [f for f in staged if f.startswith(".github/workflows/")]
                print(
                    f"::warning::Token lacks `workflow` scope for {target_repo}; "
                    f"retrying sync without workflow file(s): {', '.join(skipped_workflows)}"
                )
                include_workflows = False
                _run_git(["fetch", "origin", branch, "--depth", "1"], cwd=clone_dir, token=token)
                _run_git(["reset", "--hard", "FETCH_HEAD"], cwd=clone_dir, token=token)
                continue

            if AUTH_ERROR_RE.search(err):
                if not strict_auth:
                    warn = (
                        f"Push to {target_repo} denied ({err}). Grant BUMP_PAT or PROD_SYNC_PAT "
                        f"Contents (and Workflows) write access on {target_repo}."
                    )
                    print(f"::warning::{warn}")
                    return {
                        "repo": target_repo,
                        "status": "skipped-auth",
                        "files": staged,
                        "message": warn,
                    }
                raise RuntimeError(f"Push to {target_repo} failed due to permissions: {err}")

            if attempt < 5:
                print(
                    f"::warning::Push attempt {attempt}/5 to {target_repo} raced with a remote "
                    f"update ({err}); fetching latest {branch} tip and re-applying sync."
                )
                time.sleep(attempt * 2)
                _run_git(["fetch", "origin", branch, "--depth", "1"], cwd=clone_dir, token=token)
                _run_git(["reset", "--hard", "FETCH_HEAD"], cwd=clone_dir, token=token)
            else:
                raise RuntimeError(f"Could not push to {target_repo} after 5 attempts: {err}")

    raise RuntimeError(f"Unexpected exit from sync loop for {target_repo}")


def write_step_summary(results: list[dict[str, object]], source_sha: str) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    lines = [
        "## Production Sync Summary",
        "",
        f"- **Source commit:** `{source_sha[:12] if source_sha else 'HEAD'}`",
        "",
        "| Target Repository | Status | Commit | Files Synced | Details |",
        "|---|---|---|---|---|",
    ]
    for res in results:
        repo = str(res.get("repo", ""))
        status = str(res.get("status", ""))
        commit = str(res.get("commit", "—"))
        files = res.get("files") or []
        file_count = len(files) if isinstance(files, list) else 0
        msg = str(res.get("message", ""))
        lines.append(f"| `{repo}` | `{status}` | `{commit}` | {file_count} | {msg} |")
        if isinstance(files, list) and files:
            lines.append("")
            lines.append(f"<details><summary>Synced files for <code>{repo}</code> ({file_count})</summary>")
            lines.append("")
            for f in files:
                lines.append(f"- `{f}`")
            lines.append("")
            lines.append("</details>")
            lines.append("")
    text = "\n".join(lines) + "\n"
    print(text)
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(text)


def main() -> int:
    source_repo = os.environ.get("SOURCE_REPO", DEFAULT_SOURCE_REPO).strip() or DEFAULT_SOURCE_REPO
    source_sha = os.environ.get("SOURCE_SHA", "").strip()
    commit_subject = os.environ.get("COMMIT_SUBJECT", "").strip()
    target_repos = parse_target_repos(os.environ.get("TARGET_REPOS"))
    token = (os.environ.get("SYNC_PAT") or os.environ.get("BUMP_PAT") or "").strip()
    branch = os.environ.get("TARGET_BRANCH", DEFAULT_BRANCH).strip() or DEFAULT_BRANCH
    skip_ci = os.environ.get("SKIP_CI", "0").strip().lower() in ("1", "true", "yes")
    dry_run = os.environ.get("DRY_RUN", "0").strip().lower() in ("1", "true", "yes")
    strict_auth = os.environ.get("STRICT_AUTH", "0").strip().lower() in ("1", "true", "yes")

    if not source_sha:
        proc = _run_git(["rev-parse", "HEAD"], cwd=ROOT, check=False)
        source_sha = proc.stdout.strip()
    if not commit_subject:
        proc = _run_git(["log", "-1", "--pretty=%s"], cwd=ROOT, check=False)
        commit_subject = proc.stdout.strip()

    results: list[dict[str, object]] = []
    for repo in target_repos:
        res = sync_remote_repo(
            src_root=ROOT,
            target_repo=repo,
            token=token,
            branch=branch,
            source_repo=source_repo,
            source_sha=source_sha,
            commit_subject=commit_subject,
            skip_ci=skip_ci,
            dry_run=dry_run,
            strict_auth=strict_auth,
        )
        results.append(res)

    write_step_summary(results, source_sha)
    return 0


if __name__ == "__main__":
    sys.exit(main())
