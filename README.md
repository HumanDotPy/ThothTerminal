# Thoth Terminal

Thoth is a local-first research workspace. Collect sources, work with datasets,
connect evidence to notes and theses, and develop reports you can return to.
Desktop, web, and agent tools share the same research objects.

Built by [Agentic Systems Group](https://agenticsystemsgroup.com/).
The public project lives at [HumanDotPy/ThothTerminal](https://github.com/HumanDotPy/ThothTerminal).

## Final beta

**0.3.1-beta.1** is a source release for an independent installation. It includes
application code, the required Rust extension, and build instructions. It
contains no operator database, documents, research watchlists, private reports,
model credentials, or authenticated browser profiles.

Start with the [installation guide](docs/public_release_quickstart.md).
This beta requires some setup; it is not a prebuilt Windows installer.

## What it does

- Collect feeds, filings, PDFs, transcripts, web sources, and your own notes.
- Search documents and research objects together, with linked source context.
- Work with datasets, observations, companies, and supply chains.
- Keep notes, theses, questions, and reports in a persistent workspace.
- Give agents access through MCP and bounded local-agent workflows.
- Publish selected report versions separately from private working research.

Integrations are optional. Browser-backed sources need a browser installation
and, where applicable, your own account. External model services receive the
context you send them. Broker access is disabled by default.

## Install

Windows 11, Python 3.11+, Rust/Cargo, and PostgreSQL 16+ are required for the
documented desktop source installation. Docker Desktop can provide PostgreSQL.
See the [native build guide](docs/native_html.md) for Linux native builds.

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
```

Configure your PostgreSQL URL in `.env` as `THOTH_DATABASE_URL`, then run:

```powershell
thoth init-db
thoth desktop
```

The [quickstart](docs/public_release_quickstart.md) includes the local PostgreSQL
helper. Fresh initialization creates the schema and generic taxonomy; it does
not populate research watchlists or investment conclusions. Add sources through
**Sources → Guided setup**, or configure your own sources.

The local web interface is at `http://127.0.0.1:8750`. Before starting another
service on an existing installation, run `thoth runtime-status` and use the
healthy desktop-managed runtime when present.

## Architecture

PostgreSQL is the canonical live database. The required Rust extension handles
HTML extraction, search ranking, and institutional query planning. DuckDB serves
analytical dataset queries through a bounded cache. Python orchestrates these
engines and the shared desktop, web, and MCP services.

Performance-sensitive features and hot paths default to Rust; Python is
appropriate for orchestration around optimized native or external systems.
Evaluate real workflows across latency, CPU/GPU time, memory, I/O, token use,
cache reuse, and successful tool calls. Compare the same workload before and
after, preserve correctness, and remove superseded implementations.

## Useful commands

```powershell
thoth version
thoth runtime-status
thoth ingest sources --name "My source"
thoth ingest note --title "Working question" --text "What evidence would change this view?"
thoth web
```

`thoth desktop` owns its background services. Use the web command for a separate
headless installation. Do not run a second server against an active desktop.

To build a Windows desktop bundle after installing the native extension:

```powershell
.\scripts\build-desktop.ps1
```

PostgreSQL remains a separate prerequisite for the bundle.

## Privacy and contributions

Keep `.env`, databases, downloaded objects, browser state, logs, and generated
research out of Git. `.gitignore` helps prevent accidental additions; review
each commit and artifact before publication. See [SECURITY.md](SECURITY.md) for
deployment boundaries and [CONTRIBUTING.md](CONTRIBUTING.md) for contributions.

Use one canonical system of record and extend existing research objects rather
than introducing shadow databases. Keep idle work bounded, make failures
inspectable, and verify changes through the affected user or agent workflow.
