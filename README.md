# 鱼的记忆 · QQ 聊天版

给 AstrBot 里的人格加一份**会淡忘**的长期记忆。设计参考 DSH 的 `dsh-fish-memory`，
但按 AstrBot 的插件形态重写：挂 `on_llm_request` 读、挂 `on_decorating_result` 写。

## 它做什么

- **写**：每轮聊天结束，用一个便宜模型把「值得记住的事」提炼成一句话存下（稳定事实、约定、情绪事件、她的偏好）。
- **忘**：每条有鲜活度，30 天减半。不删除，只是越来越难被想起来。
- **读**：下次对话按「证据命中 + 新鲜度 + 重要度 + 情绪」挑最多 6 条、≤800 字注入。
- **关系卡**：每个 QQ 号一张小卡（生人/半熟/熟人、聊过几次、上次聊到什么）。
- **进化**：`/进化` 从最近 30 天的记忆里找「性格变化」候选，给人看，不自动改人设。

## 装

把整个 `astrbot_plugin_fish_memory/` 目录放进：

```
/opt/qqbot/astrbot/data/plugins/
```

然后 WebUI → 插件 → 重载，或 `docker compose restart astrbot`。

## 配置

| 项 | 默认 | 说明 |
|---|---|---|
| enable | 开 | 总开关 |
| inject_enable | 开 | 只写不读时关它 |
| write_enable | 开 | 关掉不花提炼的 token |
| session_default_on | 开 | 新会话默认开 |
| provider_id | 空 | 提炼用哪个模型；留空用当前会话的 |
| min_msg_chars | 8 | 短于此不提炼 |
| daily_quota | 8 | 每会话每天最多记几条 |
| extract_interval_seconds | 60 | 同一会话两次提炼的最小间隔，控 token 的主要旋钮 |
| top_n | 6 | 每次最多注入几条 |
| max_inject_chars | 800 | 注入字数上限 |
| min_evidence | 0.34 | 证据门槛，调高更少提 |
| half_life_days | 30 | 半衰期 |
| max_entries | 400 | 每会话条数上限 |
| owner_qq | 929765409 | 能用管理命令的号 |

## 命令

```
/记忆簿          看这个会话记住了什么
/记忆全书        看所有会话的条数统计 + 最近 5 条
/删记忆 3        删第 3 条；/删记忆 全部 清空
/记忆开关        开关这个会话
/关系卡          看关系卡
/进化            看性格变化候选
/导出记忆        导出人生档案 markdown
```

只有 owner_qq 里的号能用。

## 工作记忆 vs 人生档案

| | 工作记忆 entries.json | 人生档案 journal/*.md |
|---|---|---|
| 会淡忘 | 会（30 天减半） | 不会 |
| 会被修剪 | 会（超过 400 条删最旧最不重要） | 不会，只追加 |
| 参与注入 | 会 | 不会 |
| 用途 | 让她「想起来」 | 给你导出、迁移、留档 |

每写一条记忆，同时往 `journal/YYYY-MM.md` 追加一行，所以**修剪只影响她当下能想起什么，不影响你手里的人生档案**。
`/导出记忆` 把 journal 按月份拼成一份 markdown，落在 `export/人生档案-<时间>.md`。

这份 markdown 是纯文本，以后想搬去 DSH 的 `dsh-fish-memory`，它可以「从旧档案导入」直接吃 `.md`。

## 数据

```
data/plugin_data/fish_memory/state.json              会话开关、每日配额
data/plugin_data/fish_memory/<hash>/entries.json     记忆条目
data/plugin_data/fish_memory/<hash>/relations.json   关系卡
```

`<hash>` 是 `unified_msg_origin` 的 md5 前 12 位，一个群/一个私聊一个目录。

## 注意

- **token**：注入 ≤800 字（约 500 token）/轮；提炼一次约 500-800 token，只在消息 ≥8 字时触发。
- **共享**：记忆按会话隔离。群里的记忆不会出现在私聊，反之亦然。想跨会话共享需要改 bucket 逻辑。
- **触发**：注入在每次 LLM 请求前；提炼在每次回复后，异步队列，不阻塞回复。
- **B 站**：想让评论区也用这份记忆，需要另外在 `bili_reader` 里读同一份 `entries.json`（默认没接）。
