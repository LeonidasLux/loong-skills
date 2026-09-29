# -*- coding: utf-8 -*-
"""CCA 开放 API 客户端：统一走 `<base_url>/api/v2`，鉴权用 X-Emp-No + X-Uac-Token。"""

import time

import requests

try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except ImportError:  # pragma: no cover - 仅当环境缺 urllib3 时
    pass

from .config import first_task_id
from .constants import (
    CCA_API_PREFIX,
    DEFAULT_HTTP_TIMEOUT,
    ENV_EMP_NO,
    ENV_UAC_TOKEN,
    ISSUE_PAGE_SIZE,
    LOGIN_HINTS,
    OPENCLAW_ENV_EMP_NO,
    OPENCLAW_ENV_UAC_TOKEN,
)


def base_url(cca):
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


def api_request(cfg, path, params=None, method="get", json_body=None):
    """调用 CCA 开放 API（<base_url>/api/v2/...），返回解析后的完整响应体。

    Args:
        cfg: 已加载的配置
        path: /api/v2 之后的路径，如
            /project/{projectShortId}/task/{taskShortId}/executions
        params: 查询参数；会自动补上开放 API 必传的 username（员工号）
        method: get / post
        json_body: POST 请求体（dict 或 list）

    Returns:
        解析后的响应体。有的接口直接返回数组（如版本列表），有的返回
        {"data": ...} / {"bo": ...}，由调用方按需取字段。

    Raises:
        RuntimeError: 缺少凭据、HTTP 状态异常或响应体为空
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

    url = base_url(cca) + CCA_API_PREFIX + path
    query = dict(params or {})
    # 开放 API 的 username（员工号）必传，接口缺该参数会直接报错
    query.setdefault("username", emp_no)

    request_cfg = cfg.get("request") or {}
    timeout = request_cfg.get("timeout", DEFAULT_HTTP_TIMEOUT)
    retries = int(request_cfg.get("retries", 2) or 0)
    last_error = None
    for attempt in range(retries + 1):
        try:
            if str(method).lower() == "post":
                response = requests.post(
                    url,
                    headers=build_auth_headers(cfg),
                    params=query,
                    json=json_body,
                    verify=False,
                    timeout=timeout,
                )
            else:
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
        if payload is None:
            raise RuntimeError(f"CCA API 返回异常: url={url}, body={body[:500]!r}")
        return payload

    raise RuntimeError(f"请求 CCA 失败：{url}（{type(last_error).__name__}: {last_error}）")


def api_get(cfg, path, params=None):
    """调用 CCA 开放 API（GET），返回响应体中的 data 字段。

    Raises:
        RuntimeError: 缺少凭据、HTTP 状态异常、响应体为空，或接口返回错误码（data 为 null）
    """
    payload = api_request(cfg, path, params=params)
    if not isinstance(payload, dict) or payload.get("data") is None:
        url = base_url(cfg["cca"]) + CCA_API_PREFIX + path
        raise RuntimeError(f"CCA API 返回异常: url={url}, body={str(payload)[:500]}")
    return payload["data"]


def api_post(cfg, path, params=None, json_body=None):
    """调用 CCA 开放 API（POST + JSON body），返回解析后的完整响应体。

    Hub 相关接口（版本摘要统计等）都是 POST + JSON body，所以单独提供。
    """
    return api_request(cfg, path, params=params, method="post", json_body=json_body)


def api_get_bytes(cfg, path, params=None):
    """GET 原始字节，用于下载报告 zip（非 JSON 响应）。

    Raises:
        RuntimeError: 缺少凭据、HTTP 状态异常或响应体为空
    """
    cca = cfg["cca"]
    emp_no = str(cca.get("emp_no") or "").strip()
    uac_token = str(cca.get("uac_token") or "").strip()
    if not emp_no or not uac_token:
        raise RuntimeError("缺少 CCA 凭据（X-Emp-No/X-Uac-Token）")

    url = base_url(cca) + CCA_API_PREFIX + path
    query = dict(params or {})
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

        content = response.content or b""
        if response.status_code in (401, 403):
            raise RuntimeError(f"CCA 凭据无效或权限不足（HTTP {response.status_code}）：{url}")
        location = response.headers.get("location", "")
        if response.status_code in (301, 302, 303, 307, 308) and any(
            hint in location.lower() for hint in LOGIN_HINTS
        ):
            raise RuntimeError(f"CCA 凭据已失效：请求被重定向到登录页（{location}）")
        if response.status_code != 200 or not content:
            raise RuntimeError(
                f"下载失败: url={url}, status={response.status_code}, "
                f"body[:300]={content[:300].decode('utf-8', 'replace')!r}"
            )
        return content

    raise RuntimeError(f"请求 CCA 失败：{url}（{type(last_error).__name__}: {last_error}）")


def get_latest_record(cfg, task_id, label):
    """取任务最近一次执行的记录（按开始时间倒序的第一条）。"""
    return get_latest_record_for(cfg, cfg["cca"]["project_id"], task_id, label)


def get_latest_record_for(cfg, project_id, task_id, label):
    """取指定项目下任务最近一次执行的记录（按开始时间倒序的第一条）。"""
    data = api_get(
        cfg,
        f"/project/{project_id}/task/{task_id}/executions",
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
        data = api_get(
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


def first_task_ref(cfg):
    """挑一个可用于鉴权校验的任务，返回 (project_id, task_id)。

    优先 Klocwork / Coverity 任务；只配了 Hub 任务时退回 Hub 任务
    （Hub 任务在 cca.hub.project_id 指定的项目下）。
    """
    for project in cfg.get("projects") or []:
        if not isinstance(project, dict):
            continue
        for key in ("kw_id", "coverity_id"):
            task_id = str(project.get(key) or "").strip()
            if task_id:
                return str(cfg["cca"].get("project_id") or ""), task_id

    hub_cfg = cfg["cca"].get("hub") or {}
    hub_project = str(hub_cfg.get("project_id") or "").strip()
    for project in cfg.get("projects") or []:
        if not isinstance(project, dict):
            continue
        hub_id = str(project.get("hub_id") or "").strip()
        if hub_id:
            return str(project.get("hub_project_id") or "").strip() or hub_project, hub_id
    return "", ""


def check_auth(cfg, task_id=None):
    """调用 CCA 开放 API 验证凭据（X-Emp-No/X-Uac-Token）是否可用。"""
    project_id, fallback_task = first_task_ref(cfg)
    if task_id:
        project_id, fallback_task = str(cfg["cca"].get("project_id") or ""), task_id
    task_id = fallback_task or first_task_id(cfg)
    if not task_id:
        return {
            "ok": False,
            "reason": (
                "没有可用的任务 ID，无法验证凭据："
                "请先配置 projects[].kw_id / coverity_id / hub_id 之一"
            ),
        }
    try:
        record = get_latest_record_for(cfg, project_id, task_id, "凭据校验")
    except RuntimeError as exc:
        return {"ok": False, "reason": str(exc), "task": task_id}
    return {
        "ok": True,
        "reason": "凭据有效",
        "task": task_id,
        "executionShortId": record.get("shortId"),
        "executionStatus": record.get("status"),
    }
