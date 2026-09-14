#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""查询 RDC 工作项，输出标题、描述、故障等级。

用法:
    python3 query_rdc_item.py DT_PP-1410130
    python3 query_rdc_item.py DT_PP-1410130 --json   # 额外输出原始字段

脚本内部调用 edw 命令行工具的 get_work_item 方法。
"""

import argparse
import html
import json
import re
import shutil
import subprocess
import sys


def run_edw(work_item_id):
    """调用 edw get_work_item 并返回解析后的字典。"""
    exe = shutil.which("edw")
    if not exe:
        sys.exit("未找到 edw 命令，请先安装并初始化 easy-develop-work（edw）工具。")

    cmd = [exe, "get_work_item", "--work_item_id", work_item_id]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        sys.exit("执行 edw 失败: {}".format(exc))

    stdout = (proc.stdout or "").strip()
    if not stdout:
        sys.exit(
            "edw 未返回内容。\n退出码: {}\nstderr: {}".format(
                proc.returncode, (proc.stderr or "").strip()
            )
        )

    # 结果可能夹带提示信息，截取最外层 JSON 对象。
    start = stdout.find("{")
    end = stdout.rfind("}")
    if start == -1 or end == -1:
        sys.exit("无法从 edw 输出中解析 JSON:\n{}".format(stdout))

    try:
        data = json.loads(stdout[start : end + 1])
    except json.JSONDecodeError as exc:
        sys.exit("edw 输出解析 JSON 失败: {}\n原始输出:\n{}".format(exc, stdout))

    if isinstance(data, dict) and data.get("error"):
        sys.exit("查询失败: {}".format(data["error"]))
    return data


def html_to_text(raw):
    """把富文本字段转成纯文本。"""
    if not raw:
        return ""
    text = str(raw)
    if "<" not in text:
        return text.strip()
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def pick_value(fields, *keys):
    """按字段 key 取值，返回可读文本。"""
    for key in keys:
        field = fields.get(key)
        if not field:
            continue
        value = field.get("value")
        if value in (None, "", [], {}):
            continue
        if isinstance(value, dict):
            return value.get("name") or value.get("label") or json.dumps(
                value, ensure_ascii=False
            )
        if isinstance(value, list):
            names = [
                item.get("nameDisplayLong") or item.get("name") or ""
                for item in value
                if isinstance(item, dict)
            ]
            return "; ".join(name for name in names if name)
        return html_to_text(value)
    return ""


def main():
    parser = argparse.ArgumentParser(
        description="调用 edw 查询 RDC 工作项，输出标题、描述、故障等级。"
    )
    parser.add_argument("work_item_id", help="RDC 工作项单号，如 DT_PP-1410130")
    parser.add_argument(
        "--json",
        action="store_true",
        help="附打印该工作项的全部字段（JSON）",
    )
    args = parser.parse_args()

    data = run_edw(args.work_item_id)
    fields = {field["key"]: field for field in data.get("fields", [])}

    title = pick_value(fields, "System_Title")
    # 描述：优先取纯文本字段，其次取富文本字段
    description = pick_value(
        fields, "System_Description", "System_Description_html"
    )
    incident_level = pick_value(
        fields, "DefectLevel", "BugSeverity", "Severity", "System_Severity"
    )

    print("=" * 60)
    print("单号: {}".format(data.get("work_item_id", args.work_item_id)))
    print("类型: {}".format(data.get("workItemTypeKey", "")))
    print("工作空间: {}".format(data.get("workspaceKey", "")))
    print("=" * 60)
    print("【标题】")
    print(title or "(空)")
    print()
    print("【描述】")
    print(description or "(空)")
    print()
    print("【故障等级】")
    print(incident_level or "(空)")

    if args.json:
        print()
        print("【原始字段 JSON】")
        print(json.dumps(fields, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
