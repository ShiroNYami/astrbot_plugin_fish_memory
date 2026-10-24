# 鱼的记忆（astrbot_plugin_fish_memory）

给 AstrBot 里的 AI 伙伴加一份**会淡忘**的长期记忆：该记的记下来，想起来的时候自己会想起来，不重要的慢慢淡掉。

设计参考 DSH 的 [`dsh-fish-memory`](https://github.com/Yuuyuko-uu/dsh-fish-memory)（Yuuyuko-uu，MIT），
按 AstrBot 的插件形态重写：挂 `on_llm_request` 读、挂 `on_decorating_result` 写，数据全在本地 JSON。

- 版本：v0.2.0
- 兼容：AstrBot `>=4.16,<5`，平台 `aiocqhttp`（QQ）
- 许可：MIT

---

## 一、它做什么

| 功能 | 说明 |
|---|---|
| **记** | 每轮聊天结束后，用一个便宜的小模型提炼 0–2 条值得长期记住的事（稳定事实、约定、情绪事件、她的偏好） |
| **忘** | 每条有「鲜活度」，30 天减半。不删除，只是越来越难被想起来 |
| **想** | 下次对话按「证据命中 + 新鲜度 + 重要度 + 情绪」挑最多 6 条、≤800 字注入上下文 |
| **关系卡** | 每个 QQ 号一张小卡：生人 / 半熟 / 熟人、聊过几次、上次聊到什么 |
| **进化** | `/进化` 从最近 30 天的记忆里找「性格变化」候选，给人看，不自动改人设 |
| **人生档案** | 每写一条记忆同时追加到 `journal/YYYY-MM.md`，只增不删，可导出成 markdown |

**工作记忆与人生档案是两套东西**：

| | `entries.json`（工作记忆） | `journal/*.md`（人生档案） |
|---|---|---|
| 会淡忘 | 会 | 不会 |
| 会被淘汰 | 会（满了按价值挤掉） | 不会，只追加 |
| 参与注入 | 会 | 不会 |
| 用途 | 让她「想起来」 | 导出、迁移、留档 |

---

## 二、安装条件

1. **AstrBot 4.16 以上**（用到的 hook：`on_llm_request` / `on_decorating_result`；`ProviderRequest` 的 `prompt` 字段）
2. 平台适配器 **aiocqhttp**（OneBot v11），能正常收发群聊/私聊
3. 一个可用的 **provider**（提炼要调一次模型；建议填便宜的 flash / lite，留空则用当前会话的模型）
4. 磁盘：每个会话两份 JSON + 按月一个 md，一年也就几 MB

不需要额外依赖，不用装数据库，不用改 AstrBot 本体。

## 三、安装

把整个目录放进 AstrBot 的插件目录：

```
/opt/qqbot/astrbot/data/plugins/astrbot_plugin_fish_memory/
```

然后重启或热重载：

```bash
docker compose restart astrbot
# 或 WebUI → 插件 → 重载
```

看到日志 `Plugin astrbot_plugin_fish_memory (v0.2.0) by byakuya` 就是加载成功。
首次加载会在 `data/config/` 生成 `astrbot_plugin_fish_memory_config.json`。

---

## 四、配置项

| 配置 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enable` | bool | true | 总开关，关掉既不写也不读 |
| `inject_enable` | bool | true | 只写不读时关它 |
| `write_enable` | bool | true | 关掉就不提炼，完全不花 token |
| `session_default_on` | bool | true | 新会话默认开；关则需 `/记忆开关` |
| `provider_id` | string | 空 | 提炼用哪个模型；留空用会话当前模型 |
| `min_msg_chars` | int | 8 | 短于此不提炼 |
| `extract_interval_seconds` | int | 60 | 同会话两次提炼最小间隔，**控 token 的主旋钮** |
| `hourly_quota` | int | 3 | 每小时最多写几条，0 = 不限 |
| `daily_quota` | int | 30 | 每天总上限 |
| `top_n` | int | 6 | 每次最多注入几条 |
| `max_inject_chars` | int | 800 | 注入字数上限（约 500 token） |
| `min_evidence` | float | 0.34 | 证据门槛，调高更少提，调低更容易想起来 |
| `half_life_days` | int | 30 | 半衰期 |
| `max_entries` | int | 800 | 每会话工作记忆上限，满了按价值淘汰 |
| `owner_qq` | string | — | 能用管理命令的 QQ，多个逗号分隔；群聊私聊都只认这个 |
| `allow_bot_admin` | bool | false | 是否放行 AstrBot 的 `admins_id`；默认不放行 |

---

## 五、记忆策略（规则）

### 5.1 写入：什么时候记

同时满足才触发一次提炼：

1. `enable`、`write_enable`、该会话开关都开
2. 是正常聊天，不是 `/` 命令
3. 用户消息 ≥ `min_msg_chars`（默认 8 字）
4. 不是纯情绪/寒暄（「哈哈哈」「6」「在吗」「?」这类直接跳过）
5. 距本会话上次提炼 ≥ `extract_interval_seconds`（默认 60 秒）
6. 本小时未满 `hourly_quota`、本日未满 `daily_quota`

提炼由小模型完成，**它自己判断值不值得记**：

- 记：稳定事实、约定和计划、情绪事件、她自己的偏好与经历
- 不记：客套、重复、临时状态（在吃饭/在路上）、她自己的回复、密码/token/身份证/银行卡/门牌

写入前还有两道过滤：`importance ≤ 1` 且没有 `key` 的直接丢；和现有条目文本完全相同的跳过。

### 5.2 召回：什么时候想起来

每次 LLM 请求前，用当前消息做一次本地检索（不调模型、零 token）：

```
score = 0.45 × 证据 + 0.25 × 新鲜度 + 0.20 × 重要度 + 0.10 × 情绪
新鲜度 = 0.5 ^ (天数 / half_life_days)
```

- 证据 = 查询词与记忆文本/关键词（`terms`）/`key` 的重合率；命中 `terms` 直接抬到 0.55 以上
- 低于 `min_evidence` 的记忆不注入（`pinned` 除外）
- 取分数最高的 `top_n` 条，总字数不超过 `max_inject_chars`
- 被注入一次，`hits + 1`，以后更不容易被淘汰

### 5.3 淘汰：满了留谁

工作记忆上限 `max_entries`（默认 800）：

```
价值 = 0.6 × (重要度/5) + 0.3 × 新鲜度(30天) + 0.1 × min(1, hits/3)
```

- **不参与淘汰**：`pinned`、7 天内写入的新条目
- 有「已被取代」的死条目时，先清死条目
- 否则新条目价值必须比最弱的一条高 `0.02` 才顶替，否则这条不写
- 同 `key`（`约定.论文`、`她.体重`）只留最新，旧的标 `superseded`，不再被想起但内容仍在

### 5.4 衰减与删除

- 衰减只在读取时计算，没有定时任务；不删除任何内容
- 只有 `/删记忆`、`/删记忆 全部` 会真的删
- 淘汰只影响 `entries.json`，`journal` 不受影响

### 5.5 隐私

- 提炼阶段正则过滤密码/token/身份证/银行卡/门牌，**挡不住**「我住 XX 小区」这类，公共群建议关闭
- 所有数据是本地明文 JSON，只在服务器上，不上传任何第三方

---

## 六、命令与权限

**只认 `owner_qq` 里的号**，不管在群聊还是私聊。别人在群里发 `/删记忆 3`、`/导出记忆`，只会收到一句「这条只认主人哦」，不会执行，也不会产生文件。

- AstrBot 的 `admins_id`（机器人管理员）**默认不放行**；确实需要他们也能管，把 `allow_bot_admin` 打开
- 群管理、群主身份**没有**任何特权，插件只看 QQ 号
- 命令靠 `/` 前缀触发，和唤醒词一致；不需要 @ 机器人

```
/记忆簿           看这个会话记住了什么（最近 20 条）
/记忆全书         所有会话的条数统计 + 全局最近 5 条
/删记忆 3         删第 3 条；/删记忆 全部 清空
/记忆开关         开关当前会话
/关系卡           看关系卡；/关系卡 <QQ号> 看某个人
/进化             从最近 30 天记忆里找性格变化候选
/导出记忆         导出人生档案 markdown
```

---

## 七、数据与导出

```
data/plugin_data/fish_memory/
├── state.json                 会话开关、每日配额
├── journal/2026-10.md         人生档案（只追加）
├── export/life-*.md           导出结果
└── <hash>/
    ├── meta.json              原始会话标识
    ├── entries.json           工作记忆
    └── relations.json         关系卡
```

`<hash>` 是 `unified_msg_origin` 的 md5 前 12 位，**一个群 / 一个私聊一个目录，互不相通**。

### 从服务器导到 Windows

不经过 QQ，直接在服务器上生成再拉下来：

```powershell
# 1) 容器里生成（不需要 bot 在线处理消息）
ssh root@<服务器> "docker exec astrbot python /AstrBot/data/plugins/astrbot_plugin_fish_memory/cli_export.py"

# 2) 拉回本地（注意容器路径 /AstrBot/data 在宿主机是 /opt/qqbot/astrbot/data）
scp root@<服务器>:/opt/qqbot/astrbot/data/plugin_data/fish_memory/export/life-*.md D:\AI\人生档案\
```

也可以直接在 QQ 里 `/导出记忆`，效果一样。

导出的 markdown 按月份分节、末尾附关系卡，是纯文本，以后想搬去 `dsh-fish-memory`，
它支持「从旧档案导入」直接吃 `.md`。

---

## 八、已知限制

- 记忆按会话隔离：群里的记忆不会出现在私聊；关系卡的熟度也按会话分别计算
- 提炼质量取决于小模型，会写偏；建议第一周每天看 `/记忆全书`，错的删掉
- 注入多了会让模型「复读」记忆；压不住就把 `top_n` 降到 3–4、`min_evidence` 提到 0.45
- 群聊共用一份记忆，人多嘴杂时会张冠李戴，建议只在私聊和小群开
- **群聊里任何人都能「喂」记忆**：他说的稳定事实会被提炼进这个群的记忆，之后也可能被注入到别人的对话里。介意的话把大群关掉，或者只在小群开
- B 站评论区**不读**这份记忆（`BiliSurfing` 未接）；要接的话从 `entries.json` 读，不要跨插件 import
- AstrBot 大版本升级（4.x → 5.x）需要回归测试 hook 签名

## 九、变更记录

见 [CHANGELOG.md](CHANGELOG.md)。每次提交涉及行为变化时同步更新它和 `metadata.yaml` 的版本号。
