# CallPilot 开发约定（agent 必读）

项目级约定，全局 `~/.claude/CLAUDE.md` 之上的补充；冲突时以本文件为准。

## 研发流程

- **批次 issue**：每个开发批次开一个 GitHub issue（任务清单+验收标准），不是每个小需求一个。commit message 尾部 `Refs #<n>`；批次完成时 `Closes #<n>`。
- **分工**：codex 编码+测试（不 commit），Claude 写规格+独立验收（自跑测试、真机验证）+管理进度+commit。派工走 headless（codex exec 后台+日志），不占用用户的 tmux 窗口。
- **决策预算前置**：批次开始时把可预见的决策一次性问完；途中有明显推荐项的决策（UI 形态/文案/命名/文件组织）直接选推荐项、交付时报备；花钱/不可逆/体验标定/需求方向类即时确认。
- **push 授权**：本仓库验收通过后可直接 push（每次 push 前必跑敏感扫描：真实姓名/sk-*/DASHSCOPE_API_KEY=/11 位手机号，逐 pattern 看输出而非退出码）。
- **非平凡改动**（>20 行或碰通话链路）完成后、宣布 done 前，过一轮独立 review。

## 硬约束

- **真机外呼只拨 10000**（免费客服热线）；预设库里其他真实号码只作离线测试数据，绝不真机拨出。
- **非枚举**：对话理解/策略不写关键词表、话术清单、号码→类型映射；用场景描述+模型判断。
- **不写死本机信息**：真实姓名/号码只在本地 `.env` 与 gitignored 配置；测试用 李明/Alex/占位号。
- **绝不提交**：`.env`、`dist/`、`build/signing/`、证书私钥、`.codex_dialog.md` 等会话产物、`data/number_profiles.json`（本地预设）。
- **交付级验证**：push 后回读 `git log origin/main -1`；服务改动重启+健康检查；通话链路改动真机拨测。测试全绿≠交付完成。

## 质量门

```bash
.venv/bin/pytest -q && .venv/bin/ruff check . && .venv/bin/mypy
```
三件套全绿是 commit 前置条件。`.env.example` 与 `config.py` 注册表有防脱节测试，改配置两边同步。
