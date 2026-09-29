# -*- coding: utf-8 -*-
"""Klocwork / Coverity 缺陷解析与抓取。"""

import re

from .cca import base_url, fetch_issues, get_latest_record_id


def get_line_number(snippets):
    if not snippets:
        return "Various"
    return snippets[0].get("lineStart", "Various")


def repo_name(full_path):
    parts = str(full_path or "").split("/")
    return parts[3] if len(parts) > 3 else ""


def relative_path(full_path):
    """CCA 路径形如 /local/code/<repo>/src/x.js，返回仓库内相对路径 src/x.js。"""
    parts = str(full_path or "").split("/")
    return "/".join(parts[4:]) if len(parts) > 4 else ""


def rule_name(classification):
    """CCA 标题形如 "Reference.js,186 JS.BASE.EQEQEQ"，取最后的规则名。"""
    parts = str(classification or "").split()
    return parts[-1] if parts else ""


def build_leak(issue, project, leak_type, cfg, task_id, record_id):
    file_info = issue.get("file") or {}
    full_path = file_info.get("fullName") or ""
    cca = cfg["cca"]
    return {
        "leakId": issue.get("sn"),
        "grade": issue.get("priorityDescription"),
        "status": issue.get("status"),
        "classification": issue.get("title"),
        "rule": rule_name(issue.get("title")),
        "description": issue.get("description"),
        "committer": "",
        "filePath": full_path,
        "relativePath": relative_path(full_path),
        "lineNumber": get_line_number(file_info.get("snippets")),
        "relatedWorkItemId": "",
        "type": leak_type,
        "projectName": repo_name(full_path) or str(project.get("name") or ""),
        "belongTeam": "",
        "configuredProject": str(project.get("name") or ""),
        "taskInfo": (
            f"{base_url(cca)}/workbench/project/{cca['project_id']}"
            f"/task/{task_id}/execution/{record_id}/issue/{issue.get('sn')}"
        ),
    }


def get_kw_leaks(cfg, project):
    """抓取单个项目的 Klocwork 缺陷（仅配置状态的 Critical/Error）。"""
    task_id = str(project.get("kw_id") or "").strip()
    if not task_id:
        return []
    kw_cfg = cfg["cca"].get("kw") or {}
    levels = set(kw_cfg.get("priorities") or [])
    status_filter = set(kw_cfg.get("status_filter") or [])
    record_id = get_latest_record_id(cfg, task_id, f"{project.get('name')} Klocwork")
    leaks = []
    for issue in fetch_issues(cfg, task_id, record_id):
        if issue.get("priorityDescription") not in levels:
            continue
        if status_filter and issue.get("status") not in status_filter:
            continue
        leaks.append(build_leak(issue, project, "Klocwork", cfg, task_id, record_id))
    return leaks


def get_cov_leaks(cfg, project):
    """抓取单个项目的 Coverity 缺陷（仅未分类的 High/Medium）。"""
    task_id = str(project.get("coverity_id") or "").strip()
    if not task_id:
        return []
    cov_cfg = cfg["cca"].get("coverity") or {}
    levels = set(cov_cfg.get("priorities") or [])
    only_unclassified = cov_cfg.get("only_unclassified", True)
    record_id = get_latest_record_id(cfg, task_id, f"{project.get('name')} Coverity")
    leaks = []
    for issue in fetch_issues(cfg, task_id, record_id):
        if issue.get("priorityDescription") not in levels:
            continue
        if only_unclassified and issue.get("classification") != "Unclassified":
            continue
        leaks.append(build_leak(issue, project, "Coverity", cfg, task_id, record_id))
    return leaks


def normalize_line_numbers(leaks):
    """行号为 Various 时，从缺陷标题里抽取首个数字。"""
    for leak in leaks:
        if leak["lineNumber"] == "Various":
            matchers = re.findall(r"\d+", str(leak.get("classification") or ""))
            if matchers:
                leak["lineNumber"] = matchers[0]
