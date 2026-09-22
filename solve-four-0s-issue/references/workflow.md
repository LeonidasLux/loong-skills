# 「4个0」扫描与修复详细流程

`SKILL.md` 给出主流程，本文件是执行细节：配置怎么填、脚本怎么用、结果怎么看、问题怎么修、失败怎么排。

## 1. 目录与文件职责

| 路径 | 职责 |
| --- | --- |
| `config/config.yaml` | 本机真实配置（含 cookie/csrf），已被 `.gitignore` 忽略，**不要提交** |
| `config/config.example.yaml` | 配置模板与字段说明，随技能一起版本管理 |
| `scripts/scan_leaks.py` | 配置校验、cookie 校验、缺陷抓取、结果落盘 |
| `references/workflow.md` | 本文件：执行细节 |
| `references/fix-archive.md` | 自进化归档：只记录问题类别与解决方案（已脱敏） |

## 2. 配置说明

配置从脚本里拆出来放在 `config/config.yaml`，字段含义如下（模板里也有同样的注释）：

| 字段 | 含义 |
| --- | --- |
| `cca.base_url` | CCA 平台地址，通常为 `https://cca.zte.com.cn` |
| `cca.project_id` | CCA 项目 ID，取任务链接中 `/workbench/project/<project_id>/` 一段 |
| `cca.cookie` | 登录 CCA 后浏览器里的完整 Cookie 字符串 |
| `cca.csrf` | 请求头 `x-csrf-token` 的值 |
| `cca.kw.tool` / `cca.coverity.tool` | 缺陷列表接口的 `tool` 参数（Klocwork: `kwServer-2020.4`，Coverity: `coverity-20230302`） |
| `cca.kw.priorities` | Klocwork 需要清零的优先级，默认 `["Critical", "Error"]` |
| `cca.coverity.priorities` | Coverity 需要清零的优先级，默认 `["High", "Medium"]` |
| `cca.kw.status_filter` | 只统计这些状态的 Klocwork 缺陷，默认 `["Analyze"]`（已备案/已处理的不算） |
| `cca.coverity.only_unclassified` | 为 `true` 时只统计 `classification=Unclassified` 的 Coverity 缺陷 |
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

获取凭据的方式：浏览器登录 CCA，打开开发者工具 → Network → 任选一个 `cca.zte.com.cn` 请求，复制请求头里的 `Cookie` 与 `x-csrf-token`。

不想把凭据写进文件时，用环境变量覆盖同名配置：

```bash
export FOUR0S_CCA_COOKIE='<粘贴 cookie>'
export FOUR0S_CCA_CSRF='<粘贴 csrf>'
python3 scripts/scan_leaks.py check
```

任务 ID 与项目 ID 都从 CCA 任务链接里取：`https://cca.zte.com.cn/workbench/project/<project_id>/task/<任务ID>/...`。任务与仓库的对应关系可用扫描结果里的仓库名反向确认（脚本会把不一致写进 `warnings`）。iCenter 上的说明文档需用 `edw` / `edw-space` 技能查阅，不要用 `web-fetch`，例如：

```
https://i.zte.com.cn/#/shared/45e2844136c54b86a105b695eaef116b/wiki/page/76faeb0816ab11f1b6d6f79c8307e304/view
```

## 3. 命令

所有命令都在技能根目录执行（`cd <skill 目录>`）：

```bash
python3 scripts/scan_leaks.py check                  # 校验配置完整性 + cookie 是否有效
python3 scripts/scan_leaks.py check-cookie           # 只校验 cookie
python3 scripts/scan_leaks.py scan                   # 全量扫描（默认会同步本地仓库）
python3 scripts/scan_leaks.py scan --project <仓库名> # 只扫某个仓库，可重复
python3 scripts/scan_leaks.py scan --no-sync          # 不动本地仓库，只抓取并分析
python3 scripts/scan_leaks.py scan --json             # 额外打印完整 JSON
python3 scripts/scan_leaks.py scan --fail-on-leak     # 有问题时退出码为 1，便于流水线判断
python3 scripts/scan_leaks.py scan --out-dir /path/to/results  # 指定结果目录
```

退出码：`0` 成功；`1` 发现问题（仅 `--fail-on-leak`）；`2` 配置不完整；`3` cookie 无效；`4` 运行出错。

`scan` 会先做配置校验和 cookie 校验，任一不通过都会直接退出，不会抓取数据。

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

## 5. 修复规范

1. 先按 `projectName` 分组，再按文件聚合，一个文件里的多个问题一次改完，避免反复切文件。
2. 每个问题都要打开对应代码行的上下文再动手，确认缺陷描述与代码一致；描述与代码对不上时先向用户说明，不要猜着改。
3. 只做消除该缺陷所需的最小改动，不要顺手重构、改格式、动无关代码。
4. 不要加“为通过扫描而绕过”的写法（例如无意义的空判断、把变量强行挪走）；确实属于误报时，向用户说明并建议在 CCA 上做备案，而不是在代码里造痕迹。
5. 修复过程中不要提交/推送代码。用户要求提交时，按用户指示走 `git-push` 技能（或用户的常规提交流程）。
6. 无法安全修复的问题（需要改接口契约、需要产品决策）要单独列出来交给用户，并说明原因。
7. 修完自检：能跑 lint / 单测 / 构建就跑一遍，把结果如实汇报；跑不了要说明。

## 6. 验收

修复后重新执行 `python3 scripts/scan_leaks.py scan`（必要时加 `--project <仓库名>`），用新的 JSON 结果确认对应类别数量下降或归零。CCA 的扫描任务是异步的，本地代码改完不等于平台数据立即更新：若结果没有变化，先确认改动是否已提交到被扫描的分支，再判断是否需要等待平台重新扫描。

## 7. 归档（自进化）

每轮修复结束后，把本轮实际修复的问题按 `references/fix-archive.md` 的规则追加到该文件：

- 一个类别一条记录，类别用 `leaks[].rule`；
- 内容只有「问题类别 + 解决方案（通用手法）」以及次数/时间这类无敏感信息的记账字段；
- 禁止写入仓库名、文件路径/文件名、代码片段、行号、漏洞 ID、任务链接、分支名、提交者姓名工号、凭据。

归档前先检索该类别是否已存在：存在则合并补充，不存在才新增。

## 8. 排障

| 现象 | 处理 |
| --- | --- |
| `cookie 检查：不通过 —— cookie 已失效` | 让用户重新从浏览器复制 Cookie 与 `x-csrf-token`，更新 `config/config.yaml` 或环境变量后重试 |
| 接口返回 400 Bad Request | 说明请求参数不完整；本脚本已内置 datatables 全量参数，若平台改版需同步更新 `EXECUTION_COLUMNS` / `ISSUE_COLUMNS` |
| `local_path 不存在或不是目录` | 用提示的路径克隆仓库，或修正配置 |
| `warnings` 里出现“仓库名与配置不一致” | 任务 ID 配错了，用扫描结果里的仓库名核对 `projects[].kw_id` / `coverity_id` |
| `git` 同步失败（本地改动、分支不存在、无 upstream） | 该仓库会被跳过并记入 `warnings`，其余仓库照常；把原因原样告知用户，或用 `--no-sync` 先出结果 |
| 同步阶段长时间没有输出、像是卡住 | 现在有超时兜底，不会无限等待：等 `sync.timeout`（默认 180 秒）到点即可看到 `[sync]` 降级日志。若确实需要拉取，调大 `sync.timeout`；若不需要更新代码，用 `--no-sync` |
| 想确认 ssh 是否真的在传输数据 | 查看连接状态 `ss -tni \| grep -A1 29418`，对比 `lastrcv`/`lastsnd`；保活参数生效后静默连接会在约 60 秒内被 ssh 自行断开 |
| 结果为空但用户认为有问题 | 检查 `cca.kw.status_filter` / `cca.coverity.only_unclassified` 口径，以及任务最近一次执行时间 |
