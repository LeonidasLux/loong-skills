#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""更新 RDC 工作项的三个富文本字段：根本原因、功能交叉影响分析、解决方案描述。

用法:
    python3 update_rdc_fields.py <RDC单号> <根本原因html> <功能交叉影响分析html> <解决方案描述html>

示例:
    python3 update_rdc_fields.py DT_PP-1410130 "<p>根因...</p>" "<p>波及...</p>" "<p>方案...</p>"

    # 只预览将要提交的内容，不真正更新
    python3 update_rdc_fields.py DT_PP-1410130 "" "" "" --dry-run

参数说明:
    - 三个字段的值均按「已拼装好的 HTML 字符串」原样写入，脚本不做任何改写。
    - 任一参数传空字符串 "" 表示把该字段置空。

脚本内部调用 edw 命令行工具的 batch_update_work_items 方法。
"""

import argparse
import json
import shutil
import subprocess
import sys

# 入参顺序 -> RDC 字段 key
FIELD_KEYS = [
    ("root_cause", "BasicReason_html", "根本原因"),
    ("impact_analysis", "CrossImpactAnalysis_html", "功能交叉影响分析"),
    ("solution", "SolutionDescription_html", "解决方案描述"),
]


def find_edw():
    exe = shutil.which("edw")
    if not exe:
        sys.exit("未找到 edw 命令，请先安装并初始化 easy-develop-work（edw）工具。")
    return exe


def call_edw(args, exe):
    """执行 edw 命令并返回 (returncode, stdout, stderr)。"""
    try:
        proc = subprocess.run(
            [exe] + args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        sys.exit("执行 edw 失败: {}".format(exc))
    return proc.returncode, (proc.stdout or ""), (proc.stderr or "")


def parse_json(stdout):
    start, end = stdout.find("{"), stdout.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(stdout[start : end + 1])
    except json.JSONDecodeError:
        return None


def main():
    parser = argparse.ArgumentParser(
        description="更新 RDC 工作项的根本原因、功能交叉影响分析、解决方案描述三个字段。"
    )
    parser.add_argument("work_item_id", help="RDC 工作项单号，如 DT_PP-1410130")
    parser.add_argument("root_cause", help="根本原因 HTML")
    parser.add_argument("impact_analysis", help="功能交叉影响分析 HTML")
    parser.add_argument("solution", help="解决方案描述 HTML")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将要执行的命令和字段内容，不实际更新",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="更新完成后重新查询，回显三个字段的最新长度",
    )
    args = parser.parse_args()

    values = {
        "root_cause": args.root_cause,
        "impact_analysis": args.impact_analysis,
        "solution": args.solution,
    }

    # 这三个字段都是富文本编辑器字段，使用完整格式指定 type=html
    fields_dict = {
        field_key: {"value": values[arg_name], "type": "html"}
        for arg_name, field_key, _ in FIELD_KEYS
    }

    print("单号: {}".format(args.work_item_id))
    for arg_name, field_key, label in FIELD_KEYS:
        content = values[arg_name]
        print("  {} ({}) 长度: {} 字符".format(label, field_key, len(content)))

    if args.dry_run:
        print()
        print("【dry-run】字段内容如下，未执行更新：")
        for arg_name, field_key, label in FIELD_KEYS:
            print("-" * 50)
            print("{} ({})".format(label, field_key))
            print(values[arg_name] or "(空)")
        return

    exe = find_edw()
    code, out, err = call_edw(
        [
            "batch_update_work_items",
            "--work_item_ids_str",
            args.work_item_id,
            "--fields_dict",
            json.dumps(fields_dict, ensure_ascii=False),
        ],
        exe,
    )

    result = parse_json(out)
    if result is None:
        print("edw 输出（退出码 {}）:\n{}".format(code, out.strip()))
        if err.strip():
            print("stderr:\n{}".format(err.strip()))
        sys.exit("未能解析更新结果，请检查上面的输出。")

    if result.get("error"):
        sys.exit("更新失败: {}".format(result["error"]))

    failed = result.get("failedItems") or []
    print()
    print(
        "更新结果: 成功 {} 条, 失败 {} 条".format(
            result.get("successCount", 0), result.get("failedCount", len(failed))
        )
    )
    for item in failed:
        print("  失败项 {}: {}".format(item.get("id"), item.get("error")))

    if args.verify:
        print()
        code, out, err = call_edw(
            [
                "get_work_item",
                "--work_item_id",
                args.work_item_id,
                "--keywords",
                ",".join(key for _, key, _ in FIELD_KEYS),
            ],
            find_edw(),
        )
        check = parse_json(out)
        if not check:
            print("回查失败:\n{}".format(out.strip() or err.strip()))
        else:
            print("【回查】")
            for field in check.get("fields", []):
                value = field.get("value") or ""
                print(
                    "  {} ({}) 当前长度: {} 字符".format(
                        field.get("name"), field.get("key"), len(str(value))
                    )
                )

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
