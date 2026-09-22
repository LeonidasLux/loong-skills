#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CCA「4个0」问题扫描脚本。

「4个0」指以下 4 类问题的数量都必须为 0（口径可在配置文件中调整）：
    1. Klocwork Critical
    2. Klocwork Error
    3. Coverity High
    4. Coverity Medium

子命令：
    check         校验配置完整性，并用 CCA 接口验证 cookie 是否有效
    check-cookie  只验证 cookie 是否有效
    scan          扫描项目并输出 JSON/CSV 结果（默认子命令）

用法示例：
    python3 scripts/scan_leaks.py check
    python3 scripts/scan_leaks.py scan --project zte-aiop-aiservice-appui
    python3 scripts/scan_leaks.py scan --no-sync --json
    python3 scripts/scan_leaks.py scan --fail-on-leak

配置文件默认路径为 <skill>/config/config.yaml，可用 --config 或环境变量
FOUR0S_CONFIG 覆盖；凭据可用 FOUR0S_CCA_COOKIE / FOUR0S_CCA_CSRF 覆盖，
避免把 cookie 写进文件。

退出码：0 成功；1 扫描到非 0 问题（仅 --fail-on-leak）；2 配置不完整；
3 cookie 无效；4 运行过程中出错。
"""

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
import yaml

SKILL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = SKILL_ROOT / "config" / "config.yaml"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/89.0.4389.90 Safari/537.36"
)
CCA_AUTHORITY = "cca.zte.com.cn"

# CCA 缺陷列表接口是 datatables 风格，必须带完整的 columns 参数；
# 参数不全接口会直接返回 400 Bad Request。
EXECUTION_COLUMNS = [
    ("name", True),
    ("startTime", True),
    ("endTime", True),
    ("waitTime", True),
    ("executionTime", True),
    ("buildVersion", True),
    ("status", False),
    ("progress", False),
    ("remark", False),
    ("scanMethod", False),
    ("shortId", False),
    ("shortId", False),
]
ISSUE_COLUMNS = [
    ("sn", True),
    ("title", True),
    ("priorityDescription", True),
    ("3", True),
    ("4", False),
    ("5", False),
]

# 判定「未配置」的占位符，避免把模板值当成真实配置
PLACEHOLDER_VALUES = {"", "todo", "tbd", "none", "null", "cookie", "csrf", "change-me"}
PLACEHOLDER_PREFIXES = ("<", "your-", "xxx")
# 命中这些关键字的重定向说明会话已失效
LOGIN_HINTS = ("authorizeurl", "/login", "sso")

CSV_HEADER = ["提交者", "代码库", "文件路径", "行数", "漏洞描述", "漏洞类型", "漏洞ID", "漏洞级别", "任务链接"]


class ConfigError(RuntimeError):
    """配置文件缺失或不可用。"""


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def _is_placeholder(value):
    text = str(value or "").strip()
    if text.lower() in PLACEHOLDER_VALUES:
        return True
    return text.lower().startswith(PLACEHOLDER_PREFIXES)


def load_config(config_path):
    """加载并规范化配置；凭据允许用环境变量覆盖。"""
    path = Path(config_path).expanduser().resolve()
    if not path.is_file():
        raise ConfigError(
            f"配置文件不存在：{path}\n"
            "请复制 config/config.example.yaml 为 config/config.yaml 并填写后重试。"
        )
    try:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置文件不是合法 YAML：{path}\n{exc}") from exc
    if not isinstance(cfg, dict):
        raise ConfigError(f"配置文件内容应为键值对：{path}")

    for key in ("cca", "sync", "output", "request"):
        if not isinstance(cfg.get(key), dict):
            cfg[key] = {}
    if not isinstance(cfg.get("projects"), list):
        cfg["projects"] = []

    cookie = os.environ.get("FOUR0S_CCA_COOKIE") or cfg["cca"].get("cookie")
    csrf = os.environ.get("FOUR0S_CCA_CSRF") or cfg["cca"].get("csrf")
    cfg["cca"]["cookie"] = (cookie or "").strip()
    cfg["cca"]["csrf"] = (csrf or "").strip()
    cfg["_config_path"] = str(path)
    return cfg


def validate_config(cfg):
    """返回配置问题列表；空列表表示配置完整（不校验 cookie 是否仍然有效）。"""
    errors = []
    cca = cfg["cca"]
    if _is_placeholder(cca.get("base_url")):
        errors.append("cca.base_url 未配置")
    if _is_placeholder(cca.get("project_id")):
        errors.append("cca.project_id 未配置（任务链接中 /workbench/project/<project_id>/ 一段）")
    if _is_placeholder(cca.get("cookie")):
        errors.append("cca.cookie 未配置（可用环境变量 FOUR0S_CCA_COOKIE 提供）")
    if _is_placeholder(cca.get("csrf")):
        errors.append("cca.csrf 未配置（可用环境变量 FOUR0S_CCA_CSRF 提供）")
    if not (cca.get("kw") or {}).get("priorities"):
        errors.append("cca.kw.priorities 未配置")
    if not (cca.get("coverity") or {}).get("priorities"):
        errors.append("cca.coverity.priorities 未配置")

    projects = cfg["projects"]
    if not projects:
        errors.append("projects 未配置任何项目")
    seen = set()
    for index, project in enumerate(projects):
        if not isinstance(project, dict):
            errors.append(f"projects[{index}] 应为键值对")
            continue
        where = f"projects[{index}]"
        name = str(project.get("name") or "").strip()
        if not name:
            errors.append(f"{where}.name 未配置")
        elif name in seen:
            errors.append(f"{where}.name 重复：{name}")
        else:
            seen.add(name)
            where = f"projects[{index}] ({name})"

        local_path = str(project.get("local_path") or "").strip()
        if _is_placeholder(local_path):
            errors.append(f"{where}.local_path 未配置")
        elif not Path(local_path).is_dir():
            errors.append(f"{where}.local_path 不存在或不是目录：{local_path}")
        if not str(project.get("branch") or "").strip():
            errors.append(f"{where}.branch 未配置")
        if _is_placeholder(project.get("kw_id")) and _is_placeholder(project.get("coverity_id")):
            errors.append(f"{where} 至少需要配置 kw_id 或 coverity_id 之一")
    return errors


def _enabled_projects(cfg, only=None):
    projects = []
    for project in cfg["projects"]:
        if not isinstance(project, dict):
            continue
        if not project.get("enabled", True):
            continue
        name = str(project.get("name") or "").strip()
        if only and name not in only:
            continue
        projects.append(project)
    return projects


def _project_index(cfg):
    return {str(p.get("name") or "").strip(): p for p in cfg["projects"] if isinstance(p, dict)}


def _first_task_id(cfg, projects=None):
    for project in projects or cfg["projects"]:
        for key in ("kw_id", "coverity_id"):
            task_id = str(project.get(key) or "").strip()
            if task_id:
                return task_id
    return ""


# --------------------------------------------------------------------------- #
# CCA 接口
# --------------------------------------------------------------------------- #
def _base_url(cca):
    return str(cca.get("base_url") or "").rstrip("/")


def build_session(cfg):
    session = requests.Session()
    session.headers.update(
        {
            "user-agent": USER_AGENT,
            "cookie": cfg["cca"]["cookie"],
            "authority": CCA_AUTHORITY,
            "accept": "application/json, text/javascript, */*; q=0.01",
            "x-csrf-token": cfg["cca"]["csrf"],
        }
    )
    return session


def _request(session, url, cfg, params=None, allow_redirects=True):
    """带超时与重试的 GET；不会打印 cookie。"""
    request_cfg = cfg.get("request") or {}
    timeout = request_cfg.get("timeout", 60)
    retries = int(request_cfg.get("retries", 2) or 0)
    last_error = None
    for attempt in range(retries + 1):
        try:
            return session.get(
                url, params=params, timeout=timeout, allow_redirects=allow_redirects
            )
        except requests.RequestException as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"请求 CCA 失败：{url}（{type(last_error).__name__}: {last_error}）")


def _datatables_columns(columns):
    params = []
    for index, (data, orderable) in enumerate(columns):
        params += [
            (f"columns[{index}][data]", data),
            (f"columns[{index}][name]", ""),
            (f"columns[{index}][searchable]", "true"),
            (f"columns[{index}][orderable]", "true" if orderable else "false"),
            (f"columns[{index}][search][value]", ""),
            (f"columns[{index}][search][regex]", "false"),
        ]
    return params


def _executions_params(length=200):
    return (
        [("draw", "1")]
        + _datatables_columns(EXECUTION_COLUMNS)
        + [
            ("order[0][column]", "1"),
            ("order[0][dir]", "desc"),
            ("start", "0"),
            ("length", str(length)),
            ("search[value]", ""),
            ("search[regex]", "false"),
            ("_", str(int(time.time() * 1000))),
        ]
    )


def _issues_params(tool, priority, draw="2", order_column="2", length=200):
    return (
        [("reportId", ""), ("tool", tool), ("priority", priority), ("draw", draw)]
        + _datatables_columns(ISSUE_COLUMNS)
        + [
            ("order[0][column]", order_column),
            ("order[0][dir]", "asc"),
            ("start", "0"),
            ("length", str(length)),
            ("search[value]", ""),
            ("search[regex]", "false"),
            ("_", str(int(time.time() * 1000))),
        ]
    )


def _executions_url(cfg, task_id):
    cca = cfg["cca"]
    return f"{_base_url(cca)}/workbench/project/{cca['project_id']}/task/{task_id}/executions"


def _issues_url(cfg, task_id, record_id):
    cca = cfg["cca"]
    return (
        f"{_base_url(cca)}/workbench/project/{cca['project_id']}"
        f"/task/{task_id}/execution/{record_id}/issues"
    )


def check_cookie(cfg, task_id=None):
    """调用 CCA 接口验证 cookie 是否有效（不跟随重定向，登录跳转即视为失效）。"""
    cca = cfg["cca"]
    task_id = task_id or _first_task_id(cfg)
    if not task_id:
        return {
            "ok": False,
            "reason": "没有可用的任务 ID，无法验证 cookie：请先配置 projects[].kw_id 或 coverity_id",
        }
    url = _executions_url(cfg, task_id)
    session = build_session(cfg)
    try:
        resp = _request(
            session, url, cfg, params=_executions_params(length=1), allow_redirects=False
        )
    except RuntimeError as exc:
        return {"ok": False, "reason": str(exc), "task": task_id}

    location = resp.headers.get("location", "")
    if resp.status_code in (301, 302, 303, 307, 308) and any(
        hint in location.lower() for hint in LOGIN_HINTS
    ):
        return {
            "ok": False,
            "reason": "cookie 已失效：请求被重定向到登录页，请更新 config.yaml 中的 cca.cookie 与 cca.csrf",
            "status": resp.status_code,
            "task": task_id,
        }
    if resp.status_code in (401, 403):
        return {
            "ok": False,
            "reason": f"cookie 无效或权限不足（HTTP {resp.status_code}）",
            "status": resp.status_code,
            "task": task_id,
        }
    if resp.status_code != 200:
        return {
            "ok": False,
            "reason": f"CCA 接口返回异常状态码 {resp.status_code}",
            "status": resp.status_code,
            "body": resp.text[:200],
            "task": task_id,
        }
    try:
        payload = resp.json()
    except ValueError:
        return {
            "ok": False,
            "reason": "CCA 接口未返回 JSON，可能被登录页或网关拦截",
            "body": resp.text[:200],
            "task": task_id,
        }
    if not isinstance(payload, dict) or "data" not in payload:
        return {
            "ok": False,
            "reason": "CCA 接口返回内容缺少 data 字段，cookie 可能已失效",
            "body": resp.text[:200],
            "task": task_id,
        }
    return {
        "ok": True,
        "reason": "cookie 有效",
        "status": 200,
        "task": task_id,
        "executionRecords": len(payload.get("data") or []),
    }


def get_latest_record_id(session, cfg, task_id, label):
    """取任务最近一次执行的记录 ID（shortId）。"""
    resp = _request(
        session, _executions_url(cfg, task_id), cfg, params=_executions_params(length=1)
    )
    if resp.status_code != 200 or not resp.text.strip():
        raise RuntimeError(
            f"{label} executions 请求失败：task={task_id}, status={resp.status_code}, "
            f"body[:300]={resp.text[:300]!r}"
        )
    records = (json.loads(resp.text) or {}).get("data") or []
    if not records:
        raise RuntimeError(f"{label} 任务 {task_id} 没有任何执行记录")
    return records[0]["shortId"]


def fetch_issues(session, cfg, task_id, record_id, tool, priority, draw="2"):
    resp = _request(
        session,
        _issues_url(cfg, task_id, record_id),
        cfg,
        params=_issues_params(tool, priority, draw=draw),
    )
    if resp.status_code != 200 or not resp.text.strip():
        raise RuntimeError(
            f"CCA issues 请求失败：task={task_id}, priority={priority}, "
            f"status={resp.status_code}, body[:300]={resp.text[:300]!r}"
        )
    try:
        payload = json.loads(resp.text)
    except ValueError as exc:
        raise RuntimeError(
            f"CCA issues 返回非 JSON：task={task_id}, priority={priority}, "
            f"body[:300]={resp.text[:300]!r}"
        ) from exc
    return payload.get("data") or []


# --------------------------------------------------------------------------- #
# 缺陷解析
# --------------------------------------------------------------------------- #
def get_line_number(snippets):
    if not snippets:
        return "Various"
    return snippets[0].get("lineStart", "Various")


def _repo_name(full_path):
    parts = str(full_path or "").split("/")
    return parts[3] if len(parts) > 3 else ""


def _relative_path(full_path):
    """CCA 路径形如 /local/code/<repo>/src/x.js，返回仓库内相对路径 src/x.js。"""
    parts = str(full_path or "").split("/")
    return "/".join(parts[4:]) if len(parts) > 4 else ""


def _rule_name(classification):
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
        "rule": _rule_name(issue.get("title")),
        "description": issue.get("description"),
        "committer": "",
        "filePath": full_path,
        "relativePath": _relative_path(full_path),
        "lineNumber": get_line_number(file_info.get("snippets")),
        "relatedWorkItemId": "",
        "type": leak_type,
        "projectName": _repo_name(full_path) or str(project.get("name") or ""),
        "belongTeam": "",
        "configuredProject": str(project.get("name") or ""),
        "taskInfo": (
            f"{_base_url(cca)}/workbench/project/{cca['project_id']}"
            f"/task/{task_id}/execution/{record_id}/issue/{issue.get('sn')}"
        ),
    }


def get_kw_leaks(session, cfg, project):
    """抓取单个项目的 Klocwork 缺陷（仅未处理的 Critical/Error）。"""
    task_id = str(project.get("kw_id") or "").strip()
    if not task_id:
        return []
    kw_cfg = cfg["cca"].get("kw") or {}
    tool = kw_cfg.get("tool", "kwServer-2020.4")
    status_filter = set(kw_cfg.get("status_filter") or [])
    record_id = get_latest_record_id(session, cfg, task_id, f"{project.get('name')} Klocwork")
    leaks = []
    for level in kw_cfg.get("priorities") or []:
        for issue in fetch_issues(session, cfg, task_id, record_id, tool, level, draw="2"):
            if status_filter and issue.get("status") not in status_filter:
                continue
            leaks.append(build_leak(issue, project, "Klocwork", cfg, task_id, record_id))
    return leaks


def get_cov_leaks(session, cfg, project):
    """抓取单个项目的 Coverity 缺陷（仅未分类的 High/Medium）。"""
    task_id = str(project.get("coverity_id") or "").strip()
    if not task_id:
        return []
    cov_cfg = cfg["cca"].get("coverity") or {}
    tool = cov_cfg.get("tool", "coverity-20230302")
    only_unclassified = cov_cfg.get("only_unclassified", True)
    record_id = get_latest_record_id(
        session, cfg, task_id, f"{project.get('name')} Coverity"
    )
    leaks = []
    for level in cov_cfg.get("priorities") or []:
        for issue in fetch_issues(session, cfg, task_id, record_id, tool, level, draw="3"):
            if only_unclassified and issue.get("classification") != "Unclassified":
                continue
            leaks.append(build_leak(issue, project, "Coverity", cfg, task_id, record_id))
    return leaks


def normalize_line_numbers(leaks):
    """行号为 Various 时，按原脚本逻辑从缺陷标题里抽取首个数字。"""
    for leak in leaks:
        if leak["lineNumber"] == "Various":
            matchers = re.findall(r"\d+", str(leak.get("classification") or ""))
            if matchers:
                leak["lineNumber"] = matchers[0]


# --------------------------------------------------------------------------- #
# 本地仓库同步与提交者分析
# --------------------------------------------------------------------------- #
def execute_command(command, work_dir):
    result = subprocess.run(
        command, cwd=work_dir, universal_newlines=True, stdout=subprocess.PIPE
    )
    if result.returncode == 0:
        return result.stdout
    print(result.stdout)
    raise RuntimeError("command exec failure")


def sync_repositories(cfg, projects, log=print):
    """按项目同步本地仓库（stash -> pull -> checkout -> pull）。

    返回 (已同步的项目名集合, 同步失败信息列表)。同步失败的仓库仍会继续扫描，
    只是不再做 commit 归属分析，避免一个仓库卡住整轮扫描。
    """
    sync_cfg = cfg.get("sync") or {}
    if not sync_cfg.get("enabled", True):
        log("[sync] 已禁用本地仓库同步（sync.enabled=false）")
        return set(), []
    try:
        import git
    except ImportError as exc:  # pragma: no cover - 环境缺依赖时给出明确提示
        raise RuntimeError("缺少 GitPython，无法同步本地仓库：pip install gitpython") from exc

    synced, failures = set(), []
    for project in projects:
        name = str(project.get("name") or "").strip()
        code_path = str(project.get("local_path") or "").strip()
        branch = str(project.get("branch") or "").strip()
        try:
            repo = git.Repo(code_path)
            if sync_cfg.get("stash_before_pull", True) and repo.is_dirty(untracked_files=False):
                repo.git.stash()
                log(f"[sync] {name}: 已 stash 本地改动")
            if sync_cfg.get("pull", True):
                repo.remotes.origin.pull()
            repo.git.checkout(branch)
            if sync_cfg.get("pull", True):
                repo.remotes.origin.pull()
            synced.add(name)
            log(f"[sync] {name}: 已切换到 {branch} 并更新代码")
        except Exception as exc:  # git 异常类型多，统一兜底
            message = f"{name}: 同步失败（{type(exc).__name__}: {exc}）"
            failures.append(message)
            log(f"[sync] {message}")
    return synced, failures


def attach_committers(cfg, leaks, synced_projects, log=print):
    """用 git blame 补全缺陷提交者（找不到时保持为空）。"""
    sync_cfg = cfg.get("sync") or {}
    if not sync_cfg.get("blame", True):
        return
    try:
        import git
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("缺少 GitPython：pip install gitpython") from exc

    index = _project_index(cfg)
    grouped = {}
    for leak in leaks:
        grouped.setdefault(leak["projectName"], []).append(leak)

    for project_name, items in grouped.items():
        project = index.get(project_name)
        if not project or project_name not in synced_projects:
            continue
        code_path = str(project.get("local_path") or "").strip()
        try:
            repo = git.Repo(code_path)
        except Exception as exc:
            log(f"[blame] {project_name}: 打开本地仓库失败（{exc}）")
            continue
        for leak in items:
            line_number = leak.get("lineNumber")
            relative = leak.get("relativePath")
            if not relative or line_number in (None, "", "Various"):
                continue
            command = ["git", "blame", "-L", f"{line_number},{line_number}", "--", "./" + relative]
            try:
                output = execute_command(command, code_path).strip()
                if output:
                    commit = repo.commit(output.split()[0])
                    leak["committer"] = commit.committer.name
            except Exception:
                log(f"[blame] {project_name}: {relative}:{line_number} 未找到提交者")


# --------------------------------------------------------------------------- #
# 结果汇总与落盘
# --------------------------------------------------------------------------- #
def summarize(cfg, leaks):
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
    """按原脚本格式输出 CSV（空集也会写出表头，便于确认扫描已执行）。"""
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


def resolve_output_dir(cfg, override=None):
    """结果目录优先级：--out-dir > output.dir > 执行命令时的当前目录。"""
    if override:
        return Path(override).expanduser().resolve()
    configured = str((cfg.get("output") or {}).get("dir") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.cwd()


def write_outputs(cfg, payload, out_dir=None):
    out_dir = Path(out_dir or resolve_output_dir(cfg))
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = out_dir / f"leaks_result_{stamp}.json"
    csv_path = out_dir / f"leaks_result_{stamp}.csv"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    write_bug(payload["leaks"], csv_path)
    return json_path, csv_path


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


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #
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

    result = check_cookie(cfg)
    if not result["ok"]:
        print(f"cookie 检查：不通过 —— {result['reason']}")
        return 3
    print(
        f"cookie 检查：有效（校验任务 {result['task']}，"
        f"返回 {result.get('executionRecords', 0)} 条执行记录）"
    )
    return 0


def cmd_check_cookie(cfg, _args):
    if _is_placeholder(cfg["cca"].get("cookie")):
        print("cookie 检查：不通过 —— cca.cookie 未配置")
        return 2
    result = check_cookie(cfg)
    if not result["ok"]:
        print(f"cookie 检查：不通过 —— {result['reason']}")
        return 3
    print(f"cookie 检查：有效（校验任务 {result['task']}）")
    return 0


def cmd_scan(cfg, args):
    print(f"配置文件：{cfg['_config_path']}")
    errors = validate_config(cfg)
    if errors:
        _report_config_errors(errors)
        return 2
    print("配置检查：通过")

    auth = check_cookie(cfg)
    if not auth["ok"]:
        print(f"cookie 检查：不通过 —— {auth['reason']}")
        print("请更新 config.yaml 中的 cca.cookie 与 cca.csrf 后重试。")
        return 3
    print(f"cookie 检查：有效（校验任务 {auth['task']}）")

    projects = _enabled_projects(cfg, set(args.project) if args.project else None)
    if not projects:
        print("没有匹配到可扫描的项目，请检查 projects 配置与 --project 参数。")
        return 2
    print(f"开始扫描 {len(projects)} 个项目：{', '.join(str(p['name']) for p in projects)}")

    session = build_session(cfg)
    leaks, warnings = [], []
    for project in projects:
        name = str(project.get("name") or "")
        try:
            kw_leaks = get_kw_leaks(session, cfg, project)
        except Exception as exc:
            kw_leaks = []
            warnings.append(f"{name} Klocwork 抓取失败：{exc}")
        try:
            cov_leaks = get_cov_leaks(session, cfg, project)
        except Exception as exc:
            cov_leaks = []
            warnings.append(f"{name} Coverity 抓取失败：{exc}")
        print(f"  - {name}: Klocwork {len(kw_leaks)} 个，Coverity {len(cov_leaks)} 个")
        leaks.extend(kw_leaks + cov_leaks)

    # 校验 CCA 返回的仓库名与配置是否一致，避免任务 ID 配错导致修错仓库
    configured = set(_project_index(cfg))
    for leak in leaks:
        repo = leak["projectName"]
        if repo != leak["configuredProject"]:
            warnings.append(
                f"仓库名与配置不一致：任务属于 {leak['configuredProject']}，"
                f"但缺陷文件路径指向 {repo}（请检查 projects[].kw_id/coverity_id）"
            )
        elif repo not in configured:
            warnings.append(f"缺陷来自未配置的仓库 {repo}，无法定位本地代码")

    normalize_line_numbers(leaks)
    if not args.no_sync:
        synced, sync_failures = sync_repositories(cfg, projects)
        warnings.extend(sync_failures)
    else:
        print("[sync] 本次使用 --no-sync，跳过 stash/pull/checkout")
        synced = set()
    attach_committers(cfg, leaks, synced)

    summary = summarize(cfg, leaks)
    payload = {
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
        "leaks": leaks,
    }
    out_dir = resolve_output_dir(cfg, args.out_dir)
    json_path, csv_path = write_outputs(cfg, payload, out_dir)

    print("")
    print(f"结果输出目录：{out_dir}")
    print("扫描结果（4个0 口径）：")
    for target in summary["targets"]:
        print(f"  {target['tool']} {target['priority']}: {target['count']}")
    print(f"  合计未处理高优先级问题：{summary['total']}")
    print_leak_table(leaks)
    if warnings:
        print("")
        print("警告：")
        for warning in warnings:
            print(f"  - {warning}")
    print("")
    print(f"结果已写入：{json_path}")
    print(f"结果已写入：{csv_path}")
    if args.json:
        print("")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.fail_on_leak and summary["total"] > 0:
        return 1
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="scan_leaks.py",
        description="CCA「4个0」问题扫描：校验配置与 cookie，抓取 Klocwork/Coverity 未处理高优先级问题。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("FOUR0S_CONFIG") or str(DEFAULT_CONFIG_PATH),
        help=f"配置文件路径（默认 {DEFAULT_CONFIG_PATH}）",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("check", help="校验配置完整性并验证 cookie")
    subparsers.add_parser("check-cookie", help="只验证 cookie 是否有效")
    scan = subparsers.add_parser("scan", help="扫描缺陷并输出结果（默认子命令）")
    scan.add_argument("--project", action="append", help="只扫描指定项目，可重复")
    scan.add_argument("--no-sync", action="store_true", help="跳过 stash/pull/checkout")
    scan.add_argument(
        "--out-dir",
        help="结果输出目录（默认取 output.dir，未配置时写到当前工作目录）",
    )
    scan.add_argument("--json", action="store_true", help="在标准输出额外打印完整 JSON")
    scan.add_argument("--fail-on-leak", action="store_true", help="发现问题时以退出码 1 结束")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        args.command = "scan"
        args.project = None
        args.no_sync = False
        args.out_dir = None
        args.json = False
        args.fail_on_leak = False

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(str(exc))
        return 2

    handlers = {
        "check": cmd_check,
        "check-cookie": cmd_check_cookie,
        "scan": cmd_scan,
    }
    try:
        return handlers[args.command](cfg, args)
    except RuntimeError as exc:
        print(f"执行失败：{exc}")
        return 4


if __name__ == "__main__":
    sys.exit(main())
