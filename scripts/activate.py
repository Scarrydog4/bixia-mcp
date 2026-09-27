#!/usr/bin/env python3
"""Install a private activation config without exposing its access key."""
import argparse
import json
import os
from pathlib import Path
import sys

from bixia_client import (DEFAULT_CA, DEFAULT_CONFIG, IS_WINDOWS, ShareClient, ShareError,
                             ensure_private_file, private_write, protect_private_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Private activation JSON supplied by the service owner")
    parser.add_argument("--destination", default=str(DEFAULT_CONFIG), help="Private local config destination")
    args = parser.parse_args()
    client = None
    try:
        source = Path(args.config).expanduser()
        if IS_WINDOWS:
            protect_private_path(source)
        ensure_private_file(source)
        value = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or set(value) - {"server_url", "api_key", "ca_file"}:
            raise ShareError("configuration")
        ca_file = str(DEFAULT_CA.resolve())
        client = ShareClient(value["server_url"], value["api_key"], ca_file=ca_file)
        private_write(Path(args.destination).expanduser(), {"server_url": client.url, "api_key": value["api_key"], "ca_file": ca_file})
        print("笔下MCP已保存本机私有配置。尚未联网或提交文件。")
        return 0
    except Exception:
        print("激活失败，请检查私有配置权限（macOS/Linux为600，Windows仅当前用户及系统管理员）、访问密钥及可信证书。", file=sys.stderr)
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
