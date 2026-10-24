"""人生档案导出：把 journal/ 按月拼成一份 markdown。

journal 是只追加的，和「工作记忆」分开：
工作记忆会淡忘、会被修剪，journal 不会。
"""

from __future__ import annotations

import os
import time

from .store import LEVEL_TEXT, MemoryStore


def session_label(umo: str) -> str:
    if not umo:
        return "未知会话"
    lowered = umo.lower()
    if "group" in lowered:
        kind = "群"
    elif "friend" in lowered or "private" in lowered:
        kind = "私聊"
    else:
        kind = "会话"
    return f"{kind}·{umo.rsplit(':', 1)[-1]}"


def build_life_markdown(base_dir: str, now: float | None = None, title: str = "大肥鱼的人生档案") -> tuple[str, int]:
    store = MemoryStore(base_dir)
    now = time.time() if now is None else now
    lines: list[str] = [
        f"# {title}",
        "",
        f"导出时间：{time.strftime('%Y-%m-%d %H:%M', time.localtime(now))}",
        "",
    ]
    count = 0
    files = store.journal_files()

    if files:
        for path in files:
            month = os.path.basename(path)[: -len(".md")]
            try:
                with open(path, encoding="utf-8") as fp:
                    body = fp.read().strip()
            except Exception:
                continue
            if not body:
                continue
            lines.append(f"## {month}")
            lines.append(body)
            lines.append("")
            count += sum(1 for line in body.splitlines() if line.startswith("- "))
    else:
        rows: list[tuple[float, str, dict]] = []
        for bucket in store.buckets():
            label = session_label(bucket["umo"])
            for entry in bucket["entries"]:
                rows.append((float(entry.get("created_at") or 0), label, entry))
        rows.sort(key=lambda item: item[0])
        month = ""
        for ts, label, entry in rows:
            current = time.strftime("%Y-%m", time.localtime(ts))
            if current != month:
                month = current
                lines.append(f"## {month}")
            stamp = time.strftime("%m-%d %H:%M", time.localtime(ts))
            lines.append(f"- {stamp} {label}：{entry.get('text')}")
            count += 1
        lines.append("")

    relation_lines: list[str] = []
    for bucket in store.buckets():
        umo = bucket["umo"]
        if not umo:
            continue
        relations = store.load_relations(umo) or {}
        for card in relations.values():
            level = LEVEL_TEXT.get(int(card.get("level") or 0), "生人")
            name = card.get("name") or "某人"
            line = f"- {session_label(umo)}：{name}（{level}，{card.get('count') or 0} 次）"
            if card.get("topic"):
                line += f" 上次：{card['topic']}"
            relation_lines.append(line)

    if relation_lines:
        lines.append("## 关系卡")
        lines.extend(relation_lines)
        lines.append("")

    return "\n".join(lines).strip() + "\n", count
