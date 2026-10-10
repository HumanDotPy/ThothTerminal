# Thoth Terminal quickstart

Thoth is a local-first research terminal. Your sources, documents, notes, and
working research stay on your machine unless you explicitly connect an
external service or publish a report.

## Requirements

- Windows 11 or a current Linux distribution
- Python 3.11 or newer
- Rust/Cargo for the required native extension (source installs)
- Docker Desktop or another PostgreSQL 16+ server
- Git

## Install from source

```powershell
git clone https://github.com/HumanDotPy/ThothTerminal.git
Set-Location ThothTerminal
Copy-Item .env.example .env
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[desktop]"
pip install "maturin>=1.8,<2"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\build-native-html.ps1 -Install
$dbPassword = [Guid]::NewGuid().ToString("N")
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\start-local-postgres.ps1 -Password $dbPassword
$env:THOTH_DATABASE_URL = "postgresql+psycopg://thoth:${dbPassword}@127.0.0.1:55432/thoth"
thoth init-db
python scripts/maintain_catalog_indexes.py install
thoth desktop
```

The desktop app opens the same local interface served at
`http://127.0.0.1:8750`.

Build the native extension before starting Thoth. For Linux build commands and
native verification, see [native build and verification](native_html.md).

Save your database URL in your local `.env`, replacing the example URL. Keep
this file private. `init-db` creates the schema; connect sources through guided
setup when you choose. The release includes no database, user profile,
documents, notes, research reports, broker state, or authenticated browser state.

The source archive includes the Rust crate and build scripts. It is not a
prebuilt installer. To build a Windows desktop bundle after native installation,
run `scripts/build-desktop.ps1`. PostgreSQL remains a separate prerequisite.

## Connect your first sources

Open **Sources → Guided setup**. Choose a small credential-free pack:

- **AI research** — public arXiv AI and machine-learning feeds.
- **AI industry** — public reporting and official company news.
- **Semiconductor filings** — official NVIDIA, AMD, and Intel filing feeds.

Thoth adds each pack idempotently and queues an immediate connection check.
The advanced editor remains available for custom RSS, web lists, filings,
browser-backed sources, Google sources, and data APIs.

## Privacy defaults

- The app binds to `127.0.0.1`.
- `.env`, databases, browser state, downloaded objects, logs, and generated
  output are ignored by Git.
- Broker access is read-only and disabled by default.
- Public reports require an explicit publish step.
- Newsletter signup is disabled until SMTP and a signing secret are configured.

Before remote access or newsletter launch, read [SECURITY.md](../SECURITY.md).

## Release validation

The maintainer exports with `scripts/build-public-release.ps1`. The checked-in
`scripts/public-release-policy.json` lists each approved source file and pins
the three public icon assets by SHA-256. Only selected generic release and
security tests are included. Research fixtures, operator documentation,
profiles, runtime data, and experimental native kernels are excluded. New
files require an explicit policy review.

The exporter rejects links, unknown binary payloads, known credential formats,
personal installation paths, private network endpoints, and operator prompt
markers. Its receipt hashes every exported file. These checks supplement human
review; they do not prove that arbitrary source text contains no private facts.

Local preview builds require `-AllowDirty` and an explicit `-ReviewedPath` entry
for every included changed or untracked file. A dirty preview is recorded in
the manifest and must not be published as an official release. Export committed,
reviewed source for publication.

For a shared worktree containing unrelated edits, `-CommittedUnreviewed` reads
unreviewed changed files from `HEAD` and includes worktree bytes only for
explicitly reviewed paths. It still scans every resulting byte and records
this mixed snapshot in the preview receipt.
