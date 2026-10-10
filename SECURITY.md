# Security

## Supported versions

Security fixes are applied to the latest public release. Thoth is local-first
and binds to loopback by default. Do not expose the operator UI directly to the
internet.

## Reporting a vulnerability

Please use [GitHub's private vulnerability reporting](https://github.com/HumanDotPy/ThothTerminal/security/advisories/new).
Include the affected version, impact, and a
minimal reproduction. Do not include API keys, private documents, subscriber
addresses, or other sensitive data.

We will acknowledge receipt, investigate, and coordinate disclosure before
publishing details.

## Deployment baseline

- Keep `THOTH_HOST=127.0.0.1` for local installations.
- Use authentication and a trusted reverse proxy for any remote access.
- Never commit `.env`, database files, browser profiles, cookies, or object
  storage.
- Public research routes expose only explicitly published report versions.
- Newsletter signup uses double opt-in and requires a signing secret of at
  least 32 characters.
- Trading mutations are intentionally refused.
