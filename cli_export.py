"""在服务器上直接导出人生档案，不经过 QQ。

容器内跑：

    docker exec -i astrbot python /AstrBot/data/plugins/astrbot_plugin_fish_memory/cli_export.py

也可以用第二个参数指定数据目录：

    docker exec -i astrbot python .../cli_export.py /AstrBot/data/plugin_data/fish_memory

成功时输出两行：文件路径、条数。
"""

from __future__ import annotations

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from astrbot_plugin_fish_memory.export import build_life_markdown  # noqa: E402

DEFAULT_BASE = os.path.join(os.environ.get("ASTRBOT_DATA", "/AstrBot/data"), "plugin_data", "fish_memory")


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BASE
    if not os.path.isdir(base):
        print(f"数据目录不存在：{base}", file=sys.stderr)
        return 2
    markdown, count = build_life_markdown(base)
    out_dir = os.path.join(base, "export")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, time.strftime("life-%Y%m%d-%H%M%S.md"))
    with open(path, "w", encoding="utf-8") as fp:
        fp.write(markdown)
    print(path)
    print(count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
