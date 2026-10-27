"""鱼的记忆 · 纯逻辑层：分词、半衰期、召回打分。

这里不 import astrbot，方便在本机单独跑：

    python store.py
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time

CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
LATIN_RE = re.compile(r"[a-zA-Z0-9_+\-]{2,}")

# 单字停用词：只留那些几乎不携带信息的
STOP_CHARS = set("的了是在我你他她它们这那有和就都很吗呢吧啊呀哦嗯个上下不会要去来得到着过把被给让对从而又还只才没")
# 语气助词：含这些字的二元组基本是噪音
PARTICLES = set("的了吗呢吧啊呀哦嗯")
# 双字停用词：常见连接词、口头禅
STOP_BIGRAMS = {
    "什么", "怎么", "这个", "那个", "就是", "可以", "没有", "一个", "我们", "你们",
    "他们", "她们", "是不", "不是", "的话", "现在", "然后", "因为", "所以", "但是",
    "如果", "感觉", "有点", "真的", "知道", "觉得", "时候", "今天", "昨天", "明天",
    "之前", "之后", "一下", "一点", "这样", "那样", "这种", "那种", "还是", "应该",
    "可能", "好像", "其实", "东西", "事情", "是不是", "有没有", "能不能",
}

# 召回权重：证据、新鲜度、重要度、情绪
WEIGHTS = (0.45, 0.25, 0.20, 0.10)

DEFAULT_HALF_LIFE_DAYS = 30.0


def tokenize(text: str) -> set[str]:
    """中文按二元组切，英文/数字按词切。够用，且不需要 jieba。"""
    text = (text or "").lower()
    tokens: set[str] = {t for t in LATIN_RE.findall(text)}
    for run in CJK_RE.findall(text):
        if len(run) == 1:
            if run not in STOP_CHARS:
                tokens.add(run)
            continue
        for i in range(len(run) - 1):
            bigram = run[i : i + 2]
            if bigram in STOP_BIGRAMS:
                continue
            if bigram[0] in PARTICLES or bigram[1] in PARTICLES:
                continue
            tokens.add(bigram)
    return {t for t in tokens if t not in STOP_CHARS}


def entry_tokens(entry: dict) -> set[str]:
    text = str(entry.get("text") or "")
    key = str(entry.get("key") or "").replace(".", " ").replace("_", " ")
    terms = " ".join(str(t) for t in (entry.get("terms") or []))
    return tokenize(text) | tokenize(key) | tokenize(terms)


def freshness(created_at: float, now: float, half_life_days: float) -> float:
    """半衰期：half_life_days 天后分数减半，不删除。"""
    age_days = max(0.0, (now - float(created_at)) / 86400.0)
    return 0.5 ** (age_days / max(1.0, half_life_days))


def evidence(query_tokens: set[str], tokens: set[str]) -> float:
    if not query_tokens or not tokens:
        return 0.0
    return len(query_tokens & tokens) / float(len(query_tokens))


def score_entry(
    entry: dict,
    query_tokens: set[str],
    query_text: str,
    now: float,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
) -> tuple[float, dict]:
    """返回 (总分, 明细)。明细用于 /记忆 里解释「为什么想起这条」。

    除了字面重合，还会看提炼时给出的关键词（terms）：
    「论文写得怎么样了」和「下周要交论文」字面只重合一个词，但 term 命中就能捞起来。
    """
    ev = evidence(query_tokens, entry_tokens(entry))
    terms = [str(t).strip() for t in (entry.get("terms") or []) if str(t).strip()]
    if terms and query_text:
        lowered = query_text.lower()
        hits = sum(1 for t in terms if t.lower() in lowered)
        if hits:
            ev = max(ev, min(0.9, 0.55 + 0.15 * hits))
    fr = freshness(entry.get("created_at") or now, now, half_life_days)
    imp = max(0.0, min(5.0, float(entry.get("importance") or 3))) / 5.0
    emo = max(0.0, min(1.0, float(entry.get("emotion") or 0)))
    w = WEIGHTS
    total = w[0] * ev + w[1] * fr + w[2] * imp + w[3] * emo
    return total, {"evidence": ev, "freshness": fr, "importance": imp, "emotion": emo}


def entry_value(entry: dict, now: float, half_life_days: float = DEFAULT_HALF_LIFE_DAYS) -> float:
    """淘汰用的「价值」：重要度为主，新鲜度和被想起来的次数为辅。

    和召回打分不是一回事：召回看「和这次问的相关性」，这里看「值不值得留」。
    """
    imp = max(0.0, min(5.0, float(entry.get("importance") or 3))) / 5.0
    fr = freshness(entry.get("created_at") or now, now, half_life_days)
    hits = min(1.0, float(entry.get("hits") or 0) / 3.0)
    return 0.6 * imp + 0.3 * fr + 0.1 * hits


def select_memories(
    entries: list[dict],
    query: str,
    now: float | None = None,
    *,
    top_n: int = 6,
    max_chars: int = 800,
    min_evidence: float = 0.34,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    scope: str = "shared",
    speaker_uid: str = "",
) -> list[tuple[float, dict, dict]]:
    """按打分挑出这次要注入的记忆。

    规则：
    - 被取代的（superseded）不要
    - 长期事实（pinned）永远参与；普通记忆证据不足就跳过
    - 按分数降序，总字数封顶
    - scope="per_speaker" 时，只留当前说话人自己的、以及公共条目（who=共同 / pinned /
      没有来源的老条目），别人的条目不参与
    """
    now = time.time() if now is None else now
    q = tokenize(query)
    strict = scope == "per_speaker" and bool(speaker_uid)
    ranked: list[tuple[float, dict, dict]] = []
    for entry in entries:
        if entry.get("superseded"):
            continue
        if strict:
            source = str(entry.get("src_uid") or "")
            shared = (
                not source
                or source == speaker_uid
                or bool(entry.get("pinned"))
                or str(entry.get("who") or "") == "共同"
            )
            if not shared:
                continue
        total, detail = score_entry(entry, q, query, now, half_life_days)
        if not entry.get("pinned") and detail["evidence"] < min_evidence:
            continue
        ranked.append((total, entry, detail))
    ranked.sort(key=lambda item: item[0], reverse=True)

    picked: list[tuple[float, dict, dict]] = []
    used = 0
    for item in ranked[: max(1, top_n * 3)]:
        text = str(item[1].get("text") or "")
        if used + len(text) > max_chars and picked:
            continue
        picked.append(item)
        used += len(text)
        if len(picked) >= top_n:
            break
    return picked

def fmt_day(ts: float) -> str:
    return time.strftime("%m-%d", time.localtime(ts))


class MemoryStore:
    """一个会话一个目录：entries.json + relations.json。

    写文件用临时文件 + os.replace，避免断电写坏。
    """

    def __init__(self, base_dir: str) -> None:
        self.base = base_dir
        os.makedirs(base_dir, exist_ok=True)
        self._cache: dict[str, list[dict]] = {}
        self._relations: dict[str, dict] = {}

    # ---------- 路径 ----------

    @staticmethod
    def bucket(umo: str) -> str:
        return hashlib.md5((umo or "default").encode("utf-8")).hexdigest()[:12]

    def _dir(self, umo: str) -> str:
        path = os.path.join(self.base, self.bucket(umo))
        os.makedirs(path, exist_ok=True)
        meta = os.path.join(path, "meta.json")
        if not os.path.exists(meta):
            try:
                with open(meta, "w", encoding="utf-8") as fp:
                    json.dump({"umo": umo}, fp, ensure_ascii=False)
            except Exception:
                pass
        return path

    def _entries_file(self, umo: str) -> str:
        return os.path.join(self._dir(umo), "entries.json")

    def _relations_file(self, umo: str) -> str:
        return os.path.join(self._dir(umo), "relations.json")

    # ---------- 记忆 ----------

    def load(self, umo: str) -> list[dict]:
        if umo in self._cache:
            return self._cache[umo]
        try:
            with open(self._entries_file(umo), encoding="utf-8") as fp:
                data = json.load(fp)
            items = [m for m in data if isinstance(m, dict) and str(m.get("text") or "").strip()]
        except Exception:
            items = []
        self._cache[umo] = items
        return items

    def save(self, umo: str) -> None:
        items = self._cache.get(umo) or []
        path = self._entries_file(umo)
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(items, fp, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception:
            pass

    def _normalize(self, item: dict, next_id: int, now: float) -> dict:
        return {
            "id": next_id,
            "text": " ".join(str(item.get("text") or "").split())[:160],
            "key": str(item.get("key") or "").strip()[:40],
            "importance": max(1, min(5, int(item.get("importance") or 3))),
            "emotion": max(0.0, min(1.0, float(item.get("emotion") or 0))),
            "who": str(item.get("who") or "她")[:8],
            "terms": [str(t)[:12] for t in (item.get("terms") or [])][:6],
            "pinned": bool(item.get("pinned")),
            "src_uid": str(item.get("src_uid") or "")[:32],
            "src_name": str(item.get("src_name") or "")[:24],
            "source": str(item.get("source") or "chat")[:16],
            "created_at": float(item.get("created_at") or now),
            "last_seen": now,
            "hits": 0,
            "superseded": False,
        }

    def _next_id(self, items: list[dict]) -> int:
        return max([int(m.get("id") or 0) for m in items] or [0]) + 1

    def add(self, umo: str, item: dict) -> dict:
        """不加限制地写入（测试和手动导入用；正常聊天走 insert）。"""
        items = self.load(umo)
        now = time.time()
        entry = self._normalize(item, self._next_id(items), now)
        items.append(entry)
        self._cache[umo] = items
        return entry

    def insert(
        self,
        umo: str,
        item: dict,
        *,
        limit: int = 800,
        protect_days: int = 7,
        margin: float = 0.02,
    ) -> tuple[dict | None, dict | None]:
        """写入一条记忆，必要时按「价值」挤掉最弱的一条。

        淘汰顺序：
        1. 已被取代的死条目，先清
        2. 没满就直接放
        3. 满了：7 天内的新条目和 pinned 受保护；新条目价值要比最弱者高 margin 才顶替
        返回 (新条目, 被挤掉的条目)；没写进去时第一项为 None。
        """
        items = self.load(umo)
        now = time.time()
        entry = self._normalize(item, self._next_id(items), now)

        dead = [old for old in items if old.get("superseded")]
        if dead:
            items.remove(dead[0])
            items.append(entry)
            return entry, None

        if len(items) < limit:
            items.append(entry)
            return entry, None

        protected = {id(old) for old in items if old.get("pinned") or now - float(old.get("created_at") or 0) < protect_days * 86400}
        pool = [old for old in items if id(old) not in protected]
        if not pool:
            return None, None

        weakest = min(pool, key=lambda old: entry_value(old, now))
        if entry_value(entry, now) < entry_value(weakest, now) + margin:
            return None, None

        items.remove(weakest)
        items.append(entry)
        return entry, weakest

    def touch(self, umo: str, ids: set[int]) -> None:
        """被想起来的记忆加一次命中，长期会被优先保留。"""
        now = time.time()
        for old in self.load(umo):
            if int(old.get("id") or 0) in ids:
                old["hits"] = int(old.get("hits") or 0) + 1
                old["last_seen"] = now

    def supersede_by_key(self, umo: str, entry: dict) -> bool:
        """同 key 只留最新：把旧的标成已被取代。返回是否动过。"""
        key = entry.get("key")
        if not key:
            return False
        changed = False
        for old in self.load(umo):
            if old is entry or old.get("superseded"):
                continue
            if old.get("key") == key:
                old["superseded"] = True
                changed = True
        return changed

    def find_similar(self, umo: str, text: str) -> dict | None:
        norm = " ".join(str(text or "").split())
        for old in self.load(umo):
            if old.get("superseded"):
                continue
            if " ".join(str(old.get("text") or "").split()) == norm:
                return old
        return None

    def delete(self, umo: str, mid: int) -> bool:
        items = self.load(umo)
        kept = [m for m in items if int(m.get("id") or 0) != mid]
        if len(kept) == len(items):
            return False
        self._cache[umo] = kept
        return True

    def clear(self, umo: str) -> None:
        self._cache[umo] = []

    def prune(self, umo: str, limit: int = 400) -> None:
        """超出上限时删最旧、最不重要、且没被钉住的。"""
        items = self.load(umo)
        if len(items) <= limit:
            return
        pinned = [m for m in items if m.get("pinned")]
        rest = [m for m in items if not m.get("pinned")]
        rest.sort(key=lambda m: (float(m.get("created_at") or 0), int(m.get("importance") or 3)))
        self._cache[umo] = pinned + rest[-(limit - len(pinned)) :] if len(pinned) < limit else pinned[-limit:]

    # ---------- 遍历所有会话 ----------

    def read_entries(self, path: str) -> list[dict]:
        try:
            with open(os.path.join(path, "entries.json"), encoding="utf-8") as fp:
                data = json.load(fp)
            return [m for m in data if isinstance(m, dict) and str(m.get("text") or "").strip()]
        except Exception:
            return []

    def buckets(self) -> list[dict]:
        """列出所有会话目录，带原始 umo（老数据没有 meta 就留空）。"""
        result: list[dict] = []
        try:
            names = sorted(os.listdir(self.base))
        except OSError:
            return result
        for name in names:
            path = os.path.join(self.base, name)
            if not os.path.isdir(path):
                continue
            umo = ""
            try:
                with open(os.path.join(path, "meta.json"), encoding="utf-8") as fp:
                    umo = str((json.load(fp) or {}).get("umo") or "")
            except Exception:
                umo = ""
            result.append({"name": name, "umo": umo, "entries": self.read_entries(path), "path": path})
        return result

    # ---------- 人生档案（只追加，永不删除） ----------

    def journal_dir(self) -> str:
        path = os.path.join(self.base, "journal")
        os.makedirs(path, exist_ok=True)
        return path

    def append_journal(self, text: str, ts: float | None = None, tag: str = "") -> None:
        ts = time.time() if ts is None else ts
        path = os.path.join(self.journal_dir(), time.strftime("%Y-%m.md", time.localtime(ts)))
        stamp = time.strftime("%m-%d %H:%M", time.localtime(ts))
        line = f"- {stamp}{(' ' + tag) if tag else ''}：{text}\n"
        try:
            with open(path, "a", encoding="utf-8") as fp:
                fp.write(line)
        except Exception:
            pass

    def journal_files(self) -> list[str]:
        try:
            names = sorted(n for n in os.listdir(self.journal_dir()) if n.endswith(".md"))
        except OSError:
            return []
        return [os.path.join(self.journal_dir(), n) for n in names]

    # ---------- 关系 ----------

    def load_relations(self, umo: str) -> dict:
        if umo in self._relations:
            return self._relations[umo]
        try:
            with open(self._relations_file(umo), encoding="utf-8") as fp:
                data = json.load(fp)
            rel = data if isinstance(data, dict) else {}
        except Exception:
            rel = {}
        self._relations[umo] = rel
        return rel

    def save_relations(self, umo: str) -> None:
        path = self._relations_file(umo)
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(self._relations.get(umo) or {}, fp, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception:
            pass

    def touch_relation(self, umo: str, uid: str, name: str, topic: str) -> dict:
        rel = self.load_relations(umo)
        card = rel.get(uid) or {"count": 0, "level": 0, "name": "", "topic": "", "last_seen": 0}
        card["count"] = int(card.get("count") or 0) + 1
        if name:
            card["name"] = name[:24]
        topic = " ".join(str(topic or "").split())
        if topic:
            card["topic"] = topic[:40]
        card["last_seen"] = time.time()
        card["level"] = level_of(int(card["count"]))
        rel[uid] = card
        self._relations[umo] = rel
        return card

    # ---------- 全局关系（只记熟度，不记内容） ----------

    def _global_file(self) -> str:
        return os.path.join(self.base, "relations_global.json")

    def load_global(self) -> dict:
        if "_global" in self._relations:
            return self._relations["_global"]
        try:
            with open(self._global_file(), encoding="utf-8") as fp:
                data = json.load(fp)
            rel = data if isinstance(data, dict) else {}
        except Exception:
            rel = {}
        self._relations["_global"] = rel
        return rel

    def save_global(self) -> None:
        path = self._global_file()
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(self._relations.get("_global") or {}, fp, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception:
            pass

    def touch_global(self, uid: str, name: str) -> dict:
        """跨会话累计互动次数：只用来推熟度，不存任何聊天内容。"""
        rel = self.load_global()
        card = rel.get(uid) or {"count": 0, "name": "", "last_seen": 0}
        card["count"] = int(card.get("count") or 0) + 1
        if name:
            card["name"] = name[:24]
        card["last_seen"] = time.time()
        rel[uid] = card
        return card


def level_of(count: int) -> int:
    """五级：生人 / 眼熟 / 半熟 / 熟人 / 自己人。"""
    if count >= 80:
        return 4
    if count >= 30:
        return 3
    if count >= 10:
        return 2
    if count >= 3:
        return 1
    return 0


LEVEL_TEXT = {0: "生人", 1: "眼熟", 2: "半熟", 3: "熟人", 4: "自己人", 5: "恋人"}


if __name__ == "__main__":  # 本地冒烟测试
    import tempfile

    root = tempfile.mkdtemp(prefix="fish-mem-")
    store = MemoryStore(root)
    umo = "aiocqhttp:GroupMessage:123456"
    now = time.time()

    store.add(umo, {"text": "她说过下周要交论文，一直拖着没写", "key": "约定.论文", "terms": ["论文", "截止"], "importance": 4, "created_at": now - 86400 * 2})
    store.add(umo, {"text": "她不喜欢别人叫她全名", "importance": 5, "created_at": now - 86400 * 10})
    store.add(umo, {"text": "她养了一只叫豆豆的猫", "key": "她.宠物", "terms": ["豆豆", "猫"], "importance": 4, "created_at": now - 86400 * 20})
    store.add(umo, {"text": "她今天中午吃了牛肉面", "importance": 1, "created_at": now})

    for q in ["论文写得怎么样了", "豆豆今天乖吗", "中午吃啥"]:
        picked = select_memories(store.load(umo), q, now, min_evidence=0.2)
        print(f"\n查询：{q}")
        for total, entry, detail in picked:
            print(f"  {total:.2f} {fmt_day(entry['created_at'])} {entry['text']}  {detail}")
