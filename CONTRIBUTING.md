# Contributing to Thoth Terminal

Thank you for helping improve Thoth.

1. Create a focused branch from the current default branch.
2. Keep credentials, local research, runtime state, screenshots, and database
   files out of the repository.
3. Add or update tests for behavior changes.
4. Run the focused test set and the public-release privacy scan.
5. Open a pull request that explains the user-facing outcome and any migration
   or security impact.

## Tests

Install pytest in your development environment and run focused tests:

```powershell
python -m pip install pytest pytest-subtests
python -m pip install --target .tmp/test-deps "httpx>=0.27,<1"
$env:PYTHONPATH = (Resolve-Path .tmp/test-deps).Path
python -m pytest tests/test_public_http_privacy.py tests/test_security_modes.py -q
```

Validate the source export from a clean, reviewed commit:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\build-public-release.ps1
```

The public boundary tests use an isolated database and do not start the runtime.
Their HTTP client is isolated because the optional translation integration's
current dependency pins an older HTTPX version.
Do not run experimental migrations or ingestion against your working research
database. New export files require an explicit review of the release policy.

By contributing, you agree that your contribution is licensed under the MIT
License.
