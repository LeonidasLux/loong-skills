# -*- coding: utf-8 -*-
"""全局常量：环境变量名、占位符判定、CCA 开放 API 前缀、分页与超时默认值。"""

from pathlib import Path

# 技能根目录：<skill>/scripts/four0s/constants.py -> <skill>
SKILL_ROOT = Path(__file__).resolve().parent.parent.parent
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

# 「4个0」的默认口径（配置里不写时使用）
DEFAULT_KW_PRIORITIES = ["Critical", "Error"]
DEFAULT_KW_STATUS_FILTER = ["Analyze"]
DEFAULT_COVERITY_PRIORITIES = ["High", "Medium"]
