# Security Policy

## Supported versions

| Version | Supported          |
|---------|--------------------|
| 0.2.x   | ✅ current release |
| < 0.2   | ❌                 |

Pre-1.0, only the latest minor release receives fixes.

## Reporting a vulnerability

Please report vulnerabilities **privately** — do not open a public issue.

- Preferred: [GitHub Security Advisories](https://github.com/tianye1999/callpilot/security/advisories/new)
  ("Report a vulnerability" on the repository's Security tab).
- Alternatively, email the maintainer at the address listed on their GitHub
  profile. <!-- TODO: replace with a dedicated security contact address -->

Include what you can: affected version or commit, platform, reproduction
steps, and impact. This is a volunteer-run project — we aim to acknowledge
reports within 7 days and will credit reporters in the fix's release notes
unless you ask otherwise. There is no bug bounty.

## Scope

In scope:

- The CallPilot source tree (`src/agentcall`, `app.py`, `desktop_app.py`),
  helper scripts, and packaging.
- The local web dashboard and its HTTP/WebSocket API.
- How CallPilot handles credentials stored in your `.env`.

Out of scope:

- Leakage or misuse of **your own API keys** (DashScope, OpenAI, Doubao).
  Keys live in your local `.env`; keeping that file private is your
  responsibility.
- Carrier- and network-side behavior (SIM provisioning, VoLTE, IVR systems,
  SMS delivery).
- Attacks requiring physical access to your machine or modem, or an already
  compromised host.
- Vulnerabilities purely in third-party dependencies — report those upstream
  (though we do want to know if CallPilot's usage makes one exploitable).
