# -*- coding: utf-8 -*-
"""Hub（开源组件漏洞）数据来源。

1. 版本统计：走 CCA 开放 API
   POST /api/v2/zte-rdcloud-icode-dataserver/statistic/version-summary-statistic，
   拿到版本级/任务级的开源漏洞与合规各级别数量，以及 CCA 自己的
   BLOCK / ALARM / PASS 指标判定（indicatorReport，阈值均为 0）。
   注意：接口只给数量，不给组件名 / 版本 / CVE。
2. 组件明细：用 GET /api/v2/projects/{projectId}/{taskId}/getSingleTaskReport
   下载各 Hub 任务报告 zip、解压后解析。只认「漏洞」表 + 「开源软件BOM清单」表，
   剔除 LOW 与 IGNORED 的行，按 组件ID+版本ID 聚合取最高危险等级。

CCA 开放 API 未提供 Hub 组件级明细（issue-detail-with-code 对 toolType=hub
直接报错），所以「改哪个依赖」这一步必须解析任务报告。
"""

import io
import os
import re
import shutil
import warnings
import zipfile
from pathlib import Path

from .cca import api_get_bytes, api_post, api_request, base_url

HUB_TOOL_NAME = "hub"
HUB_VERSION_SUMMARY_PATH = "/zte-rdcloud-icode-dataserver/statistic/version-summary-statistic"
HUB_SUMMARY_DIMENSION = "versionSummary"
# 报告扩展名不可信（.xlsx 可能是 OLE2/.xls），格式按文件头判断
HUB_REPORT_EXTENSIONS = (".xlsx", ".xls", ".xlsm")
ZIP_MAGIC = b"PK\x03\x04"
OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
HUB_TASK_URL_RE = re.compile(r"/project/([^/]+)/task/([^/]+)", re.IGNORECASE)

# 危险等级排序（数值越大等级越高）
RISK_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
LEVEL_ORDER = [
    "Vulnerability.Critical",
    "Vulnerability.High",
    "Vulnerability.Medium",
    "Vulnerability.Low",
    "Vulnerability.Ok",
    "License.High",
    "License.Medium",
    "License.Low",
    "License.Ok",
]

# 修复状态为 IGNORED 视为已处置，与 CCA 平台口径一致
DEFAULT_IGNORE_STATUS = ("IGNORED",)

_SHEET_VULNERABILITY = ("漏洞", "Vulnerabilities")
_SHEET_BOM = ("开源软件BOM清单", "SBOM")
_SHEET_MATCH_PATH = ("匹配路径信息",)
_SHEET_HOME_KEYWORDS = ("首页",)
_HOME_TASK_LABELS = ("CCA的任务名", "CCA任务名称")
_HOME_VERSION_LABELS = ("版本号", "版本")

# 逻辑列 -> (精确列名候选, 关键字兜底)；两边都会先做「去空白 + 小写」归一化
_COLUMN_RULES = {
    "componentName": (
        ("开源软件名称（Component name）", "开源软件名称"),
        ("开源软件名称", "componentname", "组件名称"),
    ),
    "componentId": (("组件ID",), ("组件id",)),
    "componentVersion": (
        ("版本号（Component version name）",),
        ("版本号", "componentversion", "组件版本"),
    ),
    "versionId": (("版本ID",), ("版本id",)),
    "vulnId": (
        ("漏洞（Vulnerability id）", "漏洞编号"),
        ("vulnerabilityid", "漏洞编号", "漏洞（"),
    ),
    "remediationStatus": (
        ("修复状态（Remediation status）",),
        ("修复状态", "remediationstatus"),
    ),
    "risk": (("危险等级（Security Risk）",), ("危险等级", "securityrisk")),
    "licenseRisk": (
        ("合规风险等级（License Risk）",),
        ("合规风险等级", "licenserisk"),
    ),
    "reviewStatus": (("Review status",), ("reviewstatus",)),
    "matchType": (("Match type",), ("matchtype",)),
    "path": (("Path",), ("path",)),
    "archiveContext": (("Archive context",), ("archivecontext",)),
    "originId": (("Origin id",), ("originid",)),
}

# 需要人工处置的漏洞等级（LOW 按规则不体现，Ok 无需处置）
ACTIONABLE_LEVELS = ("Vulnerability.Critical", "Vulnerability.High", "Vulnerability.Medium")
# 「匹配路径信息」表里表示组件如何被引入的 Match type
MATCH_DIRECT = "FILE_DEPENDENCY_DIRECT"
MATCH_TRANSITIVE = "FILE_DEPENDENCY_TRANSITIVE"


# --------------------------------------------------------------------------- #
# 版本级统计
# --------------------------------------------------------------------------- #
def resolve_version_id(cfg, version_name):
    """版本名 → 版本对象（精确匹配优先，其次唯一匹配）。"""
    versions = api_request(cfg, "/vmsprojects/versions", params={"versionName": version_name})
    if isinstance(versions, dict):
        versions = versions.get("data") or []
    if not isinstance(versions, list) or not versions:
        raise RuntimeError(f"CCA 上没有匹配的版本：{version_name}")
    exact = [v for v in versions if str(v.get("versionName") or "") == version_name]
    if len(exact) == 1:
        return exact[0]
    if len(versions) == 1:
        return versions[0]
    names = "、".join(str(v.get("versionName") or "") for v in versions[:10])
    raise RuntimeError(
        f"版本名 {version_name} 匹配到 {len(versions)} 个版本（{names}），"
        "请在 cca.hub.version_name 里写完整版本号"
    )


def _level_stats(status_statistics):
    """把 statusStatistics 列表转成 {level: 数量} 与 {level: 备案数}。"""
    totals, recorded = {}, {}
    for item in status_statistics or []:
        if not isinstance(item, dict):
            continue
        level = str(item.get("level") or "").strip()
        if not level:
            continue
        totals[level] = int(item.get("total") or 0)
        recorded[level] = int(item.get("recordTotal") or 0)
    return totals, recorded


def _indicator_groups(indicator_report):
    """把 indicatorReport 拆成 blocking / alarming / passing 三组。"""
    groups = {"blocking": [], "alarming": [], "passing": []}
    if not isinstance(indicator_report, dict):
        return groups
    for key, target in (
        ("blockList", "blocking"),
        ("alarmList", "alarming"),
        ("passList", "passing"),
    ):
        for item in indicator_report.get(key) or []:
            if not isinstance(item, dict):
                continue
            groups[target].append(
                {
                    "code": str(item.get("indicatorCode") or ""),
                    "name": str(item.get("indicatorNameCn") or item.get("indicatorNameEn") or ""),
                    "category": str(item.get("categoryNameCn") or item.get("categoryNameEn") or ""),
                    "currentValue": str(item.get("currentValue") or ""),
                    "threshold": str(item.get("threshold") or ""),
                }
            )
    return groups


def get_version_summary(cfg, version_name):
    """查询版本级 Hub 统计；返回结构化结果（没有 Hub 数据时 available=False）。"""
    version = resolve_version_id(cfg, version_name)
    version_id = version.get("id")
    payload = api_post(
        cfg,
        HUB_VERSION_SUMMARY_PATH,
        params={"action": "query"},
        json_body={
            "key": "versionId",
            "value": str(version_id),
            "toolName": HUB_TOOL_NAME,
            "dimensionName": HUB_SUMMARY_DIMENSION,
        },
    )
    result = {
        "versionName": str(version.get("versionName") or version_name),
        "versionId": version_id,
        "toolName": HUB_TOOL_NAME,
        "available": False,
        "reason": "",
        "levels": {},
        "levelsRecorded": {},
        "indicators": {"blocking": [], "alarming": [], "passing": []},
        "allPass": None,
        "tasks": [],
        "reportId": "",
        "reportUpdatedAt": "",
        "reportUrl": "",
    }

    rows = payload.get("data") if isinstance(payload, dict) else None
    if not rows:
        result["reason"] = "该版本在 CCA 上没有 Hub 报告数据（未绑定 Hub 任务或报告未生成）"
        return result

    row = rows[0] if isinstance(rows[0], dict) else {}
    summaries = row.get("summaryStatistics") or []
    summary = summaries[0] if summaries and isinstance(summaries[0], dict) else {}
    levels, recorded = _level_stats(summary.get("statusStatistics"))
    if not levels:
        # CCA 没有该版本的 Hub 结果时返回 totalCount=-1 且不带 statusStatistics
        result["reason"] = (
            "该版本没有 Hub 扫描结果（CCA 返回 totalCount="
            f"{summary.get('totalCount')}），通常是未绑定 Hub 任务或报告尚未生成"
        )
        return result

    tasks = []
    for task in summary.get("taskSummaryStatistics") or []:
        if not isinstance(task, dict):
            continue
        task_levels, task_recorded = _level_stats(task.get("statusStatistics"))
        tasks.append(
            {
                "modelId": str(task.get("modelId") or ""),
                "resultCollectionId": str(task.get("resultCollectionId") or ""),
                "totalCount": int(task.get("totalCount") or 0),
                "recordCount": int(task.get("recordCount") or 0),
                "levels": task_levels,
                "levelsRecorded": task_recorded,
            }
        )

    indicators = _indicator_groups(summary.get("indicatorReport"))
    result.update(
        {
            "available": True,
            "levels": levels,
            "levelsRecorded": recorded,
            "indicators": indicators,
            "allPass": not indicators["blocking"] and not indicators["alarming"],
            "tasks": tasks,
            "reportId": str(summary.get("reportId") or ""),
            "reportUpdatedAt": str(row.get("reportUpdatedDate") or ""),
            "reportUrl": (
                f"{base_url(cfg['cca'])}/frontend/reports"
                f"?versionId={version_id}&toolName={HUB_TOOL_NAME}"
            ),
        }
    )
    return result


# --------------------------------------------------------------------------- #
# 报告解析
# --------------------------------------------------------------------------- #
def _normalize_cell(value):
    return re.sub(r"\s+", "", str(value if value is not None else "")).lower()


def _pick_sheet(sheet_names, candidates, keywords=(), regex=None):
    for candidate in candidates:
        if candidate in sheet_names:
            return candidate
    for name in sheet_names:
        if not isinstance(name, str):
            continue
        for keyword in keywords:
            if keyword in name:
                return name
    if regex:
        for name in sheet_names:
            if isinstance(name, str) and re.search(regex, name, re.IGNORECASE):
                return name
    return ""


def _header_map(row):
    header = {}
    for index, cell in enumerate(row or []):
        key = _normalize_cell(cell)
        if key and key not in header:
            header[key] = index
    return header


def _pick_column(header, candidates, keywords=()):
    for candidate in candidates:
        index = header.get(_normalize_cell(candidate))
        if index is not None:
            return index
    for keyword in keywords:
        normalized = _normalize_cell(keyword)
        if not normalized:
            continue
        for name, index in header.items():
            if normalized in name:
                return index
    return None


def _load_with_openpyxl(path):
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover - 依赖缺失时的兜底
        raise RuntimeError(
            "解析 xlsx 报告需要 openpyxl，请先安装：pip install openpyxl"
        ) from exc
    # CCA 报表缺默认样式时 openpyxl 会打 UserWarning
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        return {
            name: [list(row) for row in workbook[name].iter_rows(values_only=True)]
            for name in workbook.sheetnames
        }
    finally:
        workbook.close()


def _load_with_xlrd(path):
    try:
        import xlrd
    except ImportError as exc:  # pragma: no cover - 依赖缺失时的兜底
        raise RuntimeError(
            "解析 Hub 的 .xls 报告需要 xlrd，请先安装：pip install xlrd"
        ) from exc
    book = xlrd.open_workbook(str(path))
    sheets = {}
    for sheet in book.sheets():
        sheets[sheet.name] = [
            [sheet.cell_value(row, column) for column in range(sheet.ncols)]
            for row in range(sheet.nrows)
        ]
    return sheets


def _load_sheets(path):
    """按文件头嗅探真实格式并读成 {工作表名: 二维列表}。

    CCA 上的 Hub 报告扩展名不可信（存在 `.xlsx` 实为 OLE2/.xls 的情况），
    所以先看魔数：ZIP → openpyxl，OLE2 → xlrd。
    """
    path = Path(path)
    with open(path, "rb") as file:
        head = file.read(8)
    if head.startswith(ZIP_MAGIC):
        return _load_with_openpyxl(path)
    if head.startswith(OLE2_MAGIC):
        return _load_with_xlrd(path)
    raise RuntimeError(f"无法识别的报告格式（既不是 xlsx 也不是 xls）：{path}")


def _extract_home_value(rows, labels):
    """从「首页」表里按「标签 | 值」取值。"""
    wanted = {_normalize_cell(x) for x in labels}
    for row in rows or []:
        for index, cell in enumerate(row or []):
            if _normalize_cell(cell) not in wanted:
                continue
            for value in row[index + 1 :]:
                text = str(value if value is not None else "").strip()
                if text:
                    return text
    return ""


def _extract_task_name(rows):
    """从「首页」表里取 CCA 任务名（即微服务名）。"""
    return _extract_home_value(rows, _HOME_TASK_LABELS)


def _records(matrix):
    """把工作表矩阵按逻辑列解析成记录列表。"""
    header_index, header = -1, {}
    for index, row in enumerate(matrix or []):
        header = _header_map(row)
        if header:
            header_index = index
            break
    if header_index < 0:
        return []

    indexes = {
        logical: _pick_column(header, candidates, keywords)
        for logical, (candidates, keywords) in _COLUMN_RULES.items()
    }
    records = []
    for row in matrix[header_index + 1 :]:
        if not row:
            continue
        record = {}
        empty = True
        for logical, column in indexes.items():
            if column is None or column >= len(row):
                record[logical] = ""
                continue
            text = str(row[column] if row[column] is not None else "").strip()
            record[logical] = text
            if text:
                empty = False
        if not empty:
            records.append(record)
    return records


def _origin_map(bom_records):
    """组件ID+版本ID -> Origin id（Maven 坐标）。"""
    mapping = {}
    for record in bom_records:
        key = (record.get("componentId", ""), record.get("versionId", ""))
        if not all(key):
            continue
        if not mapping.get(key):
            mapping[key] = record.get("originId", "")
    return mapping


def _gav_chain(path):
    """从「匹配路径信息」的 Path 里解析 Maven GAV 链。

    形如 `com.zte.x:module:1.0:-maven/g:a:1.0/h:b:2.0`，返回
    `["com.zte.x:module:1.0", "g:a:1.0", "h:b:2.0"]`；jar 路径（/BOOT-INF/...）返回空。
    """
    text = str(path or "").strip()
    if ":-maven/" in text:
        text = text.split(":-maven/", 1)[1]
    elif text.startswith("/"):
        return []
    return [segment for segment in text.split("/") if segment]


def _match_path_map(match_records):
    """组件ID+版本ID -> 引入路径记录列表。"""
    mapping = {}
    for record in match_records:
        key = (record.get("componentId", ""), record.get("versionId", ""))
        if not all(key):
            key = (record.get("componentName", ""), record.get("componentVersion", ""))
        if not any(key):
            continue
        mapping.setdefault(key, []).append(
            {
                "matchType": str(record.get("matchType") or "").strip(),
                "path": str(record.get("path") or "").strip(),
                "archiveContext": str(record.get("archiveContext") or "").strip(),
            }
        )
    return mapping


def _introduction(records):
    """把引入路径记录归纳成「直接/传递 + 一级依赖 + 直接父级」。"""
    direct, first_level, parents, chains = False, [], [], []
    for record in records:
        match_type = record.get("matchType", "")
        if match_type == MATCH_DIRECT:
            direct = True
        chain = _gav_chain(record.get("path", ""))
        if not chain:
            continue
        if match_type == MATCH_TRANSITIVE:
            first_level.append(chain[0])
            if len(chain) >= 2:
                parents.append(chain[-2])
            chains.append(" -> ".join(chain))
    introduction = {
        "direct": direct,
        "firstLevel": sorted(set(first_level)),
        "parents": sorted(set(parents)),
        "chains": chains[:3],
    }
    if direct:
        introduction["kind"] = "直接引入"
    elif first_level:
        introduction["kind"] = "传递引入"
    elif records:
        introduction["kind"] = "仅制品内匹配"
    else:
        introduction["kind"] = ""
    return introduction


def _higher_risk(current, candidate):
    left, right = str(current or "").upper(), str(candidate or "").upper()
    if not right:
        return left
    if not left:
        return right
    return right if RISK_ORDER.get(right, 1) > RISK_ORDER.get(left, 1) else left


def _error_result(path, message):
    return {
        "file": str(path),
        "taskName": "",
        "reportVersion": "",
        "rowsTotal": 0,
        "rowsKept": 0,
        "levels": {level: 0 for level in LEVEL_ORDER},
        "components": [],
        "componentCount": 0,
        "vulnerableCount": 0,
        "vulnerabilityCount": 0,
        "maxRisk": "",
        "errors": [message],
    }


def _license_level(value):
    """合规风险等级 → License.X（空 / NONE 视为 Ok）。"""
    text = str(value or "").strip().upper()
    if text in ("HIGH", "MEDIUM", "LOW"):
        return f"License.{text.title()}"
    return "License.Ok"


def _vulnerability_level(risk):
    """组件最高危险等级 → Vulnerability.X（无漏洞视为 Ok）。"""
    text = str(risk or "").strip().upper()
    if text in RISK_ORDER:
        return f"Vulnerability.{text.title()}"
    return "Vulnerability.Ok"


def parse_report_excel(path, hub_cfg=None):
    """解析单个 Hub 报告，按组件维度汇总（与 CCA 平台口径一致）。

    组件全集取自「开源软件BOM清单」表；危险等级取该组件在「漏洞」表里
    非 IGNORED 记录中的最高值（没有则算 Ok）；合规等级取自 BOM 的
    「合规风险等级」列；「首页」表取任务名与版本号。
    """
    hub_cfg = hub_cfg or {}
    ignore_status = {
        str(x).strip().upper() for x in (hub_cfg.get("ignore_status") or DEFAULT_IGNORE_STATUS)
    }

    path = Path(path)
    try:
        sheets = _load_sheets(path)
    except Exception as exc:
        return _error_result(path, f"无法打开报告：{exc}")

    sheet_names = list(sheets)
    vuln_sheet = _pick_sheet(
        sheet_names, _SHEET_VULNERABILITY, keywords=("漏洞",), regex=r"vulnerabilit"
    )
    if not vuln_sheet:
        return _error_result(path, f"未找到漏洞工作表（当前工作表：{'、'.join(sheet_names)}）")
    bom_sheet = _pick_sheet(sheet_names, _SHEET_BOM, keywords=("BOM",), regex=r"bom")
    match_sheet = _pick_sheet(sheet_names, _SHEET_MATCH_PATH, keywords=("匹配路径",))
    home_sheet = _pick_sheet(sheet_names, ("首页",), keywords=_SHEET_HOME_KEYWORDS)

    task_name = ""
    report_version = ""
    if home_sheet:
        task_name = _extract_task_name(sheets[home_sheet])
        report_version = _extract_home_value(sheets[home_sheet], _HOME_VERSION_LABELS)

    bom_records = _records(sheets[bom_sheet]) if bom_sheet else []
    origins = _origin_map(bom_records)
    match_paths = _match_path_map(_records(sheets[match_sheet])) if match_sheet else {}
    vuln_records = _records(sheets[vuln_sheet])

    components = {}

    def key_of(record):
        key = (record.get("componentId", ""), record.get("versionId", ""))
        if not all(key):
            key = (record.get("componentName", ""), record.get("componentVersion", ""))
        return key if any(key) else None

    def ensure(record, key):
        component = components.get(key)
        if component is None:
            component = {
                "componentName": record.get("componentName", ""),
                "componentVersion": record.get("componentVersion", ""),
                "componentId": key[0],
                "versionId": key[1],
                "licenseLevel": _license_level(record.get("licenseRisk")),
                "licenseReviewed": str(record.get("reviewStatus") or "").strip().upper()
                == "REVIEWED",
                "maxRisk": "",
                "vulnerabilities": [],
                "seen": set(),
                "originId": origins.get(key, ""),
                "introduction": _introduction(match_paths.get(key, [])),
            }
            components[key] = component
        return component

    # 组件全集以 BOM 表为准，保证「无漏洞组件」也进入统计（对应平台的 Ok 计数）
    for record in bom_records:
        key = key_of(record)
        if key:
            ensure(record, key)

    rows_kept = 0
    for record in vuln_records:
        status = str(record.get("remediationStatus") or "").strip().upper()
        if status and status in ignore_status:
            continue
        risk = str(record.get("risk") or "").strip().upper()
        key = key_of(record)
        if not risk or not key:
            continue
        rows_kept += 1
        component = ensure(record, key)
        component["maxRisk"] = _higher_risk(component["maxRisk"], risk)

        vuln_id = record.get("vulnId", "")
        if vuln_id and vuln_id not in component["seen"]:
            component["seen"].add(vuln_id)
            token = vuln_id
            if record.get("remediationStatus"):
                token = f"{vuln_id} 状态:{record['remediationStatus']}"
            component["vulnerabilities"].append(token)

    items = []
    for component in components.values():
        component.pop("seen", None)
        component["vulnerabilityLevel"] = _vulnerability_level(component["maxRisk"])
        component["vulnerable"] = component["vulnerabilityLevel"] != "Vulnerability.Ok"
        component["actionable"] = component["vulnerabilityLevel"] in ACTIONABLE_LEVELS
        component["licenseRisky"] = component["licenseLevel"] != "License.Ok"
        component["vulnerabilityCount"] = len(component["vulnerabilities"])
        component["vulnerabilities"] = "；".join(component["vulnerabilities"])
        items.append(component)
    items.sort(key=lambda item: (item["componentName"], item["componentVersion"]))

    levels = {level: 0 for level in LEVEL_ORDER}
    for item in items:
        levels[item["vulnerabilityLevel"]] += 1
        levels[item["licenseLevel"]] += 1
    max_risk = ""
    for component in items:
        max_risk = _higher_risk(max_risk, component["maxRisk"])
    return {
        "file": str(path),
        "taskName": task_name,
        "reportVersion": report_version,
        "rowsTotal": len(vuln_records),
        "rowsKept": rows_kept,
        "levels": levels,
        "components": items,
        "componentCount": len(items),
        "vulnerableCount": sum(1 for item in items if item["vulnerable"]),
        "actionableCount": sum(1 for item in items if item["actionable"]),
        "licenseRiskyCount": sum(1 for item in items if item["licenseRisky"]),
        "vulnerabilityCount": sum(item["vulnerabilityCount"] for item in items),
        "maxRisk": max_risk,
        "errors": [],
    }


def _expand_dir(raw):
    text = str(raw or "").strip()
    if not text:
        return None
    return Path(os.path.expandvars(os.path.expanduser(text))).resolve()


# --------------------------------------------------------------------------- #
# 任务报告下载
# --------------------------------------------------------------------------- #
def parse_task_ref(ref, fallback_name=""):
    """把任务引用解析成 {projectId, taskId, name}。

    支持三种写法：
        "https://cca.zte.com.cn/workbench/project/<pid>/task/<tid>/executions"
        {"project_id": "<pid>", "task_id": "<tid>", "name": "core"}
        "<pid>/<tid>"
    """
    if isinstance(ref, dict):
        project_id = str(ref.get("project_id") or ref.get("projectId") or "").strip()
        task_id = str(ref.get("task_id") or ref.get("taskId") or "").strip()
        name = str(ref.get("name") or "").strip()
        url = str(ref.get("url") or "").strip()
        if url and not (project_id and task_id):
            matched = HUB_TASK_URL_RE.search(url)
            if matched:
                project_id, task_id = matched.group(1), matched.group(2)
        return {"projectId": project_id, "taskId": task_id, "name": name or fallback_name}

    text = str(ref or "").strip()
    matched = HUB_TASK_URL_RE.search(text)
    if matched:
        return {"projectId": matched.group(1), "taskId": matched.group(2), "name": fallback_name}
    if "/" in text:
        project_id, _, task_id = text.partition("/")
        return {
            "projectId": project_id.strip(),
            "taskId": task_id.strip(),
            "name": fallback_name,
        }
    return {"projectId": "", "taskId": text, "name": fallback_name}


def task_refs(cfg, extra_tasks=None):
    """汇总要处理哪些 Hub 任务。

    最小配置就是：`cca.hub.project_id`（Hub 任务所在项目，所有微服务共用一个）
    + 各 `projects[].hub_id`（每个微服务一个 Hub 任务）。
    也支持在项目里写完整 `hub_project_id` 覆盖，或用 `--hub-task` 临时补任务。
    """
    hub_cfg = cfg["cca"].get("hub") or {}
    default_project = str(hub_cfg.get("project_id") or "").strip()
    refs, seen = [], set()

    def add(ref):
        key = (ref["projectId"], ref["taskId"])
        if key in seen:
            return
        seen.add(key)
        if not ref["projectId"] or not ref["taskId"]:
            ref["error"] = (
                f"Hub 任务信息不完整（{ref['projectId'] or '缺少项目ID'}/"
                f"{ref['taskId'] or '缺少任务ID'}）：请配置 cca.hub.project_id 与 projects[].hub_id"
            )
        refs.append(ref)

    for project in cfg.get("projects") or []:
        if not isinstance(project, dict) or not project.get("enabled", True):
            continue
        hub_id = str(project.get("hub_id") or "").strip()
        if not hub_id:
            continue
        project_id = str(project.get("hub_project_id") or "").strip() or default_project
        add(
            {
                "projectId": project_id,
                "taskId": hub_id,
                "name": str(project.get("name") or ""),
            }
        )

    for raw in extra_tasks or []:
        ref = parse_task_ref(raw)
        if not ref["projectId"]:
            ref["projectId"] = default_project
        add(ref)
    return refs


def download_dir_for(cfg, hub_cfg, ref):
    """每个任务一个固定目录，便于重跑时覆盖而不是堆积文件。

    统一为 `<根目录>/hub_download/<project>_<task>/`：根目录取
    `cca.hub.download_dir`，未配置时取 `output.dir`，再退到当前工作目录。
    """
    base = _expand_dir((hub_cfg or {}).get("download_dir"))
    if base is None:
        configured = str((cfg.get("output") or {}).get("dir") or "").strip()
        base = Path(os.path.expanduser(configured)).resolve() if configured else Path.cwd()
    return base / "hub_download" / f"{ref['projectId']}_{ref['taskId']}"


def download_task_report(cfg, ref, out_dir, log=print):
    """下载单个 Hub 任务报告 zip 并解压，返回解压出的报告文件列表。"""
    content = api_get_bytes(
        cfg, f"/projects/{ref['projectId']}/{ref['taskId']}/getSingleTaskReport"
    )
    out_dir = Path(out_dir)
    # 只清理本技能为该任务创建的子目录，避免新旧报告混在一起
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    extracted = []
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        for member in archive.namelist():
            name = Path(member).name  # 去掉目录层级，避免路径穿越
            if not name or member.endswith("/"):
                continue
            target = out_dir / name
            with archive.open(member) as source, open(target, "wb") as dest:
                shutil.copyfileobj(source, dest)
            if target.suffix.lower() in HUB_REPORT_EXTENSIONS:
                extracted.append(target)
    log(f"[hub] 已下载 {ref['projectId']}/{ref['taskId']} 报告：{len(extracted)} 个文件")
    return extracted


# --------------------------------------------------------------------------- #
# 汇总入口
# --------------------------------------------------------------------------- #
def _match_platform_tasks(result, api_tasks):
    """把报告解析出的任务与平台的任务级统计对上（按组件数 + 等级分布）。

    平台的 taskSummaryStatistics 没有任务名，只能按签名匹配：先要求组件数与
    各级别分布都相同，退一步只要求组件数相同；剩下的条目记为未匹配。
    """
    used = set()
    for info in (result.get("byTask") or {}).values():
        match = None
        for index, task in enumerate(api_tasks):
            if index in used:
                continue
            if task.get("totalCount") == info.get("components") and task.get("levels") == info.get("levels"):
                match = index
                break
        if match is None:
            for index, task in enumerate(api_tasks):
                if index in used:
                    continue
                if task.get("totalCount") == info.get("components"):
                    match = index
                    break
        if match is None:
            continue
        used.add(match)
        platform = api_tasks[match]
        info["platform"] = {
            "resultCollectionId": platform.get("resultCollectionId", ""),
            "totalCount": platform.get("totalCount"),
            "levels": platform.get("levels") or {},
        }
        info["platformConsistent"] = (platform.get("levels") or {}) == (info.get("levels") or {})
    result["platformTasksUnmatched"] = [
        {
            "resultCollectionId": task.get("resultCollectionId", ""),
            "totalCount": task.get("totalCount"),
            "levels": task.get("levels") or {},
        }
        for index, task in enumerate(api_tasks)
        if index not in used
    ]
    return result


def collect_hub(
    cfg, version_name=None, extra_tasks=None, download=True, log=print, quiet_when_clean=True
):
    """汇合版本级统计与各任务报告，返回 CLI 输出用的 hub 段。

    quiet_when_clean=True 时，没有任何待处理漏洞就只保留告警与最终结论，
    不打印进度与核对信息（默认用于 scan）。
    """
    info = (lambda *args, **kwargs: None) if quiet_when_clean else log
    hub_cfg = dict(cfg["cca"].get("hub") or {})
    version_name = str(version_name or hub_cfg.get("version_name") or "").strip()

    result = {
        "versionName": version_name,
        "versionId": None,
        "toolName": HUB_TOOL_NAME,
        "summary": None,
        "components": [],
        "compliance": [],
        "filtered": {},
        "byTask": {},
        "levels": {level: 0 for level in LEVEL_ORDER},
        "levelsApi": {},
        "levelDiff": {},
        "excelFiles": [],
        "downloadedFiles": [],
        "tasks": [],
        "reportVersions": [],
        "errors": [],
    }

    if version_name:
        try:
            result["summary"] = get_version_summary(cfg, version_name)
            result["versionId"] = result["summary"].get("versionId")
            if result["summary"].get("available"):
                info(
                    f"[hub] 版本 {result['summary']['versionName']}："
                    f"{_format_levels(result['summary']['levels'])}"
                )
            else:
                info(f"[hub] {result['summary'].get('reason')}")
        except Exception as exc:
            result["errors"].append(f"Hub 版本统计抓取失败：{exc}")
            log(f"[hub] 版本统计抓取失败：{exc}")
    else:
        info("[hub] 未配置 cca.hub.version_name，跳过版本级指标（组件明细不受影响）")

    # 下载 Hub 任务报告（Hub 任务与 KW/Coverity 不在同一个项目）
    refs = task_refs(cfg, extra_tasks)
    result["tasks"] = [
        {"projectId": r["projectId"], "taskId": r["taskId"], "name": r.get("name", "")}
        for r in refs
    ]
    candidates = []
    if not refs:
        result["errors"].append(
            "没有配置 Hub 任务：请在 cca.hub.project_id 填 Hub 项目 ID，"
            "并在各 projects[].hub_id 填该微服务的 Hub 任务 ID（或用 --hub-task 临时指定）"
        )
    if refs and download:
        for ref in refs:
            if ref.get("error"):
                result["errors"].append(ref["error"])
                continue
            try:
                files = download_task_report(
                    cfg, ref, download_dir_for(cfg, hub_cfg, ref), log=info
                )
            except Exception as exc:
                message = f"下载 Hub 报告失败（{ref['projectId']}/{ref['taskId']}）：{exc}"
                result["errors"].append(message)
                log(f"[hub] {message}")
                continue
            for path in files:
                result["downloadedFiles"].append(str(path))
                candidates.append((path, ref.get("name", "")))
    elif refs:
        info("[hub] 已按参数跳过 Hub 报告下载")

    if not candidates:
        if refs and download:
            result["errors"].append("Hub 报告下载后没有可解析的文件，请检查任务是否有报告")
        return result

    ignored_text = "、".join(
        str(x).upper() for x in (hub_cfg.get("ignore_status") or DEFAULT_IGNORE_STATUS)
    )
    for path, hint in candidates:
        parsed = parse_report_excel(path, hub_cfg)
        result["excelFiles"].append(str(path))
        if parsed.get("reportVersion") and parsed["reportVersion"] not in result["reportVersions"]:
            result["reportVersions"].append(parsed["reportVersion"])
        for message in parsed["errors"]:
            result["errors"].append(f"{path.name}：{message}")
        task = parsed["taskName"] or hint or path.stem
        for level, count in (parsed.get("levels") or {}).items():
            result["levels"][level] = result["levels"].get(level, 0) + count
        for component in parsed["components"]:
            if component.get("actionable"):
                result["components"].append({"taskName": task, **component, "excelFile": str(path)})
            elif component.get("licenseRisky") and not component.get("licenseReviewed"):
                result["compliance"].append({"taskName": task, **component, "excelFile": str(path)})
        bucket = result["byTask"].setdefault(
            task,
            {
                "components": 0,
                "vulnerable": 0,
                "actionable": 0,
                "vulnerabilities": 0,
                "maxRisk": "",
                "file": str(path),
                "rowsTotal": 0,
                "rowsKept": 0,
                "levels": {},
            },
        )
        bucket["components"] += parsed["componentCount"]
        bucket["vulnerable"] += parsed["vulnerableCount"]
        bucket["actionable"] += parsed["actionableCount"]
        bucket["vulnerabilities"] += parsed["vulnerabilityCount"]
        bucket["rowsTotal"] += parsed.get("rowsTotal", 0)
        bucket["rowsKept"] += parsed.get("rowsKept", 0)
        bucket["maxRisk"] = _higher_risk(bucket["maxRisk"], parsed["maxRisk"])
        for level, count in (parsed.get("levels") or {}).items():
            bucket["levels"][level] = bucket["levels"].get(level, 0) + count
        if parsed["vulnerableCount"]:
            info(
                f"[hub] {path.name}：任务 {task}，组件 {parsed['componentCount']} 个"
                f"（{parsed['vulnerableCount']} 个有漏洞），漏洞 {parsed['vulnerabilityCount']} 条"
                f"（最高 {parsed['maxRisk'] or '无'}）"
            )
        else:
            info(
                f"[hub] {path.name}：任务 {task}，组件 {parsed['componentCount']} 个，"
                f"漏洞表 {parsed.get('rowsTotal', 0)} 行；剔除 {ignored_text} 状态后无待处理漏洞"
            )
    api_levels = (result["summary"] or {}).get("levels") or {}
    api_tasks = (result["summary"] or {}).get("tasks") or []
    _match_platform_tasks(result, api_tasks)
    if api_levels:
        result["levelsApi"] = api_levels
        result["levelDiff"] = {
            level: result["levels"].get(level, 0) - api_levels.get(level, 0)
            for level in LEVEL_ORDER
        }
        risky = {k: v for k, v in result["levelDiff"].items() if v and not k.endswith(".Ok")}
        harmless = {k: v for k, v in result["levelDiff"].items() if v and k.endswith(".Ok")}
        summary_levels = _format_levels(result["levels"])
        info(f"[hub] 报告合计（{len(result['excelFiles'])} 个任务）：{summary_levels}")
        if risky:
            detail = "，".join(f"{k} {'+' if v > 0 else ''}{v}" for k, v in risky.items())
            log(f"[hub] 风险项与平台版本级指标不一致：{detail}")
        elif harmless:
            detail = "，".join(f"{k} {'+' if v > 0 else ''}{v}" for k, v in harmless.items())
            info(f"[hub] 风险项与平台一致；无风险组件计数有差异：{detail}")
        else:
            info("[hub] 与平台版本级指标完全一致")
    if not version_name and result["reportVersions"]:
        info(
            "[hub] 报告里的版本号是 "
            + "、".join(result["reportVersions"])
            + "；想同时看版本级指标就把它填到 cca.hub.version_name"
        )
    result["components"].sort(
        key=lambda item: (
            -RISK_ORDER.get(str(item.get("maxRisk") or "").upper(), -1),
            str(item.get("componentName") or ""),
        )
    )
    result["compliance"].sort(key=lambda item: str(item.get("componentName") or ""))
    report_levels = result["levels"]
    result["filtered"] = {
        "low": report_levels.get("Vulnerability.Low", 0),
        "ignoredRows": sum(
            int(task_info.get("rowsTotal", 0)) - int(task_info.get("rowsKept", 0))
            for task_info in (result.get("byTask") or {}).values()
        ),
        "licenseRisky": len(result["compliance"]),
        "actionable": len(result["components"]),
    }
    if result["components"]:
        log(f"[hub] 待处理漏洞 {len(result['components'])} 个组件，明细见下方 Hub 段落")
    return result


def summarize_hub(hub):
    """把 hub 段压成 summary 里的精简结论。"""
    if not hub:
        return {"configured": False}
    summary = hub.get("summary") or {}
    indicators = summary.get("indicators") or {}
    components = hub.get("components") or []
    return {
        "configured": True,
        "versionName": hub.get("versionName") or "",
        "versionId": hub.get("versionId"),
        "summaryAvailable": bool(summary.get("available")),
        "allPass": summary.get("allPass"),
        "blocking": indicators.get("blocking", []),
        "alarming": indicators.get("alarming", []),
        "passing": indicators.get("passing", []),
        "levels": summary.get("levels") or {},
        "levelsRecorded": summary.get("levelsRecorded") or {},
        "summaryTasks": summary.get("tasks") or [],
        "componentCount": len(components),
        "componentCountAll": sum(
            int(info.get("components") or 0) for info in (hub.get("byTask") or {}).values()
        ),
        "complianceCount": len(hub.get("compliance") or []),
        "filtered": hub.get("filtered") or {},
        "vulnerabilityCount": sum(
            int(item.get("vulnerabilityCount") or 0) for item in components
        ),
        "byTask": hub.get("byTask") or {},
        "platformTasksUnmatched": hub.get("platformTasksUnmatched") or [],
        "levelsReport": hub.get("levels") or {},
        "levelsApi": hub.get("levelsApi") or {},
        "levelDiff": hub.get("levelDiff") or {},
        "tasks": hub.get("tasks") or [],
        "reportVersions": hub.get("reportVersions") or [],
        "errors": list(hub.get("errors") or []),
    }


def _format_levels(levels):
    if not levels:
        return "无数据"
    ordered = [level for level in LEVEL_ORDER if level in levels]
    ordered += [level for level in sorted(levels) if level not in ordered]
    return "，".join(f"{level} {levels[level]}" for level in ordered)


def _side(levels, prefix):
    """按 Vulnerability.* / License.* 分组显示各级别数量。"""
    return " · ".join(
        f"{level.split('.')[-1]} {levels.get(level, 0)}"
        for level in LEVEL_ORDER
        if level.startswith(prefix)
    )


def print_hub_report(hub, verbose=False):
    """终端输出 Hub 段落。

    没有待处理漏洞时默认什么都不输出（Hub 已清零，报告里不必出现）；
    verbose=True（`hub` 子命令显式要求看 Hub）时输出完整指标与任务明细。
    """
    if not hub:
        return
    summary = hub.get("summary") or {}
    components = hub.get("components") or []
    errors = hub.get("errors") or []
    if not verbose and not components and not errors:
        return

    print("")
    print(f"Hub（开源组件漏洞）版本：{hub.get('versionName') or '(未配置 cca.hub.version_name)'}")

    if verbose:
        if summary.get("available"):
            report_levels = hub.get("levels") or {}
            api_levels = summary.get("levels") or {}
            print(
                f"  平台版本级指标：{_side(api_levels, 'Vulnerability.')}"
                f" ｜ {_side(api_levels, 'License.')}"
            )
            print(f"  报告核对结果：{_side(report_levels, 'Vulnerability.')}")
            indicators = summary.get("indicators") or {}
            for label, key in (("CCA 指标 BLOCK", "blocking"), ("CCA 指标 ALARM", "alarming")):
                for item in indicators.get(key) or []:
                    print(
                        f"  {label}：{item['name']} = {item['currentValue']}"
                        f"（阈值 {item['threshold']}）"
                    )
            if summary.get("reportUrl"):
                print(f"  版本报告：{summary['reportUrl']}")
        elif summary:
            print(f"  版本统计不可用：{summary.get('reason') or '-'}")

    if components:
        print(f"  待处理漏洞组件（{len(components)} 个，中/高/严重且报告未处置）：")
        for item in components:
            intro = item.get("introduction") or {}
            print(
                f"  - [{item['taskName']}] {item['componentName']} {item['componentVersion']}"
                f" · {item['vulnerabilityLevel']} · 漏洞 {item['vulnerabilityCount']} 条"
            )
            print(
                f"      引入方式：{intro.get('kind') or '未知（报告无匹配路径信息）'}"
                + (f"｜一级依赖：{'、'.join(intro['firstLevel'][:3])}" if intro.get("firstLevel") else "")
            )
            print(f"      Origin id：{item['originId']}")

    if verbose:
        compliance = hub.get("compliance") or []
        if compliance:
            tasks = sorted({item.get("taskName", "") for item in compliance})
            print(
                f"  合规风险（License，独立于漏洞维度）：{len(compliance)} 个组件"
                f"（{'、'.join(tasks)}），明细见 JSON 的 hub.compliance"
            )
        by_task = hub.get("byTask") or {}
        if by_task:
            print("")
            print("任务明细（报告解析为准，最后一列是与平台任务级统计的对比）：")
            print(f"  {'任务(微服务)':<40} {'组件':<7} {'待处理':<7} {'已忽略行':<9} 平台对比")
            for name, info in by_task.items():
                platform = info.get("platform")
                if platform is None:
                    platform_text = "平台无对应任务条目"
                elif info.get("platformConsistent"):
                    platform_text = "一致"
                else:
                    diff = {
                        level: (info.get("levels") or {}).get(level, 0)
                        - (platform.get("levels") or {}).get(level, 0)
                        for level in LEVEL_ORDER
                    }
                    detail = "，".join(
                        f"{level} {'+' if value > 0 else ''}{value}"
                        for level, value in diff.items()
                        if value
                    )
                    platform_text = f"与平台不一致（{detail or '平台无数据'}）"
                ignored_rows = int(info.get("rowsTotal", 0)) - int(info.get("rowsKept", 0))
                print(
                    f"  {str(name)[:40]:<40} {info.get('components', 0):<7} "
                    f"{info.get('actionable', 0):<7} {ignored_rows:<9} {platform_text}"
                )
        for task in hub.get("platformTasksUnmatched") or []:
            print(
                f"  （平台任务条目 resultCollectionId={task['resultCollectionId']} 组件 "
                f"{task['totalCount']} 个，未匹配到报告）"
            )
        level_diff = hub.get("levelDiff") or {}
        if level_diff:
            risky = {
                level: value
                for level, value in level_diff.items()
                if value and not level.endswith(".Ok")
            }
            if risky:
                detail = "，".join(
                    f"{level} {'+' if value > 0 else ''}{value}" for level, value in risky.items()
                )
                print(f"  报告合计 vs 平台版本级：风险项不一致（{detail}），以报告明细为准")

    for message in errors:
        print(f"  ! {message}")
