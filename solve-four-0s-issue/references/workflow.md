# 「4个0」扫描与修复详细流程

`SKILL.md` 给出主流程，本文件是执行细节：配置怎么填、脚本怎么用、结果怎么看、问题怎么修、失败怎么排。

## 1. 目录与文件职责

| 路径 | 职责 |
| --- | --- |
| `config/config.yaml` | 本机真实配置（可含 `cca.emp_no` / `cca.uac_token` 凭据），已被 `.gitignore` 忽略，**不要提交** |
| `config/config.example.yaml` | 配置模板与字段说明，随技能一起版本管理 |
| `scripts/scan_leaks.py` | CLI 入口：参数解析、子命令编排（`check` / `check-auth` / `scan` / `hub`） |
| `scripts/four0s/constants.py` | 环境变量名、占位符、API 前缀、分页与超时等常量 |
| `scripts/four0s/config.py` | 配置加载、校验、项目筛选 |
| `scripts/four0s/cca.py` | CCA 开放 API 客户端（只带 `X-Emp-No` + `X-Uac-Token`） |
| `scripts/four0s/issues.py` | Klocwork / Coverity 缺陷解析与抓取 |
| `scripts/four0s/hub.py` | Hub：版本级统计（在线）+ 报告 Excel 组件明细（离线） |
| `scripts/four0s/gitops.py` | 本地仓库同步与 `git blame` |
| `scripts/four0s/report.py` | 结果汇总、落盘与终端输出 |
| `references/workflow.md` | 本文件：执行细节 |
| `references/fix-archive.md` | 自进化归档：只记录问题类别与解决方案（已脱敏） |

模块边界：只有 `cca.py` 发 HTTP 请求；`hub.py` 在线部分复用 `cca.api_post`，离线部分只读本地 Excel；`report.py` 不做 I/O 之外的业务判断。

## 2. 配置说明

配置从脚本里拆出来放在 `config/config.yaml`，字段含义如下（模板里也有同样的注释）。

**最小配置**：`cca.base_url` + 凭据 + `projects[].name`/`local_path`/`branch` 必填；
其余都有默认值或可不配 ——

- 只做「4个0」：每个项目配 `kw_id` / `coverity_id`，`cca.project_id` 填这两个任务所在项目；
  `cca.kw.priorities`、`cca.kw.status_filter`、`cca.coverity.priorities`、
  `cca.coverity.only_unclassified` 不写就按标准口径（Critical/Error、High/Medium、Analyze、Unclassified）。
- 只做 Hub：`cca.hub.project_id` + `projects[].hub_id`，此时连 `cca.project_id` 都不用配；
  版本级指标（`cca.hub.version_name`）是可选增强。

| 字段 | 含义 |
| --- | --- |
| `cca.base_url` | CCA 平台地址，通常为 `https://cca.zte.com.cn` |
| `cca.project_id` | Klocwork/Coverity 任务所在项目 ID；只做 Hub 时可以不配 |
| `cca.emp_no` | 员工号，作为请求头 `X-Emp-No`；已由技能/环境变量提供时可留空 |
| `cca.uac_token` | UAC token，作为请求头 `X-Uac-Token`；已由技能/环境变量提供时可留空 |
| `cca.kw.priorities` | 可选，Klocwork 需要清零的优先级，默认 `["Critical", "Error"]` |
| `cca.coverity.priorities` | 可选，Coverity 需要清零的优先级，默认 `["High", "Medium"]` |
| `cca.kw.status_filter` | 可选，只统计这些状态的 Klocwork 缺陷，默认 `["Analyze"]`（已备案/已处理的不算） |
| `cca.coverity.only_unclassified` | 可选，为 `true`（默认）时只统计 `classification=Unclassified` 的 Coverity 缺陷 |
| `cca.hub.project_id` | 【最小配置】Hub 任务所在项目 ID，所有微服务共用一个（任务链接里的 project 段） |
| `projects[].hub_id` | 【最小配置】该微服务的 Hub 任务 ID；留空表示这个仓库不查 Hub |
| `cca.hub.version_name` | 可选。Hub 版本号（写完整版本名）；有值才拉版本级指标，留空只出组件明细 |
| `cca.hub.download_dir` | 可选。任务报告下载解压根目录；实际落点是 `<该值 或 output.dir 或当前目录>/hub_download/<project>_<task>/` |
| `cca.hub.ignore_status` | 视为已处置、不计入的修复状态，默认 `["IGNORED"]`（与平台口径一致） |
| `projects[].name` | 仓库名，必须与 CCA 返回文件路径里的仓库目录名一致（如 `zte-aiop-aiservice-appui`） |
| `projects[].local_path` | 本地仓库绝对路径，用于切换分支和 `git blame` |
| `projects[].branch` | 需要在本地检出的分支 |
| `projects[].kw_id` | 该仓库在 CCA 上的 Klocwork 任务 ID，没有就留空 |
| `projects[].coverity_id` | 该仓库在 CCA 上的 Coverity 任务 ID，没有就留空 |
| `projects[].enabled` | `false` 时跳过该仓库 |
| `sync.enabled` | `false` 时不 stash/pull/checkout，直接分析当前工作区 |
| `sync.stash_before_pull` | 拉取前是否 `git stash` 本地改动 |
| `sync.pull` | 是否 `pull` 远端 |
| `sync.blame` | 是否用 `git blame` 补全提交者 |
| `sync.timeout` | 单条 git 命令超时秒数（默认 180）。超时按进程组终止，并把该仓库降级为「按本地现有代码分析」 |
| `sync.ssh_keepalive` | 为 ssh 追加 `ConnectTimeout`/`ServerAliveInterval`，远端静默时快速失败而不是挂死 |
| `output.dir` | 扫描结果（JSON/CSV）输出目录，**可以不配置**；缺省时写到执行 `scan` 命令时的当前目录 |
| `request.timeout` / `request.retries` | 单请求超时秒数与重试次数 |

凭据获取方式（`X-Emp-No` 员工号 + `X-Uac-Token`，按优先级）：

1. OpenClaw 注入的环境变量 `coclaw_empno` / `coclaw_token`——在 OpenClaw 里运行时通常零配置；
2. 本技能专用环境变量 `FOUR0S_CCA_EMP_NO` / `FOUR0S_CCA_UAC_TOKEN`；
3. `config/config.yaml` 的 `cca.emp_no` / `cca.uac_token`；
4. 命令行 `--emp-no` / `--uac-token`（优先级最高，需写在子命令之前，如 `python3 scripts/scan_leaks.py --uac-token <token> check`）。

不想把凭据写进文件时，用环境变量提供：

```bash
export FOUR0S_CCA_EMP_NO='0668001277'
export FOUR0S_CCA_UAC_TOKEN='<UAC token>'
python3 scripts/scan_leaks.py check
```

脚本走 CCA 开放 API（`<base_url>/api/v2`），只带 `X-Emp-No` + `X-Uac-Token` 两个鉴权头，浏览器 Cookie / `x-csrf-token` 已不再使用。

任务 ID 与项目 ID 都从 CCA 任务链接里取：`https://cca.zte.com.cn/workbench/project/<project_id>/task/<任务ID>/...`。任务与仓库的对应关系可用扫描结果里的仓库名反向确认（脚本会把不一致写进 `warnings`）。iCenter 上的说明文档需用 `edw` / `edw-space` 技能查阅，不要用 `web-fetch`，例如：

```
https://i.zte.com.cn/#/shared/45e2844136c54b86a105b695eaef116b/wiki/page/76faeb0816ab11f1b6d6f79c8307e304/view
```

## 3. 命令

所有命令都在技能根目录执行（`cd <skill 目录>`）：

```bash
python3 scripts/scan_leaks.py check                  # 校验配置完整性 + 凭据是否有效
python3 scripts/scan_leaks.py check-auth             # 只校验凭据
python3 scripts/scan_leaks.py scan                   # 全量扫描（默认会同步本地仓库）
python3 scripts/scan_leaks.py scan --project <仓库名> # 只扫某个仓库，可重复
python3 scripts/scan_leaks.py scan --no-sync          # 不动本地仓库，只抓取并分析
python3 scripts/scan_leaks.py scan --json             # 额外打印完整 JSON
python3 scripts/scan_leaks.py scan --fail-on-leak     # 有问题时退出码为 1，便于流水线判断
python3 scripts/scan_leaks.py scan --out-dir /path/to/results  # 指定结果目录
python3 scripts/scan_leaks.py scan --no-hub            # 本轮不跑 Hub
python3 scripts/scan_leaks.py scan --hub-version XYUAC-AICSV1.26.33  # 临时指定 Hub 版本号
python3 scripts/scan_leaks.py scan --hub-task <Hub 任务链接>          # 临时补一个 Hub 任务（会自动下载报告）
python3 scripts/scan_leaks.py hub --version XYUAC-AICSV1.26.33       # 只跑 Hub（不抓代码缺陷、不动本地仓库）
python3 scripts/scan_leaks.py hub --task <Hub 任务链接>               # 只跑 Hub 并下载指定任务报告
python3 scripts/scan_leaks.py hub --no-download        # 只看版本级指标，不下载任务报告
```

退出码：`0` 成功；`1` 发现问题（仅 `--fail-on-leak`）；`2` 配置不完整；`3` 凭据无效；`4` 运行出错。

`scan` 会先做配置校验和凭据校验，任一不通过都会直接退出，不会抓取数据。

同步阶段对每个仓库执行：按需 `git stash`（仅跟踪文件）→ `git checkout <配置分支>` → `git pull --ff-only`。每条命令都有超时（`sync.timeout`，默认 180 秒）并按进程组终止，另外给 ssh 加了保活参数，所以远端 Gerrit 静默时不会无限等待：超时或拉取失败只写进 `warnings`，该仓库降级为按本地现有代码分析，其余仓库继续。注意进度日志在 `scan` 结束前就会实时输出，看到长时间不动时不要急着 kill，先看是否有 `[sync]` 日志。

结果目录优先级为 `--out-dir` > `output.dir` > 当前工作目录。默认落到当前目录，所以从技能根目录直接运行时结果会出现在技能目录里；在会话中执行时建议显式指定到临时目录（例如 `--out-dir /media/vdc/0668001277/workspace/tmp/solve-four-0s-issue`），避免把扫描产物留在仓库工作区。

## 4. 结果结构

结果同时落盘为 `leaks_result_<时间戳>.json` 和 `.csv`，JSON 关键字段：

| 字段 | 含义 |
| --- | --- |
| `summary.targets` | 「4个0」四类问题的数量与口径 |
| `summary.allZero` | 四类问题是否全部清零 |
| `summary.byProject` / `summary.byType` | 按仓库 / 按工具的分布 |
| `warnings` | 抓取失败、仓库名与配置不一致等需要人工确认的信息 |
| `leaks[].projectName` | 缺陷所属仓库，用它在 `projects` 里找到 `local_path` 与 `branch` |
| `leaks[].relativePath` | 仓库内相对路径，配合 `local_path` 得到真实文件 |
| `leaks[].lineNumber` | 行号（`Various` 时会尝试从标题里解析） |
| `leaks[].rule` | 问题类别（如 `JS.BASE.EQEQEQ`、`NO_EFFECT`），归档时用它做分类键 |
| `leaks[].classification` / `description` | 原始标题与描述，用于判断问题性质 |
| `leaks[].committer` | 通过 `git blame` 得到的提交者（同步失败或无法定位时为空） |
| `leaks[].taskInfo` | CCA 上的问题链接，可直接给用户点击 |
| `summary.hub` | Hub 结论：`allPass`、`blocking` / `alarming` / `passing` 指标、各级别数量、组件数与漏洞数、按任务统计 |
| `hub.summary` | Hub 在线统计原文结构：`levels` / `levelsRecorded`(备案数) / `indicators` / `tasks` / `reportId` / `reportUrl` |
| `hub.levels` / `hub.levelsApi` / `hub.levelDiff` | 报告算出的各级别数量、平台版本级数量、两者差值（用于对账） |
| `hub.components` | 待处理漏洞组件：`taskName`(微服务) / `componentName` / `componentVersion` / `vulnerabilityLevel` / `licenseLevel` / `vulnerabilityCount` / `vulnerabilities` / `originId` / `introduction`（引入方式） |
| `hub.components[].introduction` | `kind`（直接引入/传递引入/仅制品内匹配）、`firstLevel`（一级依赖 GAV）、`parents`、`chains`（最多 3 条 GAV 链） |
| `hub.compliance` | 合规风险组件（License 中/高危且未评审），与漏洞维度分开，不参与漏洞结论 |
| `hub.filtered` | 被规则过滤掉的数量：低危、已忽略行、合规风险、待处理 |
| `hub.byTask` | 按任务的组件数、有漏洞组件数、漏洞条目数、各级别数量 |
| `hub.tasks` / `hub.downloadedFiles` | 本轮涉及的 Hub 任务，以及从 CCA 下载解压出的报告文件 |
| `hub.reportVersions` | 报告首页里的版本号（未配 `cca.hub.version_name` 时，可据此补配置） |
| `hub.errors` | Hub 相关告警（版本无数据、Excel 打不开、缺少版本号等） |

另外会多写一份 `hub_components_<时间戳>.csv`（仅当解析出组件明细时），列与 `hub.components` 一致。

### 4.1 Hub 数据来源与口径

Hub 有两条通路：

1. **版本级（在线，只读）**
   `GET /api/v2/vmsprojects/versions?versionName=xxx` 拿版本 ID，再
   `POST /api/v2/zte-rdcloud-icode-dataserver/statistic/version-summary-statistic?action=query`
   （body：`{"key":"versionId","value":"<id>","toolName":"hub","dimensionName":"versionSummary"}`）
   拿各级别数量与 `indicatorReport`。返回的 `statusStatistics` 形如
   `License.High/Medium/Low/Ok`、`Vulnerability.Critical/High/Medium/Low/Ok`，
   每项带 `recordTotal`（备案数）；`indicatorReport` 把开源指标分成
   `blockList` / `alarmList` / `passList`，阈值都是 0，`allPass` 即三组中前两组为空。
   `taskSummaryStatistics[]` 给到任务级数量，但只有 `modelId` / `resultCollectionId`，
   **没有任务名**，所以在线通路定位不到微服务与组件。
2. **任务报告（在线下载，组件明细的唯一来源，也是最小配置）**
   `GET /api/v2/projects/{projectId}/{taskId}/getSingleTaskReport` 返回报告 zip，
   同样只带 `X-Emp-No` + `X-Uac-Token`。配置只有两项：
   `cca.hub.project_id`（Hub 任务所在项目，所有微服务共用一个）+ `projects[].hub_id`
   （每个微服务一个任务，从任务链接 `/workbench/project/<project_id>/task/<task_id>/executions` 取）；
   `--hub-task` 可临时补任务（支持整条链接，也支持只给任务 ID）。
   解压目录为 `<download_dir 或 output.dir 或当前目录>/hub_download/<project>_<task>/`，
   每次重跑先清空该任务自己的目录，避免新旧报告混在一起。
   **解析口径**：组件全集取「开源软件BOM清单」表（含无漏洞组件，对应平台的 Ok 计数）；
   「漏洞」表里剔除 `ignore_status`（默认 IGNORED）后，按 `组件ID+版本ID` 取最高危险等级作为
   该组件的 `Vulnerability.*`；合规等级取 BOM 的「合规风险等级」列（配合「Review status」判断是否已评审）
   作为 `License.*`；同时按 `组件ID+版本ID` 关联 `Origin id`（Maven 坐标）、漏洞号去重。
   若报告带「匹配路径信息」表，还会按同一对键补上**引入方式**：`Match type=FILE_DEPENDENCY_DIRECT`
   记为「直接引入」，`FILE_DEPENDENCY_TRANSITIVE` 记为「传递引入」并解析 GAV 链，
   取链上第一段作为**一级依赖**（谁把它带进来的），用于判断「改 `pom`」还是「走备案」；
   该表缺失（例如 webui 的空报告）时静默跳过，不影响其它结论。
   同一个任务上「报告算出来的 levels」应与平台任务级 `taskSummaryStatistics` 一致，
   `hub.levelsReport` / `hub.levelsApi` / `hub.levelDiff` 用来对平。
   **输出过滤规则**（脚本已实现，展示时不要绕过）：
   - 低危（`Vulnerability.Low`）不体现，即使接口有数量；
   - 中/高/严重漏洞若报告里状态为 `IGNORED`（已在平台备案），不体现；
   - 明细只保留「中/高/严重 且报告里未处置」的组件（`hub.components`）；
   - 合规风险（`License.Medium/High` 且 `Review status != REVIEWED`）是与漏洞独立的维度，
     只输出数量与 `hub.compliance`，不进漏洞明细。
   被过滤掉的数量统计在 `hub.filtered` 与 `hub.byTask[].actionable` 里，便于对账。

   **静默规则**：`scan` 在没有待处理组件时**完全不输出 Hub 段落**（连指标、核对结果、合规数量都不打），
   也不会提示补配 `version_name`；只有出现待处理组件或抓取/解析报错时才输出。
   `hub` 子命令是显式查看 Hub，始终输出完整信息。
   ⚠️ Hub 任务与 KW/Coverity **不在同一个 CCA 项目**（例如某产品是 `7svnaxMR`），
   而且每个微服务一个 Hub 任务，所以要逐个配。

**报告格式**：CCA 上的 Hub 报告文件名常写成 `.xlsx`，内容却是 OLE2/.xls，
所以解析按文件头判断（`PK` → openpyxl，`D0CF11E0` → xlrd），不看扩展名，
两种依赖都要装：`pip install openpyxl xlrd`。

为什么组件级只能靠报告：CCA 开放 API 的 Hub 明细行只给 `esIssueId/esDataId/hash`，
`issue-detail-with-code` 对 `toolType=hub` 直接返回 `ExceptionHandler-Caused-by:null`
（`coverity` / `kwserver` 同一接口正常），前端也只渲染 Hub 的环形图与趋势图。
也就是说「指标是否达标」用版本统计最省事，「改哪个 pom 依赖」必须靠任务报告。

报告里与定责最相关的是 `Origin id`（Maven 坐标，如
`org.apache.tomcat.embed:tomcat-embed-core:9.0.108`）和最高危险等级，据此可以直接判断
是升框架还是改 `pom` 覆盖版本。

## 5. 处置建议与修复规范

### 5.1 先给「修复还是备案」的结论

展示阶段（`SKILL.md` 第 3 步）不能只丢问题清单，必须带着结论来。逐条按下面的流程定：

1. 只读地打开缺陷所在文件的上下文（不改代码），把缺陷描述与代码逐条对上；对不上就先向用户说明，不要猜着改。
2. 能定位、确认是真实缺陷，且能在不改对外行为的前提下最小改动消除 → **建议修复**，并说明打算改哪里。
3. 属于误报，或因受控输入、设计取舍、依赖约束等原因无法在代码层安全消除 → **建议备案**，同时按 5.2 给出备案说明。
4. 定位不到、上下文不足或需要业务决策 → **待确认**，写清缺的是哪类信息（例如接口文档、部署环境、产品口径），不要凭印象下结论。
5. 同一 `rule` 的同类问题口径保持一致；表格里每条一句话说明理由，末尾给统计：建议修复 X 条、建议备案 Y 条、待确认 Z 条。
6. Hub 组件漏洞单独成段给结论：先按 `mvn dependency:tree` 判断组件是 MSA 框架引入还是微服务自行引入；自行引入的给「升级到哪个版本」的修复建议，框架引入且框架无新版本的给备案建议，只有版本级指标、拿不到组件时如实说明需要 Hub 报告 Excel。

改动代价大的问题（牵动对外接口、数据格式或业务流程）即使属于真实缺陷，也要先把代价讲清楚再给结论，由用户决定修还是备案。

### 5.2 备案说明写什么

备案说明要写成一段可直接粘贴进 CCA 备案输入框的文字，覆盖五点：

1. 问题类别与位置（用 CCA 链接指代，不贴代码）；
2. 判定依据：结合代码逻辑或数据流说明为什么是误报或可接受；
3. 影响与风险评估：最坏后果、为什么在本场景可接受；
4. 不修改代码的理由；
5. 重新评估的条件：哪些变更出现后需要重新判断。

- 备案是用户在 CCA 平台上的动作，技能只产出说明文本；用户要求技能直接改备案状态时，如实说明做不到。
- 备案后该问题不再进入「4个0」口径：Klocwork 的 `status` 不再是 `Analyze`（对应 `cca.kw.status_filter`），Coverity 的 `classification` 不再是 `Unclassified`（对应 `cca.coverity.only_unclassified`）。因此对误报来说备案是正当的清零路径，代码里造痕迹绕过不是。
- 备案说明本身也要守脱敏要求：不写仓库名、路径、代码片段、漏洞 ID、任务链接、提交者等。

### 5.3 修复规范

1. 先按 `projectName` 分组，再按文件聚合，一个文件里的多个问题一次改完，避免反复切文件。
2. 每个问题都要打开对应代码行的上下文再动手，确认缺陷描述与代码一致；描述与代码对不上时先向用户说明，不要猜着改。
3. 只做消除该缺陷所需的最小改动，不要顺手重构、改格式、动无关代码。
4. 不要加“为通过扫描而绕过”的写法（例如无意义的空判断、把变量强行挪走）；确实属于误报时，向用户说明并建议在 CCA 上做备案，而不是在代码里造痕迹。
5. 用户选择备案的问题不要动代码，只把 5.2 的备案说明交给用户在平台上登记。
6. 修复过程中不要提交/推送代码。用户要求提交时，按用户指示走 `git-push` 技能（或用户的常规提交流程）。
7. 无法安全修复的问题（需要改接口契约、需要产品决策）要单独列出来交给用户，并说明原因。
8. 修完自检：能跑 lint / 单测 / 构建就跑一遍，把结果如实汇报；跑不了要说明。
9. Hub 组件升级同样只动 `pom.xml` 里对应的版本（`properties` 或 `dependencyManagement`），升完用 `mvn dependency:tree | grep <组件>` 确认版本生效，并评估兼容性；框架引入且框架无新版本的组件不要擅自覆盖。

## 6. 验收

修复后重新执行 `python3 scripts/scan_leaks.py scan`（必要时加 `--project <仓库名>`），用新的 JSON 结果确认对应类别数量下降或归零。CCA 的扫描任务是异步的，本地代码改完不等于平台数据立即更新：若结果没有变化，先确认改动是否已提交到被扫描的分支，再判断是否需要等待平台重新扫描。

Hub 的验收分两半：版本级指标用 `python3 scripts/scan_leaks.py hub --version <版本号>` 复跑，看 `blocking` / `alarming` 是否清空；组件级明细要等对应 Hub 任务重跑出新报告后再跑一次 `hub`（技能会重新下载覆盖本地那份），因为改本地 `pom` 不会立刻改变平台上的报告。

## 7. 归档（自进化）

每轮结束后，把本轮实际修复的问题、以及经用户确认的备案问题，按 `references/fix-archive.md` 的规则追加到该文件：

- 一个类别一条记录，类别用 `leaks[].rule`；
- 修复项写「问题类别 + 解决方案（通用手法）」，备案项写「问题类别 + 通用备案依据」；
- 内容只有上述信息以及次数/时间这类无敏感信息的记账字段；
- 禁止写入仓库名、文件路径/文件名、代码片段、行号、漏洞 ID、任务链接、分支名、提交者姓名工号、凭据。

归档前先检索该类别是否已存在：存在则合并补充，不存在才新增。

## 8. 排障

| 现象 | 处理 |
| --- | --- |
| `凭据检查：不通过 —— CCA 凭据无效或权限不足（HTTP 401/403）` | `X-Emp-No` / `X-Uac-Token` 无效或已失效：确认是否在 OpenClaw 环境里运行（OpenClaw 会注入 `coclaw_empno` / `coclaw_token`），或更新环境变量 `FOUR0S_CCA_EMP_NO` / `FOUR0S_CCA_UAC_TOKEN`、`config.yaml` 的 `cca.emp_no` / `cca.uac_token` 后重试 |
| `凭据检查：不通过 —— 请求被重定向到登录页` | 同上，鉴权已失效；脚本不会绕过登录 |
| 接口返回 400 / 返回非 JSON | CCA 开放 API `/api/v2` 的路径或参数与平台版本不一致，或请求被网关拦截；对照 `Studio-CCACodeScan` 技能的 `references/REFERENCE.md` 核对接口 |
| `local_path 不存在或不是目录` | 用提示的路径克隆仓库，或修正配置 |
| `warnings` 里出现“仓库名与配置不一致” | 任务 ID 配错了，用扫描结果里的仓库名核对 `projects[].kw_id` / `coverity_id` |
| `版本名 X 匹配到 N 个版本` | `cca.hub.version_name` 写得不够完整（接口是模糊匹配），补全完整版本号后重试 |
| Hub 显示“该版本没有 Hub 报告数据” | 该版本未绑定 Hub 任务或报告未生成；换版本号，或先到 CCA 上确认 Hub 报告存在 |
| Hub 组件明细为空 | 三种情况分开看：① 没配 `projects[].hub_id`/`--hub-task` 或用了 `--no-download`；② 任务没有报告（`hub.errors` 里会写「下载 Hub 报告失败」）；③ 报告里非 IGNORED 的漏洞记录为空（日志会写「组件 N 个，剔除 IGNORED 状态后无待处理漏洞」） |
| 报告与平台指标不一致（`levelDiff` 非 0） | 先看是否所有 Hub 任务都配了 `hub_id`（少配的任务会造成差额，日志会提示）；都配齐后仍有小额差异，则是平台「任务级求和」与「版本级汇总」本身的口径差异（版本级可能跨任务去重/时间点不同），以平台版本级为准 |
| `配置了 projects[].hub_id 但缺少 cca.hub.project_id` | 补上 Hub 项目 ID（任务链接里的 project 段），所有微服务共用同一个 |
| `下载 Hub 报告失败（…）` | 任务链接抄错（project_id/task_id），或该任务在本账号无权限；到 CCA 任务页复制链接后重试 |
| `解析 xlsx 报告需要 openpyxl` | `pip install openpyxl` 后重试 |
| `解析 Hub 的 .xls 报告需要 xlrd` | `pip install xlrd` 后重试（CCA 的 Hub 报告常是伪装成 `.xlsx` 的老 `.xls`） |
| `无法识别的报告格式` | 该文件既不是 ZIP 也不是 OLE2，通常是下载到了登录页/错误页；重新下载报告 |
| Hub 在线指标为 0 但 Excel 里有组件 | 指标按 CCA 口径统计（剔除 LOW 等），与报告 Excel 的原始行数不同；以 `indicatorReport` 与 Excel 解析结果分别说明 |
| `git` 同步失败（本地改动、分支不存在、无 upstream） | 该仓库会被跳过并记入 `warnings`，其余仓库照常；把原因原样告知用户，或用 `--no-sync` 先出结果 |
| 同步阶段长时间没有输出、像是卡住 | 现在有超时兜底，不会无限等待：等 `sync.timeout`（默认 180 秒）到点即可看到 `[sync]` 降级日志。若确实需要拉取，调大 `sync.timeout`；若不需要更新代码，用 `--no-sync` |
| 想确认 ssh 是否真的在传输数据 | 查看连接状态 `ss -tni \| grep -A1 29418`，对比 `lastrcv`/`lastsnd`；保活参数生效后静默连接会在约 60 秒内被 ssh 自行断开 |
| 结果为空但用户认为有问题 | 检查 `cca.kw.status_filter` / `cca.coverity.only_unclassified` 口径，以及任务最近一次执行时间 |
