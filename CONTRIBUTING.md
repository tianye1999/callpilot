# Contributing to CallPilot / 贡献指南

> **v0.1 Developer Preview.** The single most valuable contribution right now is a
> **hardware reproduction report** from anyone running a Quectel EC20/EG25. See
> [Hardware contributors](#hardware-contributors) below and open a
> [Hardware reproduction report](../../issues/new?template=hardware-report.yml).
>
> **v0.1 开发者预览版。** 现阶段最有价值的贡献是**同型号硬件的复现报告**——参见下方
> [硬件贡献者](#硬件贡献者)，并开一个
> [硬件复现报告](../../issues/new?template=hardware-report.yml) issue。

---

## English

Thanks for helping build CallPilot! This project bridges a Quectel EC20/EG25 4G
modem to a cloud realtime voice model. Contributions of all kinds are welcome —
code, docs, and especially **hardware reproduction feedback**.

### Development setup

Requires **Python 3.12+** and a working **`ffmpeg`** on your `PATH`
(`ffmpeg -version` should print something). Hardware is **not** required to build
or run the tests.

```bash
git clone https://github.com/tianye1999/callpilot.git callpilot
cd callpilot
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"    # editable install + dev extras (pytest)
cp .env.example .env                 # then edit .env with your own values
```

Use the **`.venv/bin/`** interpreter and tools directly (e.g. `.venv/bin/python`,
`.venv/bin/pytest`) rather than relying on an activated shell — this is how the
project is developed and documented.

Running the full service additionally needs an EC20/EG25 modem and a DashScope
API key; see the [README](README.md) Quick start. You do **not** need any of that
to contribute code or run tests.

### Running the tests

```bash
.venv/bin/pytest
```

- **No hardware, no network, no API key needed.** The suite (160 tests today)
  runs fully offline.
- Tests use in-memory **fakes** in `tests/fakes/` that are duck-typed to the real
  components: `FakeModem` (mimics `Eg25Modem`, records AT calls and lets tests
  trigger `RING`/hangup/SMS), `FakeAudioBridge` (in-memory audio loopback,
  interface-compatible with `ModemAudioBridge`), and `FakeAgent` (a scripted
  `VoiceAgent`). Wire these into your tests instead of touching real hardware or
  cloud APIs.
- `tests/conftest.py` auto-isolates side effects: it points `CALL_LOG_DIR` at a
  temp dir and sets `SUMMARY_ENABLED=false` so tests never write to your real
  recordings dir or hit the network. A test that needs different behavior can
  `monkeypatch.setenv` / `delenv` — test-level env wins over the fixture.
- Please run `.venv/bin/pytest` and get a green suite **before** opening a PR.

### Code style

Match the existing code — no separate linter/formatter is enforced yet, so
consistency is by convention:

- **Chinese docstrings and inline comments.** Module/class/function docstrings and
  comments are written in Chinese (see any file under `src/agentcall/`). Keep new
  code consistent; user-facing strings that reach the caller/UI are bilingual
  where the surrounding code already is.
- **`from __future__ import annotations`** at the top of every module (right after
  the module docstring), so annotations stay as strings and modern type syntax
  (`str | None`, `list[tuple[...]]`) works uniformly.
- **Relative imports within the package** (e.g. `from .base import VoiceAgent`,
  `from .doubao_agent import DoubaoVoiceAgent`). Import `agentcall.*` absolutely
  only from outside the package (tests, entrypoints).
- Type-annotate public functions and dataclass fields; prefer small, testable
  modules that can be driven by the fakes.
- **Never block the audio path.** `send_audio` and the audio bridge run on the
  realtime voice loop — keep them non-blocking (no sync I/O, no long sleeps).

### Commit & PR conventions

- **Branch** off `main`; don't commit to `main` directly. Use a short descriptive
  branch name (e.g. `fix/second-call-silent`, `feat/dtmf-tool`).
- **Run the tests first.** `.venv/bin/pytest` must be green before you push.
- **Commit messages** are concise and describe the *what/why*; a leading roadmap
  task ID (e.g. `P0-1`) is welcome when it maps to `docs/roadmap.md`. Chinese or
  English are both fine — match the surrounding history.
- Keep PRs focused. In the description, say **what changed**, **why**, and
  **how you verified it** (test output, or — for hardware paths — real-device
  observations, since CI cannot exercise the modem).
- If your change touches a hardware/audio path that the test suite can't cover,
  say so explicitly and describe your manual verification.

### Hardware contributors

This is where v0.1 needs the most help. Only one modem/firmware/OS combo is
verified so far (`EC20CEFAGR08A03M4G` on macOS with `uac_ffmpeg` audio). If you
have an EC20/EG25:

- Please open a **[Hardware reproduction report](../../issues/new?template=hardware-report.yml)**
  (label `hardware-report`) with your modem model + firmware (`ATI` / `AT+CGMR`
  output), macOS version, Python version, USB interface mapping, audio mode, and
  which features worked vs. failed.
- Partial results are still valuable — "outbound works, SMS doesn't" tells us a
  lot. Attach redacted logs for anything that failed.
- Windows/Linux native-serial paths exist but are **unverified** — reports on
  those platforms are especially welcome.

### Code of conduct

Be respectful and constructive; assume good faith and help newcomers.

### Safety red lines

- **Never commit secrets.** `.env` is git-ignored — keep your `DASHSCOPE_API_KEY`
  and other credentials there, never in code, tests, issues, or PRs. Scrub keys,
  phone numbers, and SIM numbers from any logs you paste.
- **Outbound-calling compliance is on you.** When testing outbound/batch dialing,
  use the dial whitelist and dial **your own numbers**. Respect anti-harassment /
  telemarketing law and recording-consent law in your jurisdiction. CallPilot is
  **not** for emergency calls.
- Don't add code that auto-dials third parties, exfiltrates call data, or weakens
  the local-only storage of recordings.

By contributing you agree your contributions are licensed under
[Apache-2.0](LICENSE).

---

## 中文

感谢参与 CallPilot！本项目把 Quectel EC20/EG25 4G 模组接到云端实时语音大模型。
欢迎各种形式的贡献——代码、文档，尤其是**硬件复现反馈**。

### 开发环境搭建

需要 **Python 3.12+** 以及 `PATH` 里可用的 **`ffmpeg`**（`ffmpeg -version` 能输出
即可）。构建与跑测试**不需要**硬件。

```bash
git clone https://github.com/tianye1999/callpilot.git callpilot
cd callpilot
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"    # 可编辑安装 + 开发依赖（pytest）
cp .env.example .env                 # 然后按需编辑 .env
```

请直接使用 **`.venv/bin/`** 下的解释器与工具（如 `.venv/bin/python`、
`.venv/bin/pytest`），而不依赖 shell 的 activate——这是本项目一贯的开发与文档约定。

跑完整服务还需要 EC20/EG25 模组和 DashScope API Key（见 [README](README.md) 快速
开始），但贡献代码或跑测试都**不需要**这些。

### 运行测试

```bash
.venv/bin/pytest
```

- **无需硬件、无需联网、无需 API Key。** 当前 160 个用例可完全离线运行。
- 测试使用 `tests/fakes/` 下的内存**假件**，与真实组件鸭子类型对齐：`FakeModem`
  （对齐 `Eg25Modem`，记录 AT 调用并可触发 `RING`/挂断/短信）、`FakeAudioBridge`
  （内存环回音频桥，接口与 `ModemAudioBridge` 对齐）、`FakeAgent`（脚本化的
  `VoiceAgent`）。写测试时接入这些假件，而不要碰真实硬件或云端 API。
- `tests/conftest.py` 会自动隔离副作用：把 `CALL_LOG_DIR` 指向临时目录，并设
  `SUMMARY_ENABLED=false`，使测试不写你的真实录音目录、也不触网。需要不同行为的
  测试可自行 `monkeypatch.setenv` / `delenv`——测试级优先于该 fixture。
- 开 PR **之前**请先跑 `.venv/bin/pytest` 并确保全绿。

### 代码风格

与现有代码保持一致——目前尚未强制统一的 linter/formatter，靠约定保持一致：

- **中文 docstring 与注释。** 模块/类/函数的 docstring 与注释用中文（参见
  `src/agentcall/` 下任意文件）。新代码保持一致；面向来电方/界面的用户可见文案，在
  周围代码已双语的地方也写成双语。
- 每个模块顶部（紧接模块 docstring）写 **`from __future__ import annotations`**，
  让注解保持字符串形式，现代类型写法（`str | None`、`list[tuple[...]]`）统一可用。
- **包内使用相对导入**（如 `from .base import VoiceAgent`、
  `from .doubao_agent import DoubaoVoiceAgent`）。只有从包外（测试、入口脚本）才用
  绝对导入 `agentcall.*`。
- 为公开函数与 dataclass 字段加类型注解；模块尽量小而可测，能被假件驱动。
- **绝不阻塞音频路径。** `send_audio` 与音频桥跑在实时语音循环上——保持非阻塞（不做
  同步 I/O、不长 sleep）。

### 提交与 PR 约定

- 从 `main` **切分支**，不要直接提交到 `main`。分支名简短达意（如
  `fix/second-call-silent`、`feat/dtmf-tool`）。
- **先跑测试。** 推送前 `.venv/bin/pytest` 必须全绿。
- **Commit 信息**简洁、说清 *做了什么/为什么*；映射到 `docs/roadmap.md` 的任务时，
  欢迎带上路线图任务 ID 前缀（如 `P0-1`）。中英文皆可，与历史保持一致。
- PR 保持聚焦。描述里写清**改了什么**、**为什么**、**如何验证**（测试输出；若涉及
  硬件路径，则给真机观察结果，因为 CI 无法驱动模组）。
- 若改动落在测试无法覆盖的硬件/音频路径上，请明确说明并描述你的人工验证方式。

### 硬件贡献者

这是 v0.1 最需要帮助的地方。目前只验证过一种组合（macOS + `uac_ffmpeg` 音频 +
`EC20CEFAGR08A03M4G` 固件）。如果你有 EC20/EG25：

- 请开一个 **[硬件复现报告](../../issues/new?template=hardware-report.yml)**
  （标签 `hardware-report`），附上模组型号+固件（`ATI` / `AT+CGMR` 输出）、macOS
  版本、Python 版本、USB interface 映射、音频模式，以及哪些功能成功/失败。
- 部分结果也很有价值——「外呼通、短信不通」就能提供大量信息。失败项请附脱敏日志。
- Windows/Linux 原生串口路径存在但**未验证**，尤其欢迎这些平台的报告。

### 行为准则

尊重、建设性沟通；默认善意，乐于帮助新人。

### 安全红线

- **绝不提交任何密钥。** `.env` 已 git 忽略——`DASHSCOPE_API_KEY` 等凭证只放这里，
  不进代码、测试、issue 或 PR。粘贴日志前请把 Key、手机号、SIM 卡号脱敏。
- **外呼合规由你负责。** 测试外呼/批量呼叫时用拨号白名单、拨**你自己的号码**；遵守
  当地反骚扰/营销法规与录音同意法规。CallPilot **不用于**紧急电话。
- 不要添加会自动拨打第三方、外泄通话数据、或削弱录音本地存储的代码。

贡献即表示你同意你的贡献以 [Apache-2.0](LICENSE) 授权。
