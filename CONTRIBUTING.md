# Contributing to CallPilot

Thanks for your interest! CallPilot is an open-source AI phone agent that bridges a
real 4G modem to a realtime voice model. It grows through real-world hardware reports
and code — here's how to help.

## Ways to contribute

### 🔌 Hardware reproduction reports (most wanted right now)

CallPilot is verified on a **Quectel EC20 on macOS**. If you have an EC20/EG25 — especially
on **Windows** or **Linux**, where code paths exist but aren't verified on real hardware —
please [open an issue](../../issues/new) with:

- modem model + firmware (`ATI` output)
- OS and version
- what worked / what didn't (dial, auto-answer, SMS send/receive, DTMF, both audio directions)

### 🐛 Bugs & 💡 features

Open an issue. For bugs, include repro steps, logs, and your platform. New here? Start
with a [good first issue](../../labels/good%20first%20issue).

### 🧑‍💻 Code

```bash
git clone https://github.com/tianye1999/callpilot.git && cd callpilot
bash scripts/setup.sh          # checks Python 3.12+/ffmpeg, creates .venv + .env
.venv/bin/pytest               # tests run WITHOUT hardware (fake modem/bridge/agent)
```

Quality gate before opening a PR (all three must pass):

```bash
.venv/bin/pytest -q && .venv/bin/ruff check . && .venv/bin/mypy
```

- Architecture & where-to-change-what: [`docs/architecture.md`](docs/architecture.md)
- Learn/test a single modem primitive (raw AT, dial, SMS, DTMF) in isolation: [`examples/modem/`](examples/modem/)

## Ground rules

- **Never commit secrets.** API keys and SMTP passwords live only in the git-ignored `.env`.
- **Real-machine dialing:** for testing, dial your own number or your carrier's free
  customer-service hotline. Respect anti-harassment, telemarketing, and call-recording
  laws in your jurisdiction — you are responsible for any consent the law requires.
- Keep PRs focused; describe what you changed and how you verified it.

## License

By contributing, you agree your contributions are licensed under [Apache-2.0](LICENSE).
