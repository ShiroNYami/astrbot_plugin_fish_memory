"""鱼的记忆 · 提示词与解析。

提炼、注入、进化三段文案都在这里，方便单独调，不用翻主逻辑。
"""

from __future__ import annotations

import json
import re

from .store import LEVEL_TEXT, fmt_day

EXTRACT_SYSTEM = (
    "你是记忆提炼器，负责从一轮聊天里挑出值得长期记住的事。"
    "只输出合法的 JSON 数组，不要解释，不要代码块以外的任何文字。"
)

EXTRACT_RULES = """从下面这轮聊天里提炼 0-2 条值得长期记住的事。

记什么：
- 对方的稳定事实：工作、住哪、家里人、喜好、雷点、身体情况
- 约定和计划：约好做什么、deadline、要买的东西
- 情绪事件：为什么高兴、为什么烦（一句话说清起因）
- 她自己说过的事：她的偏好、她的经历、她的看法

不记什么：
- 客套、寒暄、重复的话
- 临时状态（在吃饭、在路上、在开会）
- 她自己的回复内容（那是她说的话，不是记忆）
- 任何密码、token、身份证、住址门牌这类敏感信息

写法：
- 每条一句话，20-40 字，第一人称属于她（"她说过……"，"她不喜欢……"）
- 有明确时间就写进句子（"上周三"、"10 月初"）
- key 只在「会变的事」上填，格式「对象.方面」，例如 约定.论文、她.体重、她.宠物；拿不准就留空字符串
- terms：2-4 个以后可能被问到的关键词，用来做检索，例如 ["论文","deadline"]
- importance：1 随便，3 一般，5 很重要
- emotion：0 平静，1 情绪很重
- who：她 / 对方 / 共同

输出（没有值得记的就输出 []）：
[{"text":"她说过下周要交论文，一直拖着没写","key":"约定.论文","terms":["论文","截止"],"importance":4,"emotion":0.3,"who":"对方"}]
"""

EVOLVE_SYSTEM = "你是性格观察员。只输出合法 JSON 数组，不解释。"

EVOLVE_RULES = """下面是这个角色最近一段时间记住的事。请从中看出她「性格上正在发生的变化」，
给出不超过 3 条候选，每条是一句可以直接写进人设的话，例如：「最近她开始主动问对方吃饭没有」。

要求：
- 只写有证据的，凭感觉的不要
- 每条不超过 30 字，用「她」做主语
- 不要写「她变得更像人了」这种空话
- 没有明显变化就输出 []

输出：
[{"habit":"最近她开始主动问对方吃饭没有","reason":"三次提到吃饭"}]
"""


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def build_extract_prompt(user_text: str, bot_text: str, sender_name: str = "") -> str:
    who = sender_name or "对方"
    return (
        f"{EXTRACT_RULES}\n"
        f"聊天：\n{who}：{_clip(user_text, 500)}\n"
        f"她：{_clip(bot_text, 300)}\n"
    )


def parse_extracted(raw: str) -> list[dict]:
    """把模型输出解析成条目列表；坏数据一律丢掉，不让它污染记忆库。"""
    text = (raw or "").strip()
    if not text:
        return []
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    items: list[dict] = []
    for raw_item in data[:3]:
        if not isinstance(raw_item, dict):
            continue
        content = _clip(raw_item.get("text") or "", 160)
        if len(content) < 6:
            continue
        if re.search(r"(密码|token|身份证|银行卡|门牌)", content, re.I):
            continue
        try:
            importance = int(raw_item.get("importance") or 3)
        except (TypeError, ValueError):
            importance = 3
        try:
            emotion = float(raw_item.get("emotion") or 0)
        except (TypeError, ValueError):
            emotion = 0.0
        items.append(
            {
                "text": content,
                "key": _clip(raw_item.get("key") or "", 40),
                "terms": [
                    _clip(t, 12)
                    for t in (raw_item.get("terms") or [])
                    if isinstance(t, str) and len(t.strip()) >= 1
                ][:6],
                "importance": max(1, min(5, importance)),
                "emotion": max(0.0, min(1.0, emotion)),
                "who": _clip(raw_item.get("who") or "她", 8),
            }
        )
    return items


def build_inject_block(picked: list, relation: dict | None, sender_name: str = "") -> str:
    """拼注入块。刻意不用列表标题式的结构，避免模型学着列清单。"""
    if not picked:
        return ""
    lines = ["【你记得的】"]
    for _total, entry, _detail in picked:
        who = str(entry.get("src_name") or "").strip()
        prefix = f"{who}：" if who else ""
        lines.append(f"- {fmt_day(entry.get('created_at') or 0)} {prefix}{entry.get('text')}")
    lines.append(
        "（这些是你自己的记忆，冒号前是这句话是谁说的。接得上就提一句，接不上别硬提，"
        "别逐条复述，更别把别人的事安到眼前这个人头上。）"
    )
    if relation:
        level = LEVEL_TEXT.get(int(relation.get("level") or 0), "生人")
        name = sender_name or relation.get("name") or ""
        head = f"【眼前这个人】{name + '，' if name else ''}{level}"
        if relation.get("count"):
            head += f"（聊过 {relation['count']} 次）"
        if relation.get("topic"):
            head += f"，上次聊到：{relation['topic']}"
        lines.append(head)
    block = "\n".join(lines)
    return block[:1200]


def build_evolve_prompt(texts: list[str]) -> str:
    body = "\n".join(f"- {_clip(t, 60)}" for t in texts[:60])
    return f"{EVOLVE_RULES}\n记忆：\n{body}\n"


def parse_evolve(raw: str) -> list[tuple[str, str]]:
    text = (raw or "").strip()
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except Exception:
        return []
    out: list[tuple[str, str]] = []
    if isinstance(data, list):
        for item in data[:3]:
            if isinstance(item, dict):
                habit = _clip(item.get("habit") or "", 40)
                if habit:
                    out.append((habit, _clip(item.get("reason") or "", 40)))
    return out
