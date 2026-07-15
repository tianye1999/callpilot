# CallPilot 开发约定（贡献者 / AI agent 必读）

面向所有贡献者(含 AI coding agent)的项目级约定;个人/本机的开发编排(agent 分工、
push 授权等)放在 gitignored 的 `CLAUDE.local.md`,不入库。

## 硬约束

- **真机外呼只拨本机 SIM 所属运营商的免费客服热线**（移动 10086 / 电信 10000 / 联通 10010，
  按 SIM 的 IMSI(MNC) 或号段识别；拨号前核对屏幕号码）；跨运营商拨他家客服号可能收费——
  拨号只认「本卡运营商的免费客服号」。其他真实号码只作离线测试数据，绝不真机拨出。
  （2026-07-12：本机演示卡已从电信换为移动，当前免费客服号 = 10086，不再是 10000。）
- **对话 / 交互一律非枚举（硬原则）**：对话理解、交互管控、应答策略**不写**关键词表、
  话术清单、号码→类型映射——枚举无法全面、通用地覆盖真实对话，只会越补越漏；一律用
  「场景描述 + 模型判断」。
  - **全项目唯一刻意的例外**：`data/number_profiles.json`（拨打目的 / 预调教任务库，
    “号码 + 任务 → 精调提示词”）。它是**项目核心积累的高价值资料**，鼓励所有人持续
    补充完善（结构见 `data/number_profiles.example.json`，真实号码留本地）。这是“预置
    精调”，不是“对话逻辑枚举”，不违反上一条。
- **不写死本机信息**：真实姓名 / 号码只在本地 `.env` 与 gitignored 配置；测试用 李明 / Alex / 占位号。
- **绝不提交**：`.env`、`dist/`、`build/signing/`、证书私钥、`.codex_dialog.md` 等会话产物、
  `data/number_profiles.json`（本地真实预设）。
- **交付级验证**：push 后回读 `git log origin/main -1`；服务改动重启 + 健康检查；通话链路
  改动真机拨测。测试全绿 ≠ 交付完成。

## 质量门

```bash
.venv/bin/pytest -q && .venv/bin/ruff check . && .venv/bin/mypy
```
三件套全绿是 commit 前置条件。`.env.example` 与 `config.py` 注册表有防脱节测试，改配置两边同步。

三件套只覆盖本机环境；push 后要回看 GitHub Actions 三平台矩阵，**发布 Release /
关闭批次 issue 的前置条件是 main 最新 CI 全绿**（教训：曾本地全绿而 CI 连红 6 轮
无人察觉，红着发布了 v0.4.2）。Windows 腿可本地预检：`.venv/bin/mypy --platform win32`。

## 流程

- **批次 issue**：每个开发批次开一个 GitHub issue（任务清单 + 验收标准），不是每个小需求
  一个。commit message 尾部 `Refs #<n>`；批次完成时 `Closes #<n>`。
- **非平凡改动**（>20 行或碰通话链路）完成后、宣布 done 前，过一轮独立 review。
- 架构与模块职责见 [`docs/architecture.md`](docs/architecture.md)。
