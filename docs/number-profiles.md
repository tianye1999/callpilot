# 预调教任务库（number profiles）

「号码 + 任务 → 精调提示词」的预设库，是本项目**核心积累的高价值资料**：给常用号码
预先写好针对性的通话 scenario，让 AI 打这类电话时开场就进入状态，而不是每次现场发挥。

> 定位说明：项目硬原则是对话理解与交互管控**不做关键词/话术枚举**（见
> [CLAUDE.md](../CLAUDE.md)）。任务库是全项目唯一的刻意例外——它是「预置精调」，
> 相当于给特定场景配好的 system prompt，不是对话逻辑枚举。鼓励持续补充公共热线预设。

## 文件与加载

| 文件 | 作用 |
|------|------|
| [`data/number_profiles.example.json`](../data/number_profiles.example.json) | 入库的种子（只放公共服务号码），首次启动播种到本地库 |
| `data/number_profiles.json` | 本地库（gitignored），真实使用与个人预设都在这里 |

日常增删改用 Web 控制台的**任务库（Presets）**页面（新建 / 编辑 / 复制 / 停用 / 删除）；
熟悉结构后也可以直接编辑 JSON。

相关配置（以 [`.env.example`](../.env.example) 注册表为准）：`NUMBER_PROFILES_ENABLED`、
`NUMBER_PROFILES_FILE`、`PROMPT_GEN_ENABLED`、`PROMPT_GEN_MODEL`。

## 结构

顶层为 `{"profiles": [...]}`，每条预设的字段：

| 字段 | 必填 | 说明 |
|------|------|------|
| `id` | ✅ | 唯一标识，蛇形命名（如 `china_telecom_data`） |
| `number` | ✅ | 目标号码（字符串） |
| `task` | ✅ | 任务短语，用于「号码+任务」精确匹配 |
| `label` | ✅ | UI 里显示的名称 |
| `scenario` | ✅ | 通话 scenario（本体，见下节编写要点） |
| `opening` | — | 开场白（接通后第一句） |
| `opening_mode` | — | `say`（默认）或 `wait`（先听完 IVR） |
| `dtmf_spoken_followup` | — | 是否补发已口述但未调用工具的按键，默认 `false` |
| `result_verification` | — | `none`（默认）或 `carrier_sms`（官方短信校验） |

`task` / `label` / `scenario` / `opening` 均支持**普通字符串**或 **`{zh, en}` 对象**：
`scenario` / `opening` 跟随通话语言（`AGENT_LANGUAGE`），`label` / `task` 跟随 UI 语言，
缺失的语言自动回退。

## 匹配顺序

拨号时按以下顺序取 scenario：

1. **号码 + 任务** 精确匹配；
2. 仅**号码**匹配；
3. 都没有 → **动态生成**（`PROMPT_GEN_ENABLED` 开启时由模型现场生成提示词）。

选择预设拨号时会自动填入号码与主题，主题框仍可编辑本通电话的具体子事项。

## scenario 编写要点

从种子库沉淀出的模式，新写预设建议逐条对照：

1. **角色钉死**：明确「你是主叫、替机主办事的一方」——不是客服、不代表对方机构、
   **绝不冒充机主本人**。
2. **开场直给需求**：不自我介绍、不寒暄，第一句就说要办什么事（配合 `opening`）。
3. **菜单应对**：语音菜单说短词（如「流量」「人工」）；按键菜单**必须调用 `send_dtmf`
   工具真正按键**，不能只嘴上说要按几。
4. **反编造纪律**：对方未明确给出数据（金额、日期、结果）前，绝不说「已查到」、绝不编数字；
   没拿到就说还在等。拿到后口头确认一句。
5. **安全边界**（银行等敏感场景必写）：绝不索要或读出完整卡号、密码、短信验证码、CVV。
6. **双语对齐**：提供 `{zh, en}` 时两个语种语义保持一致。

查话费、流量或账单等数字结果时，可把 `result_verification` 设为
`carrier_sms`。系统只关联本通开始后、由当前 SIM 对应公共客服号发来的新短信；有匹配短信时，
摘要结论只采用短信正文并标为「已核实」，不保留模型听写的金额。等待窗口内没有匹配短信时，
摘要会明确标为「待核实」，听写数字只作参考。等待时长由
`SMS_VERIFICATION_WAIT_SECONDS` 控制。

## 最小模板

```json
{
  "id": "my_hotline_task",
  "number": "12345",
  "task": {"zh": "咨询某事项", "en": "ask about something"},
  "label": {"zh": "某热线·咨询", "en": "Some hotline · Enquiry"},
  "scenario": {
    "zh": "你在替机主拨打某某热线12345，咨询……。你是主叫、替机主办事的一方，不是客服、不冒充机主本人。开场直接说需求、不自我介绍；遇语音菜单说短词，遇按键菜单调用 send_dtmf 真正按键；对方未明确给出结果前不编造、不声称已办好。拿到结果口头确认一句。",
    "en": "You are calling hotline 12345 on the owner's behalf to ask about ... . You are the caller — not an agent, never impersonate the owner. State the need directly, no self-introduction; short keywords to voice menus, call send_dtmf for keypad menus; never fabricate results before they are given. Confirm in one sentence once you get it."
  },
  "opening": {"zh": "你好，我想咨询一件事。", "en": "Hi, I have a question."}
}
```

## 贡献约定

- 提交进 `example.json` 的种子**只放公共服务号码**（客服热线、政务热线等）；
- 私人号码、真实姓名**只留在本地** `data/number_profiles.json`，绝不入库；
- 改动后跑项目质量门（pytest / ruff / mypy）再提交。
