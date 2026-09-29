#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CCA「4个0」问题扫描脚本（CLI 入口）。

「4个0」指以下 4 类问题的数量都必须为 0（口径可在配置文件中调整）：
    1. Klocwork Critical
    2. Klocwork Error
    3. Coverity High
    4. Coverity Medium

Hub（开源组件漏洞）作为并列的第二组目标：版本级数量与 BLOCK/ALARM/PASS
指标从 CCA 开放 API 读取；组件级明细（组件名/版本/CVE/Origin id）由技能
下载各 Hub 任务报告后解析。

子命令：
    check         校验配置完整性，并用 CCA 接口验证凭据是否有效
    check-auth    只验证凭据是否有效
    scan          扫描项目并输出 JSON/CSV 结果（默认子命令）
    hub           只跑 Hub（版本统计 + 报告 Excel 组件明细）

用法示例：
    python3 scripts/scan_leaks.py check
    python3 scripts/scan_leaks.py scan --project zte-aiop-aiservice-appui
    python3 scripts/scan_leaks.py scan --no-sync --json
    python3 scripts/scan_leaks.py scan --fail-on-leak
    python3 scripts/scan_leaks.py hub --version XYUAC-AICSV1.26.33B01

CCA 接口：统一调用开放 API（<base_url>/api/v2），请求头只带两个鉴权参数
X-Emp-No + X-Uac-Token。

凭据（员工号 + UAC token）来源，按优先级：
    1. 命令行 --emp-no / --uac-token
    2. 环境变量 FOUR0S_CCA_EMP_NO / FOUR0S_CCA_UAC_TOKEN
    3. OpenClaw 注入的环境变量 coclaw_empno / coclaw_token
    4. 配置文件 <skill>/config/config.yaml 的 cca.emp_no / cca.uac_token

配置文件默认路径为 <skill>/config/config.yaml，可用 --config 或环境变量
FOUR0S_CONFIG 覆盖。

实现按职责拆在 scripts/four0s/ 包里（constants / config / cca / issues /
hub / gitops / report），本文件只做参数解析与流程编排。

退出码：0 成功；1 扫描到非 0 问题（仅 --fail-on-leak）；2 配置不完整；
3 凭据无效；4 运行过程中出错。
"""

import argparse
import io
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# 以脚本方式运行（python3 scripts/scan_leaks.py）时，保证 scripts/ 在 sys.path 中
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from four0s.cca import check_auth  # noqa: E402
from four0s.config import ConfigError, enabled_projects, load_config, project_index, validate_config  # noqa: E402
from four0s.constants import DEFAULT_CONFIG_PATH, DEFAULT_HTTP_TIMEOUT, SKILL_ROOT  # noqa: E402
from four0s.gitops import attach_committers, sync_repositories  # noqa: E402
from four0s.hub import collect_hub, print_hub_report, summarize_hub  # noqa: E402
from four0s.issues import get_cov_leaks, get_kw_leaks, normalize_line_numbers  # noqa: E402
from four0s.report import print_leak_table, resolve_output_dir, summarize, write_outputs  # noqa: E402

__all__ = ["main", "build_parser", "DEFAULT_CONFIG_PATH", "SKILL_ROOT", "DEFAULT_HTTP_TIMEOUT"]


def _report_config_errors(errors):
    print("配置检查：不通过")
    for error in errors:
        print(f"  - {error}")
    print("请补全配置文件后重试（字段说明见 config/config.example.yaml）。")


def cmd_check(cfg, _args):
    print(f"配置文件：{cfg['_config_path']}")
    errors = validate_config(cfg)
    if errors:
        _report_config_errors(errors)
        return 2
    print("配置检查：通过（平台、项目、分支、任务 ID 均已配置）")

    result = check_auth(cfg)
    if not result["ok"]:
        print(f"凭据检查：不通过 —— {result['reason']}")
        return 3
    print(
        f"凭据检查：有效（校验任务 {result['task']}，"
        f"最近一次执行 {result.get('executionShortId')}，状态 {result.get('executionStatus')}）"
    )
    return 0


def cmd_check_auth(cfg, _args):
    from four0s.config import is_placeholder
    from four0s.constants import ENV_EMP_NO, ENV_UAC_TOKEN

    if is_placeholder(cfg["cca"].get("emp_no")) or is_placeholder(cfg["cca"].get("uac_token")):
        print(
            "凭据检查：不通过 —— cca.emp_no / cca.uac_token 未配置"
            "（可用 OpenClaw 注入的 coclaw_empno / coclaw_token，"
            f"或环境变量 {ENV_EMP_NO} / {ENV_UAC_TOKEN}，或命令行 --emp-no / --uac-token）"
        )
        return 2
    result = check_auth(cfg)
    if not result["ok"]:
        print(f"凭据检查：不通过 —— {result['reason']}")
        return 3
    print(f"凭据检查：有效（校验任务 {result['task']}）")
    return 0


def _scan_code_issues(cfg, projects, warnings):
    """抓取 Klocwork / Coverity 缺陷（不含本地仓库同步）。"""
    leaks = []
    for project in projects:
        name = str(project.get("name") or "")
        try:
            kw_leaks = get_kw_leaks(cfg, project)
        except Exception as exc:
            kw_leaks = []
            warnings.append(f"{name} Klocwork 抓取失败：{exc}")
        try:
            cov_leaks = get_cov_leaks(cfg, project)
        except Exception as exc:
            cov_leaks = []
            warnings.append(f"{name} Coverity 抓取失败：{exc}")
        print(f"  - {name}: Klocwork {len(kw_leaks)} 个，Coverity {len(cov_leaks)} 个")
        leaks.extend(kw_leaks + cov_leaks)

    # 校验 CCA 返回的仓库名与配置是否一致，避免任务 ID 配错导致修错仓库
    configured = set(project_index(cfg))
    for leak in leaks:
        repo = leak["projectName"]
        if repo != leak["configuredProject"]:
            warnings.append(
                f"仓库名与配置不一致：任务属于 {leak['configuredProject']}，"
                f"但缺陷文件路径指向 {repo}（请检查 projects[].kw_id/coverity_id）"
            )
        elif repo not in configured:
            warnings.append(f"缺陷来自未配置的仓库 {repo}，无法定位本地代码")
    return leaks


def _build_payload(cfg, auth, projects, leaks, warnings, hub):
    summary = summarize(cfg, leaks)
    summary["hub"] = summarize_hub(hub)
    return {
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "configPath": cfg["_config_path"],
        "auth": auth,
        "projects": [
            {
                "name": str(p.get("name") or ""),
                "localPath": str(p.get("local_path") or ""),
                "branch": str(p.get("branch") or ""),
                "kwId": str(p.get("kw_id") or ""),
                "coverityId": str(p.get("coverity_id") or ""),
            }
            for p in projects
        ],
        "summary": summary,
        "warnings": warnings,
        "hub": hub,
        "leaks": leaks,
    }


def cmd_scan(cfg, args):
    print(f"配置文件：{cfg['_config_path']}")
    errors = validate_config(cfg)
    if errors:
        _report_config_errors(errors)
        return 2
    print("配置检查：通过")

    auth = check_auth(cfg)
    if not auth["ok"]:
        print(f"凭据检查：不通过 —— {auth['reason']}")
        print(
            "请在 OpenClaw 中运行（读取注入的 coclaw_empno/coclaw_token），"
            "或设置环境变量 FOUR0S_CCA_EMP_NO/FOUR0S_CCA_UAC_TOKEN，"
            "或 config.yaml 的 cca.emp_no/cca.uac_token 提供有效凭据后重试。"
        )
        return 3
    print(f"凭据检查：有效（校验任务 {auth['task']}）")

    projects = enabled_projects(cfg, set(args.project) if args.project else None)
    if not projects:
        print("没有匹配到可扫描的项目，请检查 projects 配置与 --project 参数。")
        return 2
    print(f"开始扫描 {len(projects)} 个项目：{', '.join(str(p['name']) for p in projects)}")

    warnings = []
    leaks = _scan_code_issues(cfg, projects, warnings)
    normalize_line_numbers(leaks)
    if not args.no_sync:
        synced, sync_failures = sync_repositories(cfg, projects)
        warnings.extend(sync_failures)
    else:
        print("[sync] 本次使用 --no-sync，跳过 stash/pull/checkout")
        synced = set()
    attach_committers(cfg, leaks, synced)

    hub = None
    if not args.no_hub:
        hub = collect_hub(
            cfg,
            version_name=args.hub_version,
            extra_tasks=args.hub_task,
            download=not args.no_hub_download,
        )

    payload = _build_payload(cfg, auth, projects, leaks, warnings, hub)
    out_dir = resolve_output_dir(cfg, args.out_dir)
    output_paths = write_outputs(cfg, payload, out_dir)

    print("")
    print(f"结果输出目录：{out_dir}")
    print("扫描结果（4个0 口径）：")
    for target in payload["summary"]["targets"]:
        print(f"  {target['tool']} {target['priority']}: {target['count']}")
    print(f"  合计未处理高优先级问题：{payload['summary']['total']}")
    print_leak_table(leaks)
    if hub:
        print_hub_report(hub)
    if warnings:
        print("")
        print("警告：")
        for warning in warnings:
            print(f"  - {warning}")
    print("")
    for path in output_paths:
        print(f"结果已写入：{path}")
    if args.json:
        print("")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.fail_on_leak and payload["summary"]["total"] > 0:
        return 1
    return 0


def cmd_hub(cfg, args):
    """只跑 Hub：版本统计 +（可选）在线下载报告 + 组件明细解析。"""
    print(f"配置文件：{cfg['_config_path']}")
    auth = check_auth(cfg) if not args.no_auth else {"ok": True, "reason": "跳过鉴权校验"}
    hub = collect_hub(
        cfg,
        version_name=args.version,
        extra_tasks=args.task,
        download=not args.no_download,
        quiet_when_clean=False,
    )
    print_hub_report(hub, verbose=True)

    payload = {
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "configPath": cfg["_config_path"],
        "auth": auth,
        "summary": {"hub": summarize_hub(hub)},
        "warnings": hub.get("errors") or [],
        "hub": hub,
        "leaks": [],
    }
    out_dir = resolve_output_dir(cfg, args.out_dir)
    output_paths = write_outputs(cfg, payload, out_dir, include_leaks_csv=False)
    print("")
    for path in output_paths:
        print(f"结果已写入：{path}")
    if args.json:
        print("")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="scan_leaks.py",
        description=(
            "CCA「4个0」问题扫描：校验配置与凭据，抓取 Klocwork/Coverity "
            "未处理高优先级问题，并（可选）汇总 Hub 开源组件漏洞"
            "（走 CCA 开放 API，仅需 X-Emp-No + X-Uac-Token）。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("FOUR0S_CONFIG") or str(DEFAULT_CONFIG_PATH),
        help=f"配置文件路径（默认 {DEFAULT_CONFIG_PATH}）",
    )
    parser.add_argument(
        "--emp-no",
        help="CCA 员工号（X-Emp-No），默认取环境变量（含 OpenClaw 注入）或配置 cca.emp_no",
    )
    parser.add_argument(
        "--uac-token",
        help="CCA UAC token（X-Uac-Token），默认取环境变量（含 OpenClaw 注入）或配置 cca.uac_token",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("check", help="校验配置完整性并验证凭据")
    subparsers.add_parser("check-auth", help="只验证凭据（X-Emp-No/X-Uac-Token）是否有效")
    scan = subparsers.add_parser("scan", help="扫描缺陷并输出结果（默认子命令）")
    scan.add_argument("--project", action="append", help="只扫描指定项目，可重复")
    scan.add_argument("--no-sync", action="store_true", help="跳过 stash/pull/checkout")
    scan.add_argument(
        "--out-dir",
        help="结果输出目录（默认取 output.dir，未配置时写到当前工作目录）",
    )
    scan.add_argument("--json", action="store_true", help="在标准输出额外打印完整 JSON")
    scan.add_argument("--fail-on-leak", action="store_true", help="发现问题时以退出码 1 结束")
    scan.add_argument("--no-hub", action="store_true", help="跳过 Hub 统计与报告解析")
    scan.add_argument("--hub-version", help="Hub 版本号（默认取 cca.hub.version_name）")
    scan.add_argument(
        "--hub-task",
        action="append",
        help="Hub 任务链接或 task_id（可重复，会与 projects[].hub_id 合并后自动下载报告）",
    )
    scan.add_argument(
        "--no-hub-download", action="store_true", help="不下载 Hub 任务报告（跳过组件明细）"
    )

    hub = subparsers.add_parser("hub", help="只跑 Hub：版本统计 + 报告 Excel 组件明细")
    hub.add_argument("--version", help="Hub 版本号（默认取 cca.hub.version_name）")
    hub.add_argument(
        "--task",
        action="append",
        help="Hub 任务链接或 task_id（可重复，会与 projects[].hub_id 合并后自动下载报告）",
    )
    hub.add_argument(
        "--no-download", action="store_true", help="不下载 Hub 任务报告（跳过组件明细）"
    )
    hub.add_argument(
        "--out-dir",
        help="结果输出目录（默认取 output.dir，未配置时写到当前工作目录）",
    )
    hub.add_argument("--json", action="store_true", help="在标准输出额外打印完整 JSON")
    hub.add_argument("--no-auth", action="store_true", help="跳过凭据校验（仅本地解析 Excel 时可用）")
    return parser


def _enable_line_buffering(stream=None):
    """让进度日志实时输出。

    输出被管道或重定向时 Python 默认用块缓冲，长任务的进度日志会攒到最后才
    出现。sys.stdout 的标注类型是 TextIO，而 reconfigure 只定义在
    io.TextIOWrapper 上，所以先做类型判断再调用。
    """
    stream = stream if stream is not None else sys.stdout
    if isinstance(stream, io.TextIOWrapper):
        try:
            stream.reconfigure(line_buffering=True)
        except (ValueError, OSError):
            pass


def main(argv=None):
    _enable_line_buffering()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        args.command = "scan"
        args.project = None
        args.no_sync = False
        args.out_dir = None
        args.json = False
        args.fail_on_leak = False
        args.no_hub = False
        args.hub_version = None
        args.hub_task = None
        args.no_hub_download = False

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(str(exc))
        return 2
    # 命令行凭据优先级最高
    if getattr(args, "emp_no", None):
        cfg["cca"]["emp_no"] = args.emp_no.strip()
    if getattr(args, "uac_token", None):
        cfg["cca"]["uac_token"] = args.uac_token.strip()

    handlers = {
        "check": cmd_check,
        "check-auth": cmd_check_auth,
        "scan": cmd_scan,
        "hub": cmd_hub,
    }
    try:
        return handlers[args.command](cfg, args)
    except RuntimeError as exc:
        print(f"执行失败：{exc}")
        return 4


if __name__ == "__main__":
    sys.exit(main())
