# -*- coding: utf-8 -*-
"""配置加载、校验与项目筛选。"""

import os
from pathlib import Path

import yaml

from .constants import (
    DEFAULT_COVERITY_PRIORITIES,
    DEFAULT_KW_PRIORITIES,
    DEFAULT_KW_STATUS_FILTER,
    ENV_EMP_NO,
    ENV_UAC_TOKEN,
    OPENCLAW_ENV_EMP_NO,
    OPENCLAW_ENV_UAC_TOKEN,
    PLACEHOLDER_PREFIXES,
    PLACEHOLDER_VALUES,
)


class ConfigError(RuntimeError):
    """配置文件缺失或不可用。"""


def is_placeholder(value):
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
    if not isinstance(cfg["cca"].get("hub"), dict):
        cfg["cca"]["hub"] = {}

    # 「4个0」的判定口径内置默认值，配置里不写也按标准口径跑
    if not isinstance(cfg["cca"].get("kw"), dict):
        cfg["cca"]["kw"] = {}
    if not isinstance(cfg["cca"].get("coverity"), dict):
        cfg["cca"]["coverity"] = {}
    cfg["cca"]["kw"].setdefault("priorities", DEFAULT_KW_PRIORITIES)
    cfg["cca"]["kw"].setdefault("status_filter", DEFAULT_KW_STATUS_FILTER)
    cfg["cca"]["coverity"].setdefault("priorities", DEFAULT_COVERITY_PRIORITIES)
    cfg["cca"]["coverity"].setdefault("only_unclassified", True)

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
    if is_placeholder(cca.get("base_url")):
        errors.append("cca.base_url 未配置")
    needs_code_project = any(
        isinstance(p, dict)
        and p.get("enabled", True)
        and (not is_placeholder(p.get("kw_id")) or not is_placeholder(p.get("coverity_id")))
        for p in cfg["projects"]
    )
    if needs_code_project and is_placeholder(cca.get("project_id")):
        errors.append(
            "cca.project_id 未配置（Klocwork/Coverity 任务所在项目，"
            "取任务链接中 /workbench/project/<project_id>/ 一段）"
        )
    if is_placeholder(cca.get("emp_no")):
        errors.append(
            "cca.emp_no 未配置（可用 OpenClaw 注入的 coclaw_empno，"
            f"或环境变量 {ENV_EMP_NO}，或命令行 --emp-no）"
        )
    if is_placeholder(cca.get("uac_token")):
        errors.append(
            "cca.uac_token 未配置（可用 OpenClaw 注入的 coclaw_token，"
            f"或环境变量 {ENV_UAC_TOKEN}，或命令行 --uac-token）"
        )
    hub_cfg = cca.get("hub") or {}
    hub_project_id = str(hub_cfg.get("project_id") or "").strip()
    hub_tasks = [
        str(p.get("hub_id") or "").strip()
        for p in cfg["projects"]
        if isinstance(p, dict) and p.get("enabled", True) and str(p.get("hub_id") or "").strip()
    ]
    if hub_tasks and not hub_project_id:
        errors.append(
            "配置了 projects[].hub_id 但缺少 cca.hub.project_id"
            "（Hub 任务所在项目，所有微服务共用一个，可从任务链接 /workbench/project/<project_id>/ 取）"
        )

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
        if is_placeholder(local_path):
            errors.append(f"{where}.local_path 未配置")
        elif not Path(local_path).is_dir():
            errors.append(f"{where}.local_path 不存在或不是目录：{local_path}")
        if not str(project.get("branch") or "").strip():
            errors.append(f"{where}.branch 未配置")
        if all(
            is_placeholder(project.get(key)) for key in ("kw_id", "coverity_id", "hub_id")
        ):
            errors.append(f"{where} 至少需要配置 kw_id / coverity_id / hub_id 之一")
    return errors


def enabled_projects(cfg, only=None):
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


def project_index(cfg):
    return {str(p.get("name") or "").strip(): p for p in cfg["projects"] if isinstance(p, dict)}


def first_task_id(cfg, projects=None):
    for project in projects or cfg["projects"]:
        for key in ("kw_id", "coverity_id"):
            task_id = str(project.get(key) or "").strip()
            if task_id:
                return task_id
    return ""
