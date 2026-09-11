"""Directly runnable, single-query intent extraction with printed output."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfoNotFoundError

# Support running this file by absolute path from any working directory.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dataelf_server.intent import IntentError, IntentRecognizer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="输入用户意图，打印识别结果，不写文件。默认段解析检索条件；# 写作要求 等标题下解析 output。")
    parser.add_argument("query", help="用户意图；包含空格时请使用引号")
    parser.add_argument("--reference-time", type=datetime.fromisoformat,
                        help="可选参考时间，需带时区，例如 2026-09-08T12:00:00+08:00")
    parser.add_argument("--timezone", default="Asia/Shanghai", help="日期解析时区")
    parser.add_argument("--config", type=Path, help="DataElf 配置文件路径")
    args = parser.parse_args(argv)
    if args.config:
        import os
        os.environ["DATAELF_CONFIG_FILE"] = str(args.config.resolve())
    try:
        intent = IntentRecognizer().extract(
            args.query, reference_time=args.reference_time,
            timezone=args.timezone,
        )
    except (IntentError, ValueError, OSError, ZoneInfoNotFoundError) as exc:
        error = {"error": str(exc)}
        print(json.dumps(error, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    print(intent.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
