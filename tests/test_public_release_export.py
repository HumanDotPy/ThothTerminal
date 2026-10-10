"""Release security contract at the actual archive boundary."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import zipfile

import pytest

_spec = importlib.util.spec_from_file_location(
    "public_release", Path(__file__).parents[1] / "scripts/public_release.py"
)
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)


def git(repo, *args, input=None):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          input=input, capture_output=True).stdout.decode().strip()


@pytest.fixture
def source(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "scripts").mkdir()
    (repo / "thoth_terminal").mkdir()
    (repo / "pyproject.toml").write_text('[project]\nversion="0.3.0b1"\n')
    (repo / "thoth_terminal/main.py").write_text('print("public app")\n')
    (repo / "scripts/public-release-policy.json").write_text(json.dumps({
        "files": ["pyproject.toml", "thoth_terminal/main.py"], "binary_sha256": {},
    }))
    git(repo, "init", "-q")
    git(repo, "add", ".")
    git(repo, "-c", "user.name=Release Test", "-c", "user.email=test@example.com",
        "commit", "-qm", "Fixture")
    return repo, tmp_path / "exports"


def test_only_allowlisted_bytes_enter_archive_with_verified_receipt(source):
    repo, output = source
    # A credential/data cache is tracked intentionally: Git alone is not a privacy boundary.
    (repo / "private.db").write_bytes(b"private database")
    (repo / "secret.env").write_text("API_KEY=private-value")
    git(repo, "add", ".")
    git(repo, "-c", "user.name=Release Test", "-c", "user.email=test@example.com",
        "commit", "-qm", "Excluded operator state")
    archive = release.export(repo, output)
    with zipfile.ZipFile(archive) as bundle:
        files = {name.split("/", 1)[1]: bundle.read(name) for name in bundle.namelist()}
    assert set(files) == {"thoth_terminal/main.py", "pyproject.toml", "PUBLIC_RELEASE_MANIFEST.json"}
    receipt = json.loads(files["PUBLIC_RELEASE_MANIFEST.json"])
    assert receipt["dirty_preview"] is False
    assert receipt["files"]["thoth_terminal/main.py"]["sha256"] == release.hashlib.sha256(files["thoth_terminal/main.py"]).hexdigest()


@pytest.mark.parametrize("payload, category", [
    ('key="ghp_' + 'A' * 30 + '"', "credential"),
    ('root="C:/Users/' + 'Operator/Documents/private"', "personal_path"),
    ('endpoint="http://' + '192.168.1.80:8765"', "private_network"),
    ('endpoint="https://' + 'alice:password@example.com"', "url_credentials"),
    ('endpoint="https://' + 'private-machine.ts.net"', "private_topology"),
    ('prompt="X list' + ' profile: confidential"', "personal_prompt"),
])
def test_included_sensitive_payload_is_rejected_without_secret_diagnostics(source, payload, category):
    repo, output = source
    (repo / "thoth_terminal/main.py").write_text(payload)
    with pytest.raises(release.ReleaseError) as error:
        release.export(repo, output, allow_dirty=True, reviewed_paths=("thoth_terminal/main.py",))
    assert category in str(error.value)
    assert payload not in str(error.value)
    assert not output.exists()


def test_dirty_flag_does_not_approve_unreviewed_changes(source):
    repo, output = source
    (repo / "thoth_terminal/main.py").write_text('print("changed app")')
    with pytest.raises(release.ReleaseError, match="explicit reviewed paths"):
        release.export(repo, output, allow_dirty=True)
    archive = release.export(repo, output, allow_dirty=True, reviewed_paths=("thoth_terminal/main.py",))
    with zipfile.ZipFile(archive) as bundle:
        receipt = json.loads(bundle.read(next(n for n in bundle.namelist() if n.endswith("MANIFEST.json"))))
    assert receipt["dirty_preview"] is True


def test_git_symlink_rejected_even_when_windows_checkout_materializes_it_as_text(source):
    repo, output = source
    blob = git(repo, "hash-object", "-w", "--stdin", input=b"private.env")
    git(repo, "update-index", "--cacheinfo", f"120000,{blob},thoth_terminal/main.py")
    with pytest.raises(release.ReleaseError, match="Non-regular Git input"):
        release.export(repo, output, allow_dirty=True, reviewed_paths=("thoth_terminal/main.py",))


def test_preview_can_use_committed_bytes_for_unrelated_dirty_files(source):
    repo, output = source
    (repo / "thoth_terminal/main.py").write_text('key="ghp_' + 'A' * 30 + '"')
    archive = release.export(repo, output, allow_dirty=True, committed_unreviewed=True)
    with zipfile.ZipFile(archive) as bundle:
        name = next(n for n in bundle.namelist() if n.endswith("/thoth_terminal/main.py"))
        assert bundle.read(name).decode().splitlines() == ['print("public app")']


def test_policy_cannot_escape_repository_or_admit_unknown_binary(source):
    repo, output = source
    policy = repo / "scripts/public-release-policy.json"
    policy.write_text(json.dumps({"files": ["../outside"], "binary_sha256": {}}))
    with pytest.raises(release.ReleaseError, match="Unsafe release path"):
        release.export(repo, output, allow_dirty=True,
                       reviewed_paths=("scripts/public-release-policy.json", "../outside"))
    policy.write_text(json.dumps({"files": ["pyproject.toml", "thoth_terminal/main.py"], "binary_sha256": {}}))
    (repo / "thoth_terminal/main.py").write_bytes(b"\x00private binary")
    with pytest.raises(release.ReleaseError, match="Unexpected binary"):
        release.export(repo, output, allow_dirty=True,
                       reviewed_paths=("scripts/public-release-policy.json", "thoth_terminal/main.py"))


def test_public_policy_cannot_include_operator_database(source):
    repo, output = source
    (repo / "private.db").write_text("operator research")
    (repo / "scripts/public-release-policy.json").write_text(json.dumps({
        "files": ["pyproject.toml", "private.db"], "binary_sha256": {},
    }))
    with pytest.raises(release.ReleaseError, match="Forbidden release input"):
        release.export(repo, output, allow_dirty=True,
                       reviewed_paths=("scripts/public-release-policy.json", "private.db"))


def test_parent_directory_junction_is_rejected_before_reading(source):
    repo, output = source
    package = repo / "thoth_terminal"
    target = repo.parent / "private-package"
    package.rename(target)
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(package), str(target)],
                       check=True, capture_output=True)
    else:
        package.symlink_to(target, target_is_directory=True)
    with pytest.raises(release.ReleaseError, match="Links/reparse points"):
        release.export(repo, output, allow_dirty=True, reviewed_paths=("thoth_terminal/main.py",))
