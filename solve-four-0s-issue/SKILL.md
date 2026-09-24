---
name: solve-four-0s-issue
description: 从 CCA 平台扫描并一键解决「4个0」问题，即 Klocwork Critical、Klocwork Error、Coverity High、Coverity Medium 四类高危缺陷清零。输出扫描结果时对每条问题给出「建议修复」或「建议备案」的结论，备案项附带可直接使用的备案说明。当用户提出处理 4个0、清理 CCA 上的 Klocwork/Coverity 高危告警、批量修复静态扫描缺陷时使用。
metadata:
  short-description: CCA 4个0问题扫描、修复/备案建议与经验归档
---

# solve-four-0s-issue

「4个0」指四类问题的数量都必须为 0：Klocwork Critical、Klocwork Error、Coverity High、Coverity Medium。

本技能负责完整闭环：校验配置与凭据 → 从 CCA 抓取缺陷 → 展示结果、给出修复/备案建议并等用户确认 → 修复代码（备案项登记到平台）→ 归档经验。

## 环境

- 依赖 `python3`（`requests`、`pyyaml`）与命令行 `git`。
- CCA 接口统一走开放 API（`<base_url>/api/v2`），请求头只需要 `X-Emp-No` + `X-Uac-Token` 两个鉴权参数，不需要浏览器 Cookie。
- 凭据优先取 OpenClaw 注入的环境变量 `coclaw_empno` / `coclaw_token`；也可以用本技能专用环境变量 `FOUR0S_CCA_EMP_NO` / `FOUR0S_CCA_UAC_TOKEN`，或写进 `config/config.yaml` 的 `cca.emp_no` / `cca.uac_token`。
- 项目与平台配置在 `config/config.yaml`（已被 `.gitignore` 忽略）；字段说明与模板见 `config/config.example.yaml`。
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
- 结果 JSON/CSV 默认写到命令运行时的当前目录，可用 `output.dir` 或 `--out-dir <目录>` 指定；JSON 含 `summary`、`leaks`、`warnings`，字段说明见 `references/workflow.md`。在会话中执行时建议把 `--out-dir` 指到本次任务的临时目录，避免产物留在仓库工作区。

### 3. 展示结果、给出处置建议，并停下来等确认（硬性要求）

把扫描结果整理成表格展示给用户：项目、问题类别（`rule`）、级别、文件:行、问题描述、提交者、CCA 上的问题链接，外加一列「处置建议」；同时给出四类问题的数量统计。

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

备案动作由用户在 CCA 平台上完成，技能只产出说明文本、不代操作。说明给用户时顺带点明：备案后该问题不再进入「4个0」统计口径（Klocwork 状态不再是 `Analyze`，Coverity 的 `classification` 不再是 `Unclassified`），所以误报走备案是达成 4个0 的正当路径。

展示完毕后必须停下，明确询问用户是否现在处理，以及「修哪些、备案哪些」（全部按建议执行，还是只处理指定项目/类别）。在用户答复之前，不要修改任何代码，也不要提交任何改动。

### 4. 修复（用户确认后）

- 动手前先读 `references/workflow.md` 的「修复规范」。
- 用户选择「备案」的问题保持代码原样，把第 3 步的备案说明交给用户在平台上登记；不要为了消掉告警去改代码。
- 先按项目、再按文件聚合问题，每个问题都要先看代码上下文，只做消除缺陷所需的最小改动。
- 修完自检：能跑 lint／单测／构建就跑，并如实汇报结果。
- 不要自动提交或推送代码；用户要求提交时，按用户指示走 `git-push` 技能。
- 无法安全修复的问题（涉及接口契约、产品决策等）单独列出交给用户判断。

### 5. 归档（技能自进化）

修复完成后，把本轮修复的问题按 `references/fix-archive.md` 的规则追加进去：一个类别一条记录，只写「问题类别 + 通用解决方案」；本轮确认备案的问题也一并归档（只写问题类别 + 通用备案依据）。两者都要遵守该文件的脱敏要求（不写仓库名、文件路径、代码片段、行号、漏洞 ID、任务链接、分支名、提交者等）。

## 参考文件

- `references/workflow.md`：配置字段、命令与退出码、结果字段、修复规范、验收与排障。
- `references/fix-archive.md`：已积累的修复经验、归档格式与脱敏规则。
