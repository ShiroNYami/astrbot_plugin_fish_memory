"""鱼的记忆 · 给 QQ 聊天加一份会淡忘的长期记忆。

设计参考 dsh-fish-memory（Yuuyuko-uu），但没有照搬 DSH 插件形态：
这里挂在 AstrBot 的 on_llm_request / on_decorating_result 上，数据落在
data/plugin_data/fish_memory/，一个会话一个目录。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .export import build_life_markdown, session_label
from .extract import (
    EXTRACT_SYSTEM,
    EVOLVE_SYSTEM,
    build_evolve_prompt,
    build_extract_prompt,
    build_inject_block,
    parse_evolve,
    parse_extracted,
)
from .store import MemoryStore, fmt_day, select_memories

MARKER = "【你记得的】"
PLUGIN = None

# 明显不值得开一次模型调用的消息：「哈哈哈」「6」「在吗」「?」
TRIVIAL_RE = re.compile(r"^(哈+|6+|草+|乐|笑死|？+|\?+|在吗|嗯+|哦+|好+|行+|可以|对+|是+|ok|收到)+$", re.I)


@register(
    "astrbot_plugin_fish_memory",
    "byakuya",
    "给 QQ 聊天加一份会淡忘的长期记忆：该记的记下来，想起时自己会想起，不重要的慢慢淡掉。",
    "v0.3.0",
    "https://github.com/ShiroNYami/astrbot_plugin_fish_memory",
)
class FishMemoryPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self._base = os.path.join(get_astrbot_data_path(), "plugin_data", "fish_memory")
        os.makedirs(self._base, exist_ok=True)
        self.store = MemoryStore(self._base)
        self._state = self._load_state()
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=50)
        self._worker: asyncio.Task | None = None
        self._last_extract: dict[str, float] = {}
        self._last_store_save: dict[str, float] = {}
        global PLUGIN
        PLUGIN = self

    # ---------- 配置 ----------

    def _get(self, key: str, default):
        try:
            value = self.config.get(key, default)
        except Exception:  # noqa: BLE001
            return default
        return default if value is None else value

    def _get_str(self, key: str, default: str = "") -> str:
        return str(self._get(key, default)).strip()

    def _get_bool(self, key: str, default: bool) -> bool:
        value = self._get(key, default)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _get_int(self, key: str, default: int) -> int:
        try:
            return int(self._get(key, default))
        except (TypeError, ValueError):
            return default

    def _get_float(self, key: str, default: float) -> float:
        try:
            return float(self._get(key, default))
        except (TypeError, ValueError):
            return default

    def _is_owner(self, event: AstrMessageEvent) -> bool:
        """只认 owner_qq 里的号。

        以前还会放行 AstrBot 的 admins_id，但那样只要有人被加进机器人管理员就能删记忆，
        所以改成默认不放行；确实需要时把 allow_bot_admin 打开。
        """
        owner = self._get_str("owner_qq", "929765409")
        ids = {x.strip() for x in owner.replace("，", ",").split(",") if x.strip()}
        try:
            if str(event.get_sender_id()) in ids:
                return True
        except Exception:  # noqa: BLE001
            pass
        if self._get_bool("allow_bot_admin", False):
            try:
                return bool(event.is_admin())
            except Exception:  # noqa: BLE001
                return False
        return False

    # ---------- 会话状态 ----------

    def _state_file(self) -> str:
        return os.path.join(self._base, "state.json")

    def _load_state(self) -> dict:
        try:
            with open(os.path.join(self._base, "state.json"), encoding="utf-8") as fp:
                data = json.load(fp)
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _save_state(self) -> None:
        path = self._state_file()
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(self._state, fp, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[fish_memory] 保存状态失败：{exc}")

    def _session(self, umo: str) -> dict:
        sessions = self._state.setdefault("sessions", {})
        return sessions.setdefault(umo, {})

    def _session_on(self, umo: str) -> bool:
        if not self._get_bool("enable", True):
            return False
        default = self._get_bool("session_default_on", True)
        return bool(self._session(umo).get("on", default))

    def _quota_recent(self, umo: str) -> list[float]:
        bucket = (self._state.get("quota") or {}).get(umo) or {}
        now = time.time()
        recent = [
            float(x)
            for x in (bucket.get("recent") or [])
            if isinstance(x, (int, float)) and now - float(x) < 3600
        ]
        return recent

    def _quota_left(self, umo: str) -> int:
        hourly = self._get_int("hourly_quota", 3)
        daily = self._get_int("daily_quota", 30)
        if hourly > 0 and len(self._quota_recent(umo)) >= hourly:
            return 0
        today = time.strftime("%Y-%m-%d")
        item = (self._state.get("quota") or {}).get(umo) or {}
        if item.get("date") != today:
            return daily
        return max(0, daily - int(item.get("n") or 0))

    def _quota_take(self, umo: str, n: int = 1) -> None:
        today = time.strftime("%Y-%m-%d")
        bucket = self._state.setdefault("quota", {})
        item = bucket.get(umo) or {}
        if item.get("date") != today:
            item = {"date": today, "n": 0, "recent": []}
        item["n"] = int(item.get("n") or 0) + n
        item["recent"] = (self._quota_recent(umo) + [time.time()])[-60:]
        bucket[umo] = item

    # ---------- 旁路信息 ----------

    @staticmethod
    def _sender(event: AstrMessageEvent) -> tuple[str, str]:
        uid = name = ""
        try:
            uid = str(event.get_sender_id() or "")
        except Exception:  # noqa: BLE001
            pass
        try:
            name = str(event.get_sender_name() or "")
        except Exception:  # noqa: BLE001
            pass
        return uid, name

    @staticmethod
    def _message_text(event: AstrMessageEvent) -> str:
        try:
            return str(event.get_message_str() or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _reply_text(event: AstrMessageEvent) -> str:
        try:
            result = event.get_result()
        except Exception:  # noqa: BLE001
            return ""
        chain = getattr(result, "chain", None) or []
        parts = [
            comp.text
            for comp in chain
            if isinstance(comp, Plain) and getattr(comp, "text", None)
        ]
        return " ".join(parts).strip()

    # ---------- 注入 ----------

    @filter.on_llm_request()
    async def inject_memory(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        if not self._get_bool("inject_enable", True):
            return
        umo = event.unified_msg_origin
        if not self._session_on(umo):
            return
        text = self._message_text(event)
        if not text or text.startswith("/") or text.startswith("／"):
            return
        if MARKER in (req.prompt or ""):
            return

        uid, name = self._sender(event)
        if not event.get_extra("fish_memory_relation"):
            event.set_extra("fish_memory_relation", True)
            if uid:
                self.store.touch_relation(umo, uid, name, text)
                self.store.save_relations(umo)

        entries = self.store.load(umo)
        if not entries:
            return
        picked = select_memories(
            entries,
            text,
            top_n=self._get_int("top_n", 6),
            max_chars=self._get_int("max_inject_chars", 800),
            min_evidence=self._get_float("min_evidence", 0.34),
            half_life_days=float(self._get_int("half_life_days", 30)),
            scope=self._get_str("group_memory_scope", "shared"),
            speaker_uid=uid,
        )
        if not picked:
            return
        relation = (self.store.load_relations(umo) or {}).get(uid) if uid else None
        block = build_inject_block(picked, relation, name)
        if not block:
            return
        req.prompt = ((req.prompt or "") + "\n\n" + block).strip()
        event.set_extra("fish_memory_user", text)

        # 被想起来一次，就多一点留下来的理由
        self.store.touch(umo, {int(entry.get("id") or 0) for _t, entry, _d in picked})
        if time.time() - self._last_store_save.get(umo, 0.0) > 120:
            self.store.save(umo)
            self._last_store_save[umo] = time.time()

    # ---------- 写回 ----------

    @filter.on_decorating_result()
    async def capture_reply(self, event: AstrMessageEvent) -> None:
        if not self._get_bool("enable", True) or not self._get_bool("write_enable", True):
            return
        umo = event.unified_msg_origin
        if not self._session_on(umo):
            return
        if event.get_extra("fish_memory_done"):
            return
        reply = self._reply_text(event)
        user = self._message_text(event)
        if not reply or not user or user.startswith("/") or user.startswith("／"):
            return
        if len(user) < self._get_int("min_msg_chars", 8):
            return
        if TRIVIAL_RE.match(user.strip()):
            return
        if self._quota_left(umo) <= 0:
            return
        interval = self._get_int("extract_interval_seconds", 60)
        if interval > 0 and time.time() - self._last_extract.get(umo, 0.0) < interval:
            return
        event.set_extra("fish_memory_done", True)
        self._last_extract[umo] = time.time()
        uid, name = self._sender(event)
        job = {"umo": umo, "uid": uid, "name": name, "user": user, "reply": reply}
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            logger.warning("[fish_memory] 提炼队列已满，本轮跳过")
            return
        self._ensure_worker()

    # ---------- 提炼 ----------

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                await self._extract(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[fish_memory] 提炼失败：{exc}")
            finally:
                self._queue.task_done()
            await asyncio.sleep(1.0)

    async def _provider(self, umo: str):
        pid = self._get_str("provider_id")
        if pid:
            getter = getattr(self.context, "get_provider_by_id", None)
            if callable(getter):
                try:
                    provider = await getter(pid)
                    if provider:
                        return provider
                except Exception:  # noqa: BLE001
                    pass
        try:
            return await self.context.get_using_provider_async(umo)
        except Exception:  # noqa: BLE001
            return None

    async def _extract(self, job: dict) -> None:
        umo = job["umo"]
        if self._quota_left(umo) <= 0:
            return
        provider = await self._provider(umo)
        if not provider:
            logger.warning("[fish_memory] 没有可用的 provider，跳过提炼")
            return
        prompt = build_extract_prompt(job["user"], job["reply"], job["name"])
        resp = await provider.text_chat(prompt=prompt, system_prompt=EXTRACT_SYSTEM)
        items = parse_extracted(getattr(resp, "completion_text", "") or "")
        if not items:
            return
        added = 0
        limit = self._get_int("max_entries", 800)
        for item in items:
            if self._quota_left(umo) <= 0:
                break
            if int(item.get("importance") or 3) <= 1 and not item.get("key"):
                continue
            if self.store.find_similar(umo, item["text"]):
                continue
            item["created_at"] = time.time()
            item["src_uid"] = job.get("uid") or ""
            item["src_name"] = job.get("name") or ""
            entry, evicted = self.store.insert(umo, item, limit=limit)
            if not entry:
                logger.info("[fish_memory] 工作集已满，这条不如现有的，跳过")
                continue
            if evicted:
                logger.info(f"[fish_memory] 挤掉较弱的一条 #{evicted['id']}：{str(evicted.get('text'))[:20]}")
            self.store.supersede_by_key(umo, entry)
            self.store.append_journal(entry["text"], entry["created_at"], self._tag(umo, job.get("name")))
            added += 1
            self._quota_take(umo)
            logger.info(f"[fish_memory] 记住 #{entry['id']}：{entry['text'][:40]}")
        if added:
            self.store.save(umo)
            self._save_state()

    @staticmethod
    def _tag(umo: str, name: str = "") -> str:
        lowered = umo.lower()
        if "group" in lowered:
            kind = "群"
        elif "friend" in lowered or "private" in lowered:
            kind = "私聊"
        else:
            kind = "会话"
        return f"{kind}·{name}" if name else kind

    # ---------- 命令 ----------

    @filter.command("记忆簿", alias={"鱼忆"}, desc="查看这个会话记住了什么（仅主人）")
    async def list_cmd(self, event: AstrMessageEvent):
        if not self._is_owner(event):
            yield event.plain_result("这条只认主人哦。")
            return
        umo = event.unified_msg_origin
        entries = [m for m in self.store.load(umo) if not m.get("superseded")]
        if not entries:
            yield event.plain_result("这个会话还什么都没记。")
            return
        entries.sort(key=lambda m: float(m.get("created_at") or 0), reverse=True)
        lines = [f"记忆簿（{len(entries)} 条，{'开' if self._session_on(umo) else '关'}）"]
        for m in entries[:20]:
            star = "★" * int(m.get("importance") or 0)
            lines.append(f"#{m['id']} {fmt_day(float(m.get('created_at') or 0))} {m['text']} {star}")
        if len(entries) > 20:
            lines.append(f"……还有 {len(entries) - 20} 条")
        yield event.plain_result("\n".join(lines))

    @filter.command("记忆全书", desc="看所有会话的记忆统计（仅主人）")
    async def all_cmd(self, event: AstrMessageEvent):
        if not self._is_owner(event):
            yield event.plain_result("这条只认主人哦。")
            return
        buckets = [b for b in self.store.buckets() if b.get("entries")]
        if not buckets:
            yield event.plain_result("还什么都没记。")
            return
        total = sum(len(b["entries"]) for b in buckets)
        lines = [f"共 {len(buckets)} 个会话、{total} 条记忆"]
        for b in sorted(buckets, key=lambda item: len(item["entries"]), reverse=True)[:12]:
            lines.append(f"{session_label(b['umo'])}：{len(b['entries'])} 条")
        rows = []
        for b in buckets:
            for entry in b["entries"]:
                rows.append((float(entry.get("created_at") or 0), session_label(b["umo"]), entry))
        rows.sort(key=lambda item: item[0], reverse=True)
        lines.append("最近：")
        for ts, label, entry in rows[:5]:
            lines.append(f"- {time.strftime('%m-%d', time.localtime(ts))} {label} {str(entry.get('text'))[:28]}")
        yield event.plain_result("\n".join(lines))

    @filter.command("导出记忆", desc="导出人生档案 markdown（仅主人）")
    async def export_cmd(self, event: AstrMessageEvent):
        if not self._is_owner(event):
            yield event.plain_result("这条只认主人哦。")
            return
        try:
            markdown, count = build_life_markdown(self._base)
            out_dir = os.path.join(self._base, "export")
            os.makedirs(out_dir, exist_ok=True)
            path = os.path.join(out_dir, time.strftime("life-%Y%m%d-%H%M%S.md"))
            with open(path, "w", encoding="utf-8") as fp:
                fp.write(markdown)
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"导出失败：{exc}")
            return
        yield event.plain_result(f"导出好了（{count} 条）：\n{path}\n用 scp 拉走就行。")

    @filter.command("删记忆", desc="删掉一条记忆，用法：/删记忆 3（仅主人）")
    async def delete_cmd(self, event: AstrMessageEvent, text: GreedyStr):
        if not self._is_owner(event):
            yield event.plain_result("这条只认主人哦。")
            return
        arg = str(text).strip().lstrip("#")
        if arg in {"全部", "all", "*"}:
            self.store.clear(event.unified_msg_origin)
            self.store.save(event.unified_msg_origin)
            yield event.plain_result("这个会话的记忆清空了。")
            return
        try:
            mid = int(arg)
        except ValueError:
            yield event.plain_result("用法：/删记忆 3，或者 /删记忆 全部")
            return
        if not self.store.delete(event.unified_msg_origin, mid):
            yield event.plain_result(f"没找到 #{mid}。")
            return
        self.store.save(event.unified_msg_origin)
        yield event.plain_result(f"忘掉 #{mid} 了。")

    @filter.command("记忆开关", desc="开关当前会话的记忆（仅主人）")
    async def toggle_cmd(self, event: AstrMessageEvent):
        if not self._is_owner(event):
            yield event.plain_result("这条只认主人哦。")
            return
        umo = event.unified_msg_origin
        session = self._session(umo)
        session["on"] = not self._session_on(umo)
        self._save_state()
        yield event.plain_result(f"这个会话的记忆：{'开' if session['on'] else '关'}")

    @filter.command("关系卡", desc="看某个人的关系卡（仅主人）")
    async def relation_cmd(self, event: AstrMessageEvent, text: GreedyStr):
        if not self._is_owner(event):
            yield event.plain_result("这条只认主人哦。")
            return
        umo = event.unified_msg_origin
        relations = self.store.load_relations(umo) or {}
        if not relations:
            yield event.plain_result("还没有任何关系卡。")
            return
        arg = str(text).strip().lstrip("@")
        if arg and arg in relations:
            relations = {arg: relations[arg]}
        lines = []
        for uid, card in list(relations.items())[:10]:
            level = {0: "生人", 1: "半熟", 2: "熟人"}.get(int(card.get("level") or 0), "生人")
            name = card.get("name") or uid
            line = f"{name}（{level}，{card.get('count') or 0} 次）"
            if card.get("topic"):
                line += f" 上次：{card['topic']}"
            lines.append(line)
        yield event.plain_result("关系卡：\n" + "\n".join(lines))

    @filter.command("进化", desc="从最近记住的事里看出性格变化候选（仅主人）")
    async def evolve_cmd(self, event: AstrMessageEvent):
        if not self._is_owner(event):
            yield event.plain_result("这条只认主人哦。")
            return
        umo = event.unified_msg_origin
        cutoff = time.time() - 86400 * 30
        texts = [
            str(m.get("text") or "")
            for m in self.store.load(umo)
            if not m.get("superseded") and float(m.get("created_at") or 0) >= cutoff
        ]
        if len(texts) < 5:
            yield event.plain_result("最近记住的事还太少，等攒够 5 条再看。")
            return
        provider = await self._provider(umo)
        if not provider:
            yield event.plain_result("没有可用的模型，等会儿再试。")
            return
        try:
            resp = await provider.text_chat(
                prompt=build_evolve_prompt(texts), system_prompt=EVOLVE_SYSTEM
            )
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"分析失败：{exc}")
            return
        candidates = parse_evolve(getattr(resp, "completion_text", "") or "")
        if not candidates:
            yield event.plain_result("最近看不出明显变化。")
            return
        lines = ["可能的性格变化（确认后再写进人设）："]
        for i, (habit, reason) in enumerate(candidates, 1):
            lines.append(f"{i}. {habit}" + (f"（依据：{reason}）" if reason else ""))
        yield event.plain_result("\n".join(lines))

    async def terminate(self) -> None:
        if self._worker and not self._worker.done():
            self._worker.cancel()
        self._save_state()
        logger.info("[fish_memory] 插件已停止")
