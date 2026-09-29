# -*- coding: utf-8 -*-
"""结果汇总、落盘与终端输出。"""

import csv
import json
from datetime import datetime
from pathlib import Path

from .constants import CSV_HEADER


def summarize(cfg, leaks):
    """按配置口径统计 Klocwork / Coverity 的「4个0」目标。"""
    kw_cfg = cfg["cca"].get("kw") or {}
    cov_cfg = cfg["cca"].get("coverity") or {}
    targets = []
    for level in kw_cfg.get("priorities") or []:
        count = len(
            [leak for leak in leaks if leak["type"] == "Klocwork" and leak["grade"] == level]
        )
        targets.append({"tool": "Klocwork", "priority": level, "count": count})
    for level in cov_cfg.get("priorities") or []:
        count = len(
            [leak for leak in leaks if leak["type"] == "Coverity" and leak["grade"] == level]
        )
        targets.append({"tool": "Coverity", "priority": level, "count": count})

    by_project = {}
    by_type = {}
    for leak in leaks:
        by_project[leak["projectName"]] = by_project.get(leak["projectName"], 0) + 1
        by_type[leak["type"]] = by_type.get(leak["type"], 0) + 1
    return {
        "total": len(leaks),
        "allZero": all(target["count"] == 0 for target in targets),
        "targets": targets,
        "byProject": by_project,
        "byType": by_type,
    }


def write_bug(leaks, csv_path):
    """输出 CSV（空集也会写出表头，便于确认扫描已执行）。"""
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(CSV_HEADER)
        for leak in leaks:
            writer.writerow(
                [
                    leak["committer"],
                    leak["projectName"],
                    leak["filePath"],
                    leak["lineNumber"],
                    str(leak.get("classification") or "") + "。" + str(leak.get("description") or ""),
                    leak["type"],
                    leak["leakId"],
                    leak["grade"],
                    leak["taskInfo"],
                ]
            )
    return csv_path


def write_hub_csv(hub, csv_path):
    """Hub 组件明细单独一张表：任务是微服务名，组件行可直接对到 pom 依赖。"""
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(
            [
                "任务(微服务)",
                "组件",
                "组件版本",
                "组件ID",
                "漏洞等级",
                "合规等级",
                "引入方式",
                "引入路径",
                "漏洞数",
                "漏洞信息",
                "Origin id",
            ]
        )
        for item in (hub or {}).get("components") or []:
            intro = item.get("introduction") or {}
            via = "；".join(intro.get("firstLevel") or []) or "；".join(intro.get("chains") or [])
            writer.writerow(
                [
                    item.get("taskName", ""),
                    item.get("componentName", ""),
                    item.get("componentVersion", ""),
                    item.get("componentId", ""),
                    item.get("vulnerabilityLevel", ""),
                    item.get("licenseLevel", ""),
                    intro.get("kind", ""),
                    via,
                    item.get("vulnerabilityCount", 0),
                    item.get("vulnerabilities", ""),
                    item.get("originId", ""),
                ]
            )
    return csv_path


def resolve_output_dir(cfg, override=None):
    """结果目录优先级：--out-dir > output.dir > 执行命令时的当前目录。"""
    if override:
        return Path(override).expanduser().resolve()
    configured = str((cfg.get("output") or {}).get("dir") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.cwd()


def write_outputs(cfg, payload, out_dir=None, include_leaks_csv=True):
    out_dir = Path(out_dir or resolve_output_dir(cfg))
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = out_dir / f"leaks_result_{stamp}.json"
    csv_path = out_dir / f"leaks_result_{stamp}.csv"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    paths = [json_path]
    if include_leaks_csv:
        write_bug(payload["leaks"], csv_path)
        paths.append(csv_path)
    if payload.get("hub", {}).get("components"):
        hub_csv = out_dir / f"hub_components_{stamp}.csv"
        write_hub_csv(payload["hub"], hub_csv)
        paths.append(hub_csv)
    return paths


def print_leak_table(leaks):
    if not leaks:
        print("本次扫描未发现未处理的高优先级问题。")
        return
    print(f"{'项目':<34} {'类型':<9} {'级别':<8} {'文件:行':<56} {'问题类别':<36} 提交者")
    for leak in leaks:
        location = f"{leak['relativePath']}:{leak['lineNumber']}"[:56]
        print(
            f"{leak['projectName']:<34} {leak['type']:<9} {str(leak['grade']):<8} "
            f"{location:<56} {str(leak['rule'])[:36]:<36} {leak['committer']}"
        )
