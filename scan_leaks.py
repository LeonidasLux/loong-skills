import csv
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

import git
import requests

try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except ImportError:
    pass

# 代码分支：每个项目（仓库）可单独定义，未配置的项目使用 _DEFAULT_CODE_BRANCH
_DEFAULT_CODE_BRANCH = "develop"
# 需要特殊分支的项目在此登记，例如:
#   "zte-aiop-aiservice-core": "master",
# 也可用环境变量覆盖: SCAN_LEAKS_BRANCH_<仓库名大写且连字符变下划线>
#   export SCAN_LEAKS_BRANCH_ZTE_AIOP_AISERVICE_CORE=release
_DEFAULT_CODE_BRANCH_BY_PROJECT = {
    "zte-aiop-aiservice-appui": _DEFAULT_CODE_BRANCH,
    "zte-aiop-aiservice-webui": _DEFAULT_CODE_BRANCH,
    "zte-aiop-aiservice-webmobileui": _DEFAULT_CODE_BRANCH,
    "zte-aiop-aiservice-chat": "release_XYUAC-AICSV1.26.33B01",
    "zte-aiop-aiservice-core": "release_XYUAC-AICSV1.26.33B01",
}
code_branch = {
    k: os.environ.get(f"SCAN_LEAKS_BRANCH_{k.upper().replace('-', '_')}", v)
    for k, v in _DEFAULT_CODE_BRANCH_BY_PROJECT.items()
}


def get_code_branch(repo_name):
    """获取仓库对应的代码分支：项目单独配置优先，其次默认分支。"""
    env_key = f"SCAN_LEAKS_BRANCH_{repo_name.upper().replace('-', '_')}"
    return os.environ.get(env_key, code_branch.get(repo_name, _DEFAULT_CODE_BRANCH))

# 本地代码库的存放位置，用于查询提交者（git blame）。默认是服务器上的 /data/... 路径。
# 在本机运行时请设置环境变量覆盖，例如:
#   export SCAN_LEAKS_NEWCO_CLAW=/home/你的用户/Projects/.../newCo-Claw
# 键名规则: SCAN_LEAKS_ + 仓库名大写且连字符变下划线，如 newCo-Claw -> SCAN_LEAKS_NEWCO_CLAW
_DEFAULT_LOCAL_CODE_PATH = {
    "zte-aiop-aiservice-appui": "/media/vdc/0668001277/Projects2/AICS/zte-aiop-aiservice-appui",
    "zte-aiop-aiservice-webui": "/media/vdc/0668001277/Projects2/AICS/zte-aiop-aiservice-webui",
    "zte-aiop-aiservice-webmobileui": "/media/vdc/0668001277/Projects2/AICS/zte-aiop-aiservice-webmobileui",
    # "zte-aiop-aiservice-bigscreenui": "/media/vdc/0668001277/Projects2/AICS/zte-aiop-aiservice-bigscreenui",
    # "zte-aiop-aiservice-manageui": "/media/vdc/0668001277/Projects2/AICS/zte-aiop-aiservice-manageui",
    # "zte-aiop-aiservice-uportal": "/media/vdc/0668001277/Projects2/AICS/zte-aiop-aiservice-uportal",
    "zte-aiop-aiservice-chat": "/media/vdc/0668001277/Projects2/AICS/zte-aiop-aiservice-chat",
    "zte-aiop-aiservice-core": "/media/vdc/0668001277/Projects2/AICS/zte-aiop-aiservice-core",
}
local_code_path = {
    k: os.environ.get(f"SCAN_LEAKS_{k.upper().replace('-', '_')}", v)
    for k, v in _DEFAULT_LOCAL_CODE_PATH.items()
}

# CCA（云代码分析）开放 API（/api/v2）配置
# 任务id查看：https://i.zte.com.cn/#/shared/45e2844136c54b86a105b695eaef116b/wiki/page/76faeb0816ab11f1b6d6f79c8307e304/view
# 说明：开放 API 只认 X-Emp-No + X-Uac-Token 鉴权；浏览器 Cookie/SSO 仅对 /workbench 前端页面有效，
#       因此本脚本改为调用开放 API，不再依赖前端 workbench 接口与手动复制的 Cookie。
CCA_BASE_URL = "https://cca.zte.com.cn"
CCA_API_PREFIX = CCA_BASE_URL + "/api/v2"
# 问题列表接口单页上限 500 条，超过需翻页
ISSUE_PAGE_SIZE = 500
# 单次 HTTP 超时（秒）
CCA_HTTP_TIMEOUT = 60

# 认证信息：优先读环境变量，未设置时回退到下面的默认值
#   export coclaw_empno=0668001277
#   export coclaw_token=<UAC token>
_DEFAULT_EMP_NO = "0668001277"
_DEFAULT_UAC_TOKEN = "6343a034a2d855b138e1bfccbd3fa837"

cca_config = {
    "project_id": "lDpVWYmz",
    "kw_tasks": ["gqQGmfza", "ZsqXBCyC", "PLFGnTgz", "gv7Atexm", "fPxZycEU"],
    "cov_tasks": ["yBaWyGwg", "aLN4Gllv", "mtvP9Rlu", "FZY6SeFI", "3pk7hRDF"],
}

# 各工具的取数规则：
#   levels       —— 需要统计的问题级别（接口字段 priorityDescription）
#   status_field —— 判定「未处理」使用的问题字段：Klocwork 用 status，Coverity 用 classification
#   status_value —— 「未处理」对应的取值
_TOOL_RULES = {
    "kw": {
        "tasks_key": "kw_tasks",
        "levels": ("Critical", "Error"),
        "status_field": "status",
        "status_value": "Analyze",
        "type": "Klocwork",
    },
    "cov": {
        "tasks_key": "cov_tasks",
        "levels": ("High", "Medium"),
        "status_field": "classification",
        "status_value": "Unclassified",
        "type": "Coverity",
    },
}


def get_emp_no():
    """员工号：优先环境变量 coclaw_empno，其次使用默认值。"""
    return os.environ.get("coclaw_empno") or _DEFAULT_EMP_NO


def get_uac_token():
    """UAC token：优先环境变量 coclaw_token，其次使用默认值。"""
    return os.environ.get("coclaw_token") or _DEFAULT_UAC_TOKEN


def get_cca_header():
    """CCA 开放 API 请求头：X-Emp-No + X-Uac-Token。"""
    return {
        'X-Emp-No': get_emp_no(),
        'X-Uac-Token': get_uac_token(),
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    }


def cca_api_get(path, params):
    """
    调用 CCA 开放 API（GET /api/v2/...），返回响应体中的 data 字段。

    Args:
        path: /api/v2 之后的路径，如 /project/{projectShortId}/task/{taskShortId}/executions
        params: 查询参数。username（员工号）必传，接口缺少该参数会直接报错

    Returns:
        data 字段内容（dict 或 list）

    Raises:
        RuntimeError: HTTP 状态非 200、响应体为空，或接口返回错误码（data 为 null）
    """
    url = CCA_API_PREFIX + path
    query = dict(params)
    query.setdefault('username', get_emp_no())
    resp = requests.get(url, headers=get_cca_header(), params=query, verify=False, timeout=CCA_HTTP_TIMEOUT)
    if resp.status_code != 200 or not resp.text.strip():
        raise RuntimeError(
            f"CCA API 请求失败: url={url}, status={resp.status_code}, body[:500]={resp.text[:500]!r}"
        )
    body = resp.json()
    if body.get('data') is None:
        raise RuntimeError(
            f"CCA API 返回异常: url={url}, body={resp.text[:500]!r}"
        )
    return body['data']


def get_latest_execution(task):
    """
    查询任务最近一次执行记录（按开始时间倒序取第一条）。

    Args:
        task: 任务短ID（taskShortId）

    Returns:
        dict: 执行记录，含 shortId（执行短ID）、status、remark 等
    """
    data = cca_api_get(
        "/project/{}/task/{}/executions".format(cca_config['project_id'], task),
        {'start': 0, 'length': 1, 'order': 'desc', 'sort': 'startTime'}
    )
    records = data.get('data') or []
    if not records:
        raise RuntimeError(f"CCA 任务 {task} 没有查询到执行记录")
    return records[0]


def get_execution_issues(task, execution_short_id):
    """
    翻页拉取某次执行下的全部问题明细（单页上限 ISSUE_PAGE_SIZE）。

    Args:
        task: 任务短ID（taskShortId）
        execution_short_id: 执行短ID（shortId）

    Returns:
        list: 问题明细列表
    """
    issues = []
    start = 0
    while True:
        data = cca_api_get(
            "/project/{}/task/{}/execution/{}/issues".format(
                cca_config['project_id'], task, execution_short_id),
            {'start': start, 'length': ISSUE_PAGE_SIZE, 'order': 'asc', 'sort': 'sn', 'filter': ''}
        )
        page = data.get('data') or []
        issues.extend(page)
        start += len(page)
        total = data.get('recordsTotal') or 0
        if not page or (total and len(issues) >= total):
            break
    return issues


def collect_leaks(tool_key):
    """
    按工具规则收集任务列表下的「未处理」问题。

    Args:
        tool_key: _TOOL_RULES 中的键（kw / cov）

    Returns:
        list: leak 字典列表
    """
    rule = _TOOL_RULES[tool_key]
    leaks = []
    for task in cca_config[rule['tasks_key']]:
        execution = get_latest_execution(task)
        execution_short_id = execution['shortId']
        issues = get_execution_issues(task, execution_short_id)
        matched = [
            issue for issue in issues
            if issue.get('priorityDescription') in rule['levels']
            and issue.get(rule['status_field']) == rule['status_value']
        ]
        for issue in matched:
            leaks.append({
                "leakId": issue['sn'],
                "grade": issue['priorityDescription'],
                "status": issue.get('status', ''),
                "classification": issue['title'],
                "description": issue['description'],
                "committer": "",
                "filePath": issue['file']['fullName'],
                "lineNumber": get_line_number(issue['file']['snippets']),
                "relatedWorkItemId": "",
                "type": rule['type'],
                "projectName": "",
                "belongTeam": "",
                "taskInfo": "{}/workbench/project/{}/task/{}/execution/{}/issue/{}".format(
                    CCA_BASE_URL, cca_config['project_id'], task, execution_short_id, issue['sn']),
            })
        print("{} task {} execution {} (status={}): issues {}, leaks {}".format(
            rule['type'], task, execution_short_id, execution.get('status', 'N/A'),
            len(issues), len(matched)))

    fix_various_line_number(leaks)
    print("{} leaks count: {}".format(rule['type'], len(leaks)))
    return leaks


def fix_various_line_number(leaks):
    """部分问题行号返回 Various，尝试从规则名中提取行号。"""
    for leak in leaks:
        if leak['lineNumber'] == "Various":
            matchers = re.findall(r'\d+', leak['classification'])
            if len(matchers) > 0:
                leak['lineNumber'] = matchers[0]


def get_kw_leaks():
    """收集 Klocwork 问题（Critical/Error 且状态为 Analyze）。"""
    return collect_leaks("kw")


def get_cov_leaks():
    """收集 Coverity 问题（High/Medium 且状态为 Unclassified）。"""
    return collect_leaks("cov")


def get_line_number(snippets):
    if snippets is None:
        return "Various"
    return snippets[0]['lineStart']


def parse_leak(leaks):
    for leak in leaks:
        repo_name = leak['filePath'].split("/")[3]
        leak['projectName'] = repo_name
        file_path = "./" + ("/".join(leak['filePath'].split("/")[4:]))
        code_path = local_code_path[repo_name]
        if not Path(code_path).is_dir():
            env_hint = f"SCAN_LEAKS_{repo_name.upper().replace('-', '_')}"
            raise FileNotFoundError(
                f"本地仓库路径不存在: {code_path!r}（项目 {repo_name}）。"
                f"请设置环境变量 {env_hint}=<本地克隆目录>，或修改脚本中的 _DEFAULT_LOCAL_CODE_PATH。"
            )
        repo = git.Repo(code_path)
        branch = get_code_branch(repo_name)
        repo.git.stash()
        repo.remotes.origin.pull()
        repo.git.checkout(branch)
        repo.remotes.origin.pull()
        print(f"{repo_name} code already pulled (branch: {branch})")
        if leak['lineNumber'] == "Various":
            continue
        command = ["git", "blame", "-L", f"{leak['lineNumber']},{leak['lineNumber']}", "--", file_path]
        try:
            result = execute_command(command, code_path)
            output = result.strip()
            if output:
                commit_id = output.split()[0]
                commit = repo.commit(commit_id)
                author = commit.committer.name
                leak['committer'] = author
        except RuntimeError as e:
            print(command, f"in {repo_name} not find committer!")


def execute_command(command, work_dir):
    result = subprocess.run(command, cwd=work_dir, universal_newlines=True, stdout=subprocess.PIPE)
    if result.returncode == 0:
        return result.stdout
    else:
        print(result.stdout)
        raise RuntimeError("command exec failure")


def write_bug(leaks):
    if len(leaks) == 0:
        return

    print(f"write bugs to csv: {len(leaks)}")
    # 默认写到脚本所在目录，避免「当前工作目录」不确定导致找不到文件；可用 SCAN_LEAKS_CSV_DIR 覆盖
    out_dir = Path(os.environ.get("SCAN_LEAKS_CSV_DIR", Path(__file__).resolve().parent))
    out_dir.mkdir(parents=True, exist_ok=True)
    # 以本地时间生成文件名，避免覆盖上一次扫描结果
    filename = out_dir / f"leaks_result_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    with open(filename, "w", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(
            ["提交者", "代码库", "文件路径", "行数", "漏洞描述", "漏洞类型", "漏洞ID", "漏洞级别", "任务链接"])
        for leak in leaks:
            writer.writerow([
                leak["committer"], leak['projectName'], leak["filePath"]
                , leak["lineNumber"], leak['classification'] + "。" + leak["description"],
                leak['type'], leak['leakId'], leak['grade'], leak["taskInfo"]
            ])
    print(f"已生成 CSV: {filename.resolve()}")


if __name__ == '__main__':
    kw_leaks = get_kw_leaks()
    cov_leaks = get_cov_leaks()
    leaks = kw_leaks + cov_leaks
    print(f"leaks count: {len(leaks)}")
    try:
        parse_leak(leaks)
    finally:
        # parse_leak 中 git blame / commit 等可能抛错导致此前未写 CSV；保证有数据时总能落盘
        write_bug(leaks)
