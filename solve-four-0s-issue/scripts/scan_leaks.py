#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CCA「4个0」问题扫描脚本。

「4个0」指以下 4 类问题的数量都必须为 0（口径可在配置文件中调整）：
    1. Klocwork Critical
    2. Klocwork Error
    3. Coverity High
    4. Coverity Medium

子命令：
    check         校验配置完整性，并用 CCA 接口验证凭据是否有效
    check-auth    只验证凭据是否有效（旧名 check-cookie 仍可用）
    scan          扫描项目并输出 JSON/CSV 结果（默认子命令）

用法示例：
    python3 scripts/scan_leaks.py check
    python3 scripts/scan_leaks.py scan --project zte-aiop-aiservice-appui
    python3 scripts/scan_leaks.py scan --no-sync --json
    python3 scripts/scan_leaks.py scan --fail-on-leak

CCA 接口：统一调用开放 API（<base_url>/api/v2），请求头只带两个鉴权参数
X-Emp-No + X-Uac-Token，不再需要浏览器 Cookie / x-csrf-token。

凭据（员工号 + UAC token）来源，按优先级：
    1. 命令行 --emp-no / --uac-token
    2. 环境变量 FOUR0S_CCA_EMP_NO / FOUR0S_CCA_UAC_TOKEN
    3. OpenClaw 注入的环境变量 coclaw_empno / coclaw_token
    4. 配置文件 <skill>/config/config.yaml 的 cca.emp_no / cca.uac_token

配置文件默认路径为 <skill>/config/config.yaml，可用 --config 或环境变量
FOUR0S_CONFIG 覆盖。

退出码：0 成功；1 扫描到非 0 问题（仅 --fail-on-leak）；2 配置不完整；
3 凭据无效；4 运行过程中出错。
"""

import argparse
import csv
import io
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
import yaml

try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except ImportError:
    pass

SKILL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = SKILL_ROOT / "config" / "config.yaml"

# CCA 开放 API（/api/v2）：只认 X-Emp-No + X-Uac-Token 两个请求头鉴权
CCA_API_PREFIX = "/api/v2"
# 问题列表接口单页上限 500 条，超过需翻页
ISSUE_PAGE_SIZE = 500
# 单次 HTTP 超时默认值（秒），可被配置项 request.timeout 覆盖
DEFAULT_HTTP_TIMEOUT = 60

# 凭据环境变量：FOUR0S_* 为本技能专用；coclaw_* 由 OpenClaw 注入
ENV_EMP_NO = "FOUR0S_CCA_EMP_NO"
ENV_UAC_TOKEN = "FOUR0S_CCA_UAC_TOKEN"
OPENCLAW_ENV_EMP_NO = "coclaw_empno"
OPENCLAW_ENV_UAC_TOKEN = "coclaw_token"

# 判定「未配置」的占位符，避免把模板值当成真实配置
PLACEHOLDER_VALUES = {
    "",
    "todo",
    "tbd",
    "none",
    "null",
    "cookie",
    "csrf",
    "token",
    "empno",
    "emp_no",
    "change-me",
}
PLACEHOLDER_PREFIXES = ("<", "your-", "xxx")
# 命中这些关键字的重定向说明鉴权已失效（被重定向到登录页）
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


def _first_env(*names):
    """按顺序返回第一个非空环境变量的值。"""
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return ""


def load_config(config_path):
    """加载并规范化配置；凭据允许用环境变量（含 OpenClaw 注入）覆盖。"""
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

    # 凭据优先级：环境变量（本技能专用 / OpenClaw 注入）> config.yaml
    emp_no = _first_env(ENV_EMP_NO, OPENCLAW_ENV_EMP_NO) or cfg["cca"].get("emp_no")
    uac_token = _first_env(ENV_UAC_TOKEN, OPENCLAW_ENV_UAC_TOKEN) or cfg["cca"].get("uac_token")
    cfg["cca"]["emp_no"] = str(emp_no or "").strip()
    cfg["cca"]["uac_token"] = str(uac_token or "").strip()
    cfg["_config_path"] = str(path)
    return cfg


def validate_config(cfg):
    """返回配置问题列表；空列表表示配置完整（不校验凭据是否仍然有效）。"""
    errors = []
    cca = cfg["cca"]
    if _is_placeholder(cca.get("base_url")):
        errors.append("cca.base_url 未配置")
    if _is_placeholder(cca.get("project_id")):
        errors.append("cca.project_id 未配置（任务链接中 /workbench/project/<project_id>/ 一段）")
    if _is_placeholder(cca.get("emp_no")):
        errors.append(
            "cca.emp_no 未配置（可用 OpenClaw 注入的 coclaw_empno，"
            f"或环境变量 {ENV_EMP_NO}，或命令行 --emp-no）"
        )
    if _is_placeholder(cca.get("uac_token")):
        errors.append(
            "cca.uac_token 未配置（可用 OpenClaw 注入的 coclaw_token，"
            f"或环境变量 {ENV_UAC_TOKEN}，或命令行 --uac-token）"
        )
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


def build_auth_headers(cfg):
    """CCA 开放 API 请求头：只带 X-Emp-No + X-Uac-Token 两个鉴权参数。"""
    cca = cfg["cca"]
    return {
        "X-Emp-No": str(cca.get("emp_no") or "").strip(),
        "X-Uac-Token": str(cca.get("uac_token") or "").strip(),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def cca_api_get(cfg, path, params=None):
    """调用 CCA 开放 API（GET <base_url>/api/v2/...），返回响应体中的 data 字段。

    Args:
        cfg: 已加载的配置
        path: /api/v2 之后的路径，如
            /project/{projectShortId}/task/{taskShortId}/executions
        params: 查询参数；会自动补上开放 API 必传的 username（员工号）

    Returns:
        data 字段内容（dict 或 list）

    Raises:
        RuntimeError: 缺少凭据、HTTP 状态异常、响应体为空，或接口返回错误码（data 为 null）
    """
    cca = cfg["cca"]
    emp_no = str(cca.get("emp_no") or "").strip()
    uac_token = str(cca.get("uac_token") or "").strip()
    if not emp_no or not uac_token:
        raise RuntimeError(
            "缺少 CCA 凭据（X-Emp-No/X-Uac-Token）："
            f"请在 OpenClaw 中运行（读取注入的 {OPENCLAW_ENV_EMP_NO}/{OPENCLAW_ENV_UAC_TOKEN}），"
            f"或设置 {ENV_EMP_NO}/{ENV_UAC_TOKEN}，"
            "或写入 config.yaml 的 cca.emp_no / cca.uac_token"
        )

    url = _base_url(cca) + CCA_API_PREFIX + path
    query = dict(params or {})
    # 开放 API 的 username（员工号）必传，接口缺该参数会直接报错
    query.setdefault("username", emp_no)

    request_cfg = cfg.get("request") or {}
    timeout = request_cfg.get("timeout", DEFAULT_HTTP_TIMEOUT)
    retries = int(request_cfg.get("retries", 2) or 0)
    last_error = None
    for attempt in range(retries + 1):
        try:
            response = requests.get(
                url,
                headers=build_auth_headers(cfg),
                params=query,
                verify=False,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
            continue

        body = response.text or ""
        if response.status_code in (401, 403):
            raise RuntimeError(
                f"CCA 凭据无效或权限不足（HTTP {response.status_code}）：{url}"
            )
        location = response.headers.get("location", "")
        if response.status_code in (301, 302, 303, 307, 308) and any(
            hint in location.lower() for hint in LOGIN_HINTS
        ):
            raise RuntimeError(f"CCA 凭据已失效：请求被重定向到登录页（{location}）")
        if response.status_code != 200 or not body.strip():
            raise RuntimeError(
                f"CCA API 请求失败: url={url}, status={response.status_code}, "
                f"body[:500]={body[:500]!r}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"CCA API 返回非 JSON（可能被登录页或网关拦截）: url={url}, "
                f"body[:300]={body[:300]!r}"
            ) from exc
        if not isinstance(payload, dict) or payload.get("data") is None:
            raise RuntimeError(f"CCA API 返回异常: url={url}, body={body[:500]!r}")
        return payload["data"]

    raise RuntimeError(f"请求 CCA 失败：{url}（{type(last_error).__name__}: {last_error}）")


def get_latest_record(cfg, task_id, label):
    """取任务最近一次执行的记录（按开始时间倒序的第一条）。"""
    data = cca_api_get(
        cfg,
        f"/project/{cfg['cca']['project_id']}/task/{task_id}/executions",
        {"start": 0, "length": 1, "order": "desc", "sort": "startTime"},
    )
    records = (data or {}).get("data") or []
    if not records:
        raise RuntimeError(f"{label} 任务 {task_id} 没有查询到执行记录")
    return records[0]


def get_latest_record_id(cfg, task_id, label):
    """取任务最近一次执行的记录 ID（shortId）。"""
    return get_latest_record(cfg, task_id, label).get("shortId")


def fetch_issues(cfg, task_id, record_id):
    """翻页拉取某次执行下的全部问题明细（单页上限 ISSUE_PAGE_SIZE）。"""
    issues = []
    start = 0
    while True:
        data = cca_api_get(
            cfg,
            f"/project/{cfg['cca']['project_id']}/task/{task_id}"
            f"/execution/{record_id}/issues",
            {
                "start": start,
                "length": ISSUE_PAGE_SIZE,
                "order": "asc",
                "sort": "sn",
                "filter": "",
            },
        )
        page = (data or {}).get("data") or []
        issues.extend(page)
        start += len(page)
        total = (data or {}).get("recordsTotal") or 0
        if not page or (total and len(issues) >= total):
            break
    return issues


def check_auth(cfg, task_id=None):
    """调用 CCA 开放 API 验证凭据（X-Emp-No/X-Uac-Token）是否可用。"""
    task_id = task_id or _first_task_id(cfg)
    if not task_id:
        return {
            "ok": False,
            "reason": "没有可用的任务 ID，无法验证凭据：请先配置 projects[].kw_id 或 coverity_id",
        }
    try:
        record = get_latest_record(cfg, task_id, "凭据校验")
    except RuntimeError as exc:
        return {"ok": False, "reason": str(exc), "task": task_id}
    return {
        "ok": True,
        "reason": "凭据有效",
        "task": task_id,
        "executionShortId": record.get("shortId"),
        "executionStatus": record.get("status"),
    }


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
    """行号为 Various 时，按原脚本逻辑从缺陷标题里抽取首个数字。"""
    for leak in leaks:
        if leak["lineNumber"] == "Various":
            matchers = re.findall(r"\d+", str(leak.get("classification") or ""))
            if matchers:
                leak["lineNumber"] = matchers[0]


# --------------------------------------------------------------------------- #
# 本地仓库同步与提交者分析
# --------------------------------------------------------------------------- #
# 单条 git 命令的默认超时（秒）。远端（如 Gerrit）静默无响应时必须能主动放弃，
# 否则整轮扫描会无限等待——sync_repositories 逐条命令都带超时。
DEFAULT_GIT_TIMEOUT = 180


class GitTimeout(RuntimeError):
    """git 命令超时，通常是远端网络卡住。"""


def _terminate_process_group(proc):
    """按进程组终止，避免只杀掉 git 却留下 ssh 子进程。"""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


def _run_process(command, work_dir, timeout, env=None):
    """执行外部命令，超时按进程组终止；返回 (returncode, stdout, stderr)。"""
    try:
        proc = subprocess.Popen(
            command,
            cwd=work_dir or None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env or os.environ.copy(),
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"命令不存在：{command[0]}") from exc
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_process_group(proc)
        raise GitTimeout(f"命令超时（>{timeout}s 已终止）：{' '.join(command)}")
    return proc.returncode, out or "", err or ""


def _ssh_base_command(cfg, code_path=""):
    """ssh 命令基线：优先 GIT_SSH_COMMAND，其次 git config core.sshCommand。"""
    if "_ssh_base" in cfg:
        return cfg["_ssh_base"]
    base = os.environ.get("GIT_SSH_COMMAND")
    if not base:
        rc, out, _ = _run_process(["git", "config", "--get", "core.sshCommand"], code_path, 10)
        base = out.strip() if rc == 0 else ""
    base = base or "ssh"
    # 远端静默时让 ssh 自己发现并断开，而不是无限等待
    if "ServerAliveInterval" not in base:
        base = f"{base} -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=4"
    cfg["_ssh_base"] = base
    return base


def _git_env(cfg, code_path=""):
    """git 子进程环境：不弹交互提示，可选给 ssh 加保活参数。"""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_PAGER"] = "cat"
    if not (cfg.get("sync") or {}).get("ssh_keepalive", True):
        return env
    env["GIT_SSH_COMMAND"] = _ssh_base_command(cfg, code_path)
    return env


def _git_timeout(cfg, default=DEFAULT_GIT_TIMEOUT):
    configured = (cfg.get("sync") or {}).get("timeout", default)
    try:
        return max(5, int(configured))
    except (TypeError, ValueError):
        return default


def run_git(cfg, code_path, args, timeout=None, check=True):
    """在指定仓库执行 git 命令；check=True 时非 0 退出码抛 RuntimeError。"""
    timeout = timeout or _git_timeout(cfg)
    rc, out, err = _run_process(["git", *args], code_path, timeout, _git_env(cfg, code_path))
    if check and rc != 0:
        detail = (err or out or "").strip().replace("\n", " ")
        raise RuntimeError(f"git {' '.join(args)} 失败（exit={rc}）：{detail[:300]}")
    return rc, out, err


def sync_repositories(cfg, projects, log=print):
    """按项目同步本地仓库：stash（可选）-> checkout 配置分支 -> pull。

    返回 (可用于 blame 的项目名集合, 告警列表)。checkout 成功即视为代码可用；
    pull 失败或超时只记告警并降级为使用本地现有代码，不会卡住或中断整轮扫描。
    """
    sync_cfg = cfg.get("sync") or {}
    if not sync_cfg.get("enabled", True):
        log("[sync] 已禁用本地仓库同步（sync.enabled=false）")
        return set(), []

    timeout = _git_timeout(cfg)
    synced, warnings = set(), []
    for project in projects:
        name = str(project.get("name") or "").strip()
        code_path = str(project.get("local_path") or "").strip()
        branch = str(project.get("branch") or "").strip()
        try:
            if not Path(code_path).is_dir():
                raise RuntimeError(f"本地路径不存在：{code_path}")
            rc, _, _ = run_git(cfg, code_path, ["rev-parse", "--git-dir"], timeout=30, check=False)
            if rc != 0:
                raise RuntimeError(f"{code_path} 不是 git 仓库")

            if sync_cfg.get("stash_before_pull", True):
                _, dirty, _ = run_git(
                    cfg, code_path, ["status", "--porcelain", "--untracked-files=no"], check=False
                )
                if dirty.strip():
                    run_git(cfg, code_path, ["stash", "push", "-q"], check=False)
                    log(f"[sync] {name}: 已 stash 未提交改动（git stash pop 可恢复）")

            run_git(cfg, code_path, ["checkout", branch], timeout=min(timeout, 120))
            synced.add(name)

            if not sync_cfg.get("pull", True):
                log(f"[sync] {name}: 已切换到 {branch}（未拉取）")
                continue
            try:
                run_git(cfg, code_path, ["pull", "--ff-only"], timeout=timeout)
                log(f"[sync] {name}: 已切换到 {branch} 并更新到最新代码")
            except (GitTimeout, RuntimeError) as exc:
                message = f"{name}: 已切到 {branch}，但拉取最新代码失败（{exc}）；本次按本地现有代码分析"
                warnings.append(message)
                log(f"[sync] {name}: 拉取失败，已降级为使用本地代码")
        except GitTimeout as exc:
            message = f"{name}: 同步超时已跳过（{exc}）"
            warnings.append(message)
            log(f"[sync] {message}")
        except Exception as exc:
            message = f"{name}: 同步失败已跳过（{type(exc).__name__}: {exc}）"
            warnings.append(message)
            log(f"[sync] {message}")
    return synced, warnings


def attach_committers(cfg, leaks, synced_projects, log=print):
    """用 git blame 补全缺陷提交者（找不到时保持为空）。"""
    if not (cfg.get("sync") or {}).get("blame", True):
        return
    index = _project_index(cfg)
    grouped = {}
    for leak in leaks:
        grouped.setdefault(leak["projectName"], []).append(leak)

    for project_name, items in grouped.items():
        project = index.get(project_name)
        if not project or project_name not in synced_projects:
            continue
        code_path = str(project.get("local_path") or "").strip()
        for leak in items:
            line_number = leak.get("lineNumber")
            relative = leak.get("relativePath")
            if not relative or line_number in (None, "", "Various"):
                continue
            try:
                rc, out, _ = run_git(
                    cfg,
                    code_path,
                    ["blame", "-L", f"{line_number},{line_number}", "--", "./" + relative],
                    timeout=60,
                    check=False,
                )
                if rc != 0 or not out.strip():
                    log(f"[blame] {project_name}: {relative}:{line_number} 未找到提交者")
                    continue
                sha = out.split()[0].lstrip("^")
                rc, author, _ = run_git(
                    cfg, code_path, ["show", "-s", "--format=%cn", sha], timeout=30, check=False
                )
                if rc == 0 and author.strip():
                    leak["committer"] = author.strip()
            except Exception as exc:
                log(f"[blame] {project_name}: {relative}:{line_number} 查询失败（{exc}）")


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
    if _is_placeholder(cfg["cca"].get("emp_no")) or _is_placeholder(cfg["cca"].get("uac_token")):
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
            f"请在 OpenClaw 中运行（读取注入的 coclaw_empno/coclaw_token），"
            f"或设置环境变量 {ENV_EMP_NO}/{ENV_UAC_TOKEN}，"
            "或 config.yaml 的 cca.emp_no/cca.uac_token 提供有效凭据后重试。"
        )
        return 3
    print(f"凭据检查：有效（校验任务 {auth['task']}）")

    projects = _enabled_projects(cfg, set(args.project) if args.project else None)
    if not projects:
        print("没有匹配到可扫描的项目，请检查 projects 配置与 --project 参数。")
        return 2
    print(f"开始扫描 {len(projects)} 个项目：{', '.join(str(p['name']) for p in projects)}")

    leaks, warnings = [], []
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
        description=(
            "CCA「4个0」问题扫描：校验配置与凭据，抓取 Klocwork/Coverity "
            "未处理高优先级问题（走 CCA 开放 API，仅需 X-Emp-No + X-Uac-Token）。"
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
    subparsers.add_parser("check-cookie", help="check-auth 的旧名称，行为相同")
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
        "check-cookie": cmd_check_auth,
        "scan": cmd_scan,
    }
    try:
        return handlers[args.command](cfg, args)
    except RuntimeError as exc:
        print(f"执行失败：{exc}")
        return 4


if __name__ == "__main__":
    sys.exit(main())
