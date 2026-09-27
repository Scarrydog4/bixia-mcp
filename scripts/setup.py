#!/usr/bin/env python3
"""Activate BIxia and print a portable MCP entry; never edit an AI client's settings."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

if sys.version_info < (3, 10):
    raise SystemExit("需要 Python 3.10 或以上。")

from bixia_client import DEFAULT_CONFIG, IS_WINDOWS, ShareClient, private_write


def main():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--activation", help="单独提供的个人激活 JSON 路径")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="本机私有配置路径")
    parser.add_argument("--output", default=str(Path.home() / ".bixia-mcp" / "mcp-servers.json"),
                        help="生成的 MCP 配置路径；不会覆盖 AI 客户端设置")
    args = parser.parse_args()
    base = Path(__file__).resolve().parent
    config = Path(args.config).expanduser().resolve()
    destination = Path(args.output).expanduser().resolve()
    activation = args.activation
    if activation is None and not config.exists():
        if not sys.stdin.isatty():
            print("请通过 --activation 指定单独提供的激活 JSON。", file=sys.stderr)
            return 1
        activation = input("请输入激活 JSON 的完整路径：").strip().strip("\"'")
        if not activation:
            print("需要激活文件才能完成安装。", file=sys.stderr)
            return 1
    try:
        if destination == config or (activation and destination == Path(activation).expanduser().resolve()):
            raise ValueError("接入配置不能覆盖激活文件或私有配置")
        if activation:
            source = Path(activation).expanduser().resolve()
            if not IS_WINDOWS:
                os.chmod(source, 0o600)
            completed = subprocess.run([sys.executable, str(base / "activate.py"),
                                        "--config", str(source), "--destination", str(config)])
            if completed.returncode:
                return completed.returncode
        client = ShareClient.from_config(str(config))
        client.close()
        entry = {"mcpServers": {"bixia-mcp": {
            "command": str(Path(sys.executable).resolve()),
            "args": [str(base / "bixia_client.py"), "--config", str(config)],
            "env": {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        }}}
        private_write(destination, entry)
        print("\nBIxia 本机配置已准备好，尚未联网或提交改写任务。")
        print("将下面条目合并到支持本机 stdio MCP 的 AI 客户端配置中，然后重启客户端。")
        print("请保留客户端已有的其他 MCP 条目。")
        print(json.dumps(entry, ensure_ascii=False, indent=2))
        print("\n接入配置已保存：" + str(destination))
        return 0
    except Exception:
        print("配置失败，请检查激活文件、文件权限和安装路径。访问密钥不会显示。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
