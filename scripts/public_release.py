"""Export a reviewed, data-free source distribution; never copy directory trees."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tomllib
import zipfile


class ReleaseError(ValueError):
    pass


TOP_LEVEL = {".env.example", ".gitignore", "LICENSE", "README.md", "SECURITY.md", "CONTRIBUTING.md", "pyproject.toml"}
PUBLIC_DOCS = {"docs/public_release_quickstart.md", "docs/native_html.md"}
BUILD_SCRIPTS = {"scripts/build-native-html.ps1", "scripts/build-desktop.ps1", "scripts/start-local-postgres.ps1",
                 "scripts/maintain_catalog_indexes.py",
                 "scripts/build-public-release.ps1", "scripts/public_release.py", "scripts/public-release-policy.json"}
PUBLIC_TESTS = {"tests/test_bare_initialization.py", "tests/test_public_http_privacy.py", "tests/test_public_release_export.py", "tests/test_versioning.py", "tests/test_security_modes.py"}
ICONS = {f"thoth_terminal/web/static/icons/{name}" for name in
         ("thoth-terminal-1024.png", "thoth-terminal-chatgpt-128.png", "thoth-terminal.ico")}


def permitted(name: str) -> bool:
    path = safe_path(name)
    if name in TOP_LEVEL | PUBLIC_DOCS | BUILD_SCRIPTS | PUBLIC_TESTS | ICONS:
        return True
    if any(part.lower() in {"data", "profiles", "fixtures", "private", "secrets", "tests", "target", "__pycache__"} for part in path.parts):
        return False
    if name.startswith("thoth_terminal/"):
        return path.suffix in {".py", ".html", ".css", ".js", ".mjs", ".md", ".tiktoken"} or name == "thoth_terminal/web/static/manifest.json"
    if name.startswith("native/thoth_html/"):
        return name in {"native/thoth_html/Cargo.toml", "native/thoth_html/Cargo.lock", "native/thoth_html/pyproject.toml"} or (name.startswith("native/thoth_html/src/") and path.suffix == ".rs")
    if name.startswith("extensions/thoth_x_capture/"):
        return path.suffix in {".js", ".html", ".md"} or name == "extensions/thoth_x_capture/manifest.json"
    return False


def git(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True).stdout


def safe_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (not name or "\\" in name or ":" in name or path.is_absolute()
            or any(part in {"", ".", ".."} for part in name.split("/"))):
        raise ReleaseError("Unsafe release path")
    return path


def read_file(repo: Path, name: str) -> bytes:
    path = repo
    for part in safe_path(name).parts:
        path /= part
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ReleaseError(f"Links/reparse points forbidden: {name}")
    if not path.is_file():
        raise ReleaseError(f"Not a regular file: {name}")
    return path.read_bytes()


# Diagnostics report categories and paths, never matched values.
PATTERNS = {
    "credential": re.compile(r"AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9_]{20,}|sk-(?:proj-|svcacct-)?[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_-]+"),
    "personal_path": re.compile(r"[A-Za-z]:[/\\]+Users[/\\]+(?!You(?:[/\\]|\b)|Public(?:[/\\]|\b))[^/\\\s\"']+|[A-Za-z]:[/\\]+(?:ThothTerminal|ArtCraftLab)\b|/(?:home|Users)/[^/\s\"']+/(?:\.ssh|\.codex|Desktop|Documents)", re.I),
    "private_topology": re.compile(r"https?://[^*\s/\"']+\.(?:ts\.net|trycloudflare\.com|ngrok(?:-free)?\.app)", re.I),
    "personal_prompt": re.compile("X list" + r" profile|X_LIST_SOURCE" + "_IDENTITIES"),
    "url_credentials": re.compile(r"https?://[^\s/\"']+:[^\s/@\"']+@", re.I),
    "private_network": re.compile(r"https?://(?:10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d+\.\d+)(?=[:/\s\"'])"),
}


def scan(name: str, payload: bytes, binary_hashes: dict[str, str]) -> None:
    if name in binary_hashes:
        if hashlib.sha256(payload).hexdigest() != binary_hashes[name]:
            raise ReleaseError(f"Unreviewed binary asset: {name}")
        return
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ReleaseError(f"Unexpected binary payload: {name}") from None
    if "\0" in text:
        raise ReleaseError(f"Unexpected binary payload: {name}")
    for category, pattern in PATTERNS.items():
        if pattern.search(text):
            raise ReleaseError(f"Privacy scan failed [{category}]: {name}")


def export(repo: Path, output: Path, *, allow_dirty: bool = False,
           reviewed_paths: tuple[str, ...] = (), committed_unreviewed: bool = False) -> Path:
    repo = repo.resolve()
    policy_name = "scripts/public-release-policy.json"
    policy_payload = read_file(repo, policy_name)
    policy = json.loads(policy_payload)
    names = policy["files"]
    for name in names:
        if not permitted(name):
            raise ReleaseError(f"Forbidden release input: {name}")
    if set(policy["binary_sha256"]) - ICONS:
        raise ReleaseError("Only reviewed public icons may be binary inputs")
    if len(set(names)) != len(names):
        raise ReleaseError("Duplicate policy paths")
    tracked = set(git(repo, "ls-files", "-z").decode().split("\0"))
    for entry in git(repo, "ls-files", "--stage", "-z").decode().split("\0"):
        if not entry:
            continue
        metadata, name = entry.split("\t", 1)
        if name in names and metadata.split()[0] not in {"100644", "100755"}:
            raise ReleaseError(f"Non-regular Git input forbidden: {name}")
    changed = set(git(repo, "diff", "HEAD", "--name-only", "-z").decode().split("\0")) - {""}
    untracked = set(git(repo, "ls-files", "--others", "--exclude-standard", "-z").decode().split("\0")) - {""}
    dirty = bool(changed or untracked)
    if dirty and not allow_dirty:
        raise ReleaseError("Dirty worktree: commit reviewed scope or explicitly request a local preview")
    reviewed = set(reviewed_paths)
    for name in reviewed:
        safe_path(name)
    required_review = (set(names) | {policy_name}) & (changed | untracked)
    if policy_name in required_review and policy_name not in reviewed:
        raise ReleaseError("Release policy requires explicit review")
    if required_review - reviewed and not committed_unreviewed:
        raise ReleaseError("Preview requires explicit reviewed paths: " + ", ".join(sorted(required_review - reviewed)))
    if set(names) - tracked - reviewed:
        raise ReleaseError("Untracked release inputs require explicit preview review")
    if not allow_dirty and reviewed:
        raise ReleaseError("Reviewed-path overrides are only allowed for local previews")
    if committed_unreviewed and not allow_dirty:
        raise ReleaseError("Committed-unreviewed mode is only allowed for local previews")
    # Snapshot once, scan those exact bytes, and write only those bytes.
    payloads = {}
    for name in names:
        safe_path(name)
        if committed_unreviewed and name in changed and name not in reviewed:
            payload = git(repo, "show", f"HEAD:{name}")
        else:
            payload = read_file(repo, name)
        scan(name, payload, policy["binary_sha256"])
        payloads[name] = payload
    version = tomllib.loads(payloads["pyproject.toml"].decode())["project"]["version"]
    if not re.fullmatch(r"[A-Za-z0-9.+-]+", version):
        raise ReleaseError("Unsafe release version")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    release = f"thoth-terminal-{version}-{stamp}"
    manifest = {
        "version": version, "git_sha": git(repo, "rev-parse", "HEAD").decode().strip(),
        "dirty_preview": dirty, "built_at_utc": stamp,
        "reviewed_worktree_paths": sorted(reviewed & set(names)),
        "committed_unreviewed": committed_unreviewed,
        "validation": "allowlisted tracked source; text privacy checks; pinned binary assets",
        "policy_sha256": hashlib.sha256(policy_payload).hexdigest(),
        "files": {name: {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                  for name, data in payloads.items()},
    }
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"{release}.zip"
    with archive.open("xb") as stream, zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, data in payloads.items():
            bundle.writestr(f"{release}/{name}", data)
        bundle.writestr(f"{release}/PUBLIC_RELEASE_MANIFEST.json", json.dumps(manifest, indent=2) + "\n")
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--reviewed-path", action="append", default=[])
    parser.add_argument("--committed-unreviewed", action="store_true")
    args = parser.parse_args()
    try:
        archive = export(args.repo, args.output or args.repo / "dist/public-release",
                         allow_dirty=args.allow_dirty, reviewed_paths=tuple(args.reviewed_path),
                         committed_unreviewed=args.committed_unreviewed)
    except (ReleaseError, OSError, subprocess.CalledProcessError) as error:
        print(f"Release rejected: {error}", file=sys.stderr)
        return 1
    print(f"archive={archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
