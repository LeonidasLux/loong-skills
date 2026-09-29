---
name: solve-four-0s-issue
description: 从 CCA 平台扫描并一键解决「4个0」问题，即 Klocwork Critical、Klocwork Error、Coverity High、Coverity Medium 四类高危缺陷清零；同时汇总 Hub 开源组件漏洞（版本级指标 + 报告 Excel 组件明细）。输出扫描结果时对每条问题给出「建议修复」或「建议备案」的结论，备案项附带可直接使用的备案说明。当用户提出处理 4个0、清理 CCA 上的 Klocwork/Coverity 高危告警、批量修复静态扫描缺陷、处理 Hub 开源组件漏洞时使用。
metadata:
  short-description: CCA 4个0问题 + Hub 开源组件漏洞扫描、修复/备案建议与经验归档
---

# solve-four-0s-issue

「4个0」指四类问题的数量都必须为 0：Klocwork Critical、Klocwork Error、Coverity High、Coverity Medium。

Hub（开源组件漏洞）是并列的第二组目标：版本级各级别数量与 BLOCK/ALARM/PASS 指标从 CCA 开放 API 读取；组件级明细（组件名/版本/CVE/Origin id）来自 Hub 任务报告（配了 `cca.hub.tasks` 时技能会自己下载解压，也可以只用本地已导出的报告）。

本技能负责完整闭环：校验配置与凭据 → 从 CCA 抓取缺陷（+ Hub 数据）→ 展示结果、给出修复/备案建议并等用户确认 → 修复代码（备案项登记到平台）→ 归档经验。

## 环境

- 依赖 `python3`（`requests`、`pyyaml`）与命令行 `git`；解析 Hub 报告还需要 `openpyxl`（xlsx）与 `xlrd`（.xls —— CCA 上的 Hub 报告常常扩展名写成 `.xlsx`，内容却是老的 OLE2/.xls 格式），不用该功能时无需安装：`pip install openpyxl xlrd`。
- CCA 接口统一走开放 API（`<base_url>/api/v2`），请求头只需要 `X-Emp-No` + `X-Uac-Token` 两个鉴权参数，不需要浏览器 Cookie。
- 凭据优先取 OpenClaw 注入的环境变量 `coclaw_empno` / `coclaw_token`；也可以用本技能专用环境变量 `FOUR0S_CCA_EMP_NO` / `FOUR0S_CCA_UAC_TOKEN`，或写进 `config/config.yaml` 的 `cca.emp_no` / `cca.uac_token`。
- 项目与平台配置在 `config/config.yaml`（已被 `.gitignore` 忽略）；字段说明与模板见 `config/config.example.yaml`。
- 最小配置：`cca.base_url` + 凭据 + 各项目的 `name`/`local_path`/`branch`；「4个0」只要再配 `cca.project_id` 与 `kw_id`/`coverity_id`，Hub 只要再配 `cca.hub.project_id` 与 `hub_id`，其余（口径、版本级指标、下载位置）都有默认值或可选。
- 实现按职责拆在 `scripts/four0s/` 包内（`constants` / `config` / `cca` / `issues` / `hub` / `gitops` / `report`），`scripts/scan_leaks.py` 只是 CLI 入口。
- 下列命令都在技能根目录执行。

## 步骤（按顺序执行）

### 1. 校验配置与凭据

```bash
python3 scripts/scan_leaks.py check
```

退出码为 0 才能进入下一步：

- 退出码 2（配置不完整）：把脚本列出的缺失项转述给用户，请其补全 `config/config.yaml`；凭据缺失时优先确认是否在 OpenClaw 环境里运行（OpenClaw 会注入 `coclaw_empno` / `coclaw_token`），也可以改用环境变量 `FOUR0S_CCA_EMP_NO` / `FOUR0S_CCA_UAC_TOKEN`，补齐后重新执行本步。
- 退出码 3（凭据无效）：告知用户 `X-Emp-No` / `X-Uac-Token` 无效或已失效，请其重新提供员工号与 UAC token。不要试图绕过鉴权，也不要在凭据无效时继续抓取。

### 2. 扫描

```bash
python3 scripts/scan_leaks.py scan
```

- 脚本会先同步本地仓库（按需 stash → 检出配置分支 → pull），再抓取缺陷，并用 `git blame` 补全提交者。每条 git 命令都有超时（`sync.timeout`，默认 180 秒）并带 ssh 保活，远端静默不会让扫描挂死：拉取失败只在 `warnings` 里报告，该仓库降级为按本地现有代码分析。
- 用户不希望改动本地仓库时加 `--no-sync`；只想看某个仓库时加 `--project <仓库名>`；需要完整 JSON 时加 `--json`。
- 同一轮里会顺带汇总 Hub，最小配置只有 `cca.hub.project_id`（Hub 任务所在项目，所有微服务共用）+ 各 `projects[].hub_id`（每个微服务一个任务）：配好就会自动下载任务报告并解析出组件明细；再加可选 `cca.hub.version_name` 才有版本级指标。只想跑 Hub 时用 `python3 scripts/scan_leaks.py hub`；不想跑 Hub 时给 `scan` 加 `--no-hub`；只用本地报告不联网下载时加 `--no-hub-download`。
- 结果 JSON/CSV 默认写到命令运行时的当前目录，可用 `output.dir` 或 `--out-dir <目录>` 指定；JSON 含 `summary`、`leaks`、`warnings`，字段说明见 `references/workflow.md`。在会话中执行时建议把 `--out-dir` 指到本次任务的临时目录，避免产物留在仓库工作区。

Hub 单独跑（不碰 Klocwork/Coverity，也不动本地仓库）—— 只要有 `cca.hub.project_id` 加一个任务 ID 就能跑：

```bash
python3 scripts/scan_leaks.py hub --out-dir <临时目录>          # 用 projects[].hub_id
# 临时指定 Hub 任务（自动下载该任务报告，可重复）：
python3 scripts/scan_leaks.py hub --task "https://cca.zte.com.cn/workbench/project/<project_id>/task/<task_id>/executions"
python3 scripts/scan_leaks.py hub --task <task_id>              # 只给任务 ID 也能拼
# 只看版本级指标，不下载任务报告：
python3 scripts/scan_leaks.py hub --no-download
```

### 3. 展示结果、给出处置建议，并停下来等确认（硬性要求）

按下面的**固定模板**渲染，每次都保持同样的结构与顺序；空的部分整段省略，不要补写说明。

```
「4个0」+ Hub 扫描结果
执行时间：<YYYY-MM-DD HH:MM> ｜ 分支：<仓库=分支 列表>

## 一、「4个0」缺陷
| 工具 | 级别 | 数量 |
| Klocwork | Critical | n |
| Klocwork | Error | n |
| Coverity | High | n |
| Coverity | Medium | n |

（合计 > 0 时才输出）缺陷明细：项目 | 类别 | 级别 | 文件:行 | 问题描述 | 提交者 | CCA 链接 | 处置建议
（合计 > 0 时才输出）处置分布：建议修复 X · 建议备案 Y · 待确认 Z

## 二、处置建议（合计 > 0 时才有）
逐条结论 + 理由；建议备案的附「备案说明」引用块。

## 三、Hub 开源组件漏洞（仅当脚本输出了待处理组件时才有）
直接沿用脚本输出的组件列表（任务 / 组件 / 版本 / 漏洞等级 / 漏洞数 / 引入方式 / Origin id）+ 处置建议。

## 四、需要你确认（仅当确实有待处理事项时才有）
只列待处理的项。

产物：<out-dir>
```

渲染规则（不要违反）：

- 四类全为 0 且 Hub 无待处理时：只输出「一」的统计表 + 一句「四类均为 0，Hub 无待处理项」+ 产物路径，**不出现** 二/三/四 段落。
- Hub 无待处理时不要出现任何 Hub 内容，也不要解释为什么没有、不要提平台指标/核对结果/合规数量。
- 低危（`Vulnerability.Low`）与已备案（`IGNORED`）的项：不列出、不解释、不建议补充扫描或其它处理。
- 不要输出「备案后不再计入口径」之类的科普，除非用户主动问。
- Hub 段落只在脚本输出待处理组件时出现，内容直接沿用脚本输出，不自行添加指标表、核对表或口径说明。

Hub 明细里的字段含义：`Origin id` 是 Maven 坐标，`引入方式`（直接引入 / 传递引入 + 一级依赖）决定处置路径 —— 直接引入的升 `pom` 版本，传递引入的先看一级依赖能否对齐或排除。

给出「处置建议」前先只读地打开缺陷所在文件的上下文，逐条判断属于哪种处置：

- **建议修复**：代码确实有问题，且能在不改变对外行为/接口契约的前提下用最小改动消除。
- **建议备案**：判定为误报，或属于受控输入、设计取舍、依赖约束等无法在代码层安全消除的情况（硬改会破坏接口契约、引入无效代码，或只是为过扫描造痕迹）。
- **待确认**：定位不到文件、读不到上下文或信息不足，写清缺什么信息，不要凭印象下结论。

同一 `rule` 的同类问题保持同一口径；表格里每条问题给一句结论性理由，末尾给分布统计（建议修复 X 条、建议备案 Y 条、待确认 Z 条）。

**建议备案的问题必须同时给出可直接使用的「备案说明」**，包含以下要素：

1. 问题类别与位置（用 CCA 链接指代，不要贴代码）；
2. 判定依据：结合代码逻辑或数据流说明为什么是误报或可接受；
3. 影响与风险评估：最坏后果是什么、为什么可接受；
4. 不修改代码的理由；
5. 重新评估的条件：出现哪些变更后需要重新判断。

备案动作由用户在 CCA 平台上完成，技能只产出说明文本、不代操作。

**有东西要处理时才停下征求确认**（「一」有缺陷，或「三」有 Hub 待处理组件）：问清「修哪些、备案哪些」。两处都为空时，给出结论即结束，不要反问、不要建议补充扫描或其它后续动作。在用户答复之前，不要修改任何代码，也不要提交任何改动。

### 4. 修复（用户确认后）

- 动手前先读 `references/workflow.md` 的「修复规范」。
- 用户选择「备案」的问题保持代码原样，把第 3 步的备案说明交给用户在平台上登记；不要为了消掉告警去改代码。
- 先按项目、再按文件聚合问题，每个问题都要先看代码上下文，只做消除缺陷所需的最小改动。
- Hub 组件漏洞按同样的原则处理：直接引用的用输出的 `Origin id` 升到修复版本（必要时用 `mvn dependency:tree` 复核），传递引入的看「一级依赖」是谁带进来的——能通过排除/对齐上游版本解决就改，属于框架基线的则走备案；升级后按「版本升级类型与风险」评估兼容性。
- 修完自检：能跑 lint／单测／构建就跑，并如实汇报结果。
- 不要自动提交或推送代码；用户要求提交时，按用户指示走 `git-push` 技能。
- 无法安全修复的问题（涉及接口契约、产品决策等）单独列出交给用户判断。

### 5. 归档（技能自进化）

修复完成后，把本轮修复的问题按 `references/fix-archive.md` 的规则追加进去：一个类别一条记录，只写「问题类别 + 通用解决方案」；本轮确认备案的问题也一并归档（只写问题类别 + 通用备案依据）。两者都要遵守该文件的脱敏要求（不写仓库名、文件路径、代码片段、行号、漏洞 ID、任务链接、分支名、提交者等）。

## 参考文件

- `references/workflow.md`：配置字段、命令与退出码、结果字段、修复规范、验收与排障。
- `references/fix-archive.md`：已积累的修复经验、归档格式与脱敏规则。
