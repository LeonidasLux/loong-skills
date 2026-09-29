# -*- coding: utf-8 -*-
"""本地仓库同步与 git blame。

每条 git 命令都带超时并按进程组终止，避免远端（如 Gerrit）静默时卡死整轮扫描；
拉取失败只记告警，仓库降级为按本地现有代码分析。
"""

import os
import signal
import subprocess
from pathlib import Path

from .config import project_index

# 单条 git 命令的默认超时（秒）
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
    index = project_index(cfg)
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
