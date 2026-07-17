# ADR-004: App Store 审核使用可撤销的真实配对环境

## Status

Accepted for App Store review preparation in #102 (2026-07-17)

## Context

CallPilot 必须先与一台在线 Edge 配对才能展示线路、短信、通话记录和真实通话能力。
Apple 审核员无法在五分钟内协调 owner 现场生成普通配对码。把长期静态凭证写入 App、审核
备注或仓库会造成不可撤销的共享访问;只做本地假数据 demo 又不能让审核员验证真实服务。

## Decision

为一台与真实用户数据隔离的 reviewer Edge 增加 `app_review` 配对用途:

- 普通 `standard` 配对保持 60--600 秒和现有 wire payload;旧 Edge 不受影响。
- `app_review` 配对仅在 Worker secret `APP_REVIEW_PAIRING_ENABLED=true` 时创建和领取,
  最长七天、一次领取即失效、D1 只保存 code hash。
- 配对表显式保存 `purpose`;关闭开关后,所有尚未领取的 reviewer code 立即不可领取,
  对外仍返回通用 `INVALID_PAIRING`,不泄露开关或用途状态。
- reviewer 设备仍受现有每 Edge 五台设备上限、claim 限流、cookie 保护和设备撤销机制约束。
- App Review Notes 可提供少量独立的一次性 code 以容忍重装/重试;不得提供 Edge bearer、
  API key 或用户账号密码。明文 code 只在生成命令的终端输出一次,不写文件或日志。
- reviewer Edge 不接入 owner 的短信、录音或通话历史。若展示内容,只使用明确脱敏的测试数据。
  外呼策略在 Edge 侧限制为无资费风险的测试号码;审核结束后撤销 reviewer 设备并关闭开关。

这不是第二套认证系统。App 继续使用既有配对页面和设备 cookie;Cloud 只扩展配对会话的
用途与生命周期策略。

## Consequences

审核期间需要保持 reviewer Edge 在线,并在提交前验证配对、只读内容、外呼失败提示与撤销
恢复。七天 code 扩大了未领取凭证的暴露窗口,由一次性领取、hash 存储、功能开关、限流、
专用无真实数据 Edge 和审核后撤销共同约束。该机制仅解决审核访问,不替代后续的正式账号
恢复与凭证轮换工作(#56)。

References: #102, #100, #56, [cloud control plane](002-cloud-control-plane.md).
