---
name: solve-four-0s-issue
description: 从 CCA 平台扫描并一键解决「4个0」问题，即 Klocwork Critical、Klocwork Error、Coverity High、Coverity Medium 四类高危缺陷清零。当用户提出处理 4个0、清理 CCA 上的 Klocwork/Coverity 高危告警、批量修复静态扫描缺陷时使用。
metadata:
  short-description: CCA 4个0问题扫描、修复与经验归档
---

# solve-four-0s-issue

「4个0」指四类问题的数量都必须为 0：Klocwork Critical、Klocwork Error、Coverity High、Coverity Medium。

本技能负责完整闭环：校验配置与 cookie → 从 CCA 抓取缺陷 → 展示结果并等用户确认 → 修复代码 → 归档修复经验。

## 环境

- 依赖 `python3`（`requests`、`pyyaml`）与命令行 `git`。
- 项目与平台配置在 `config/config.yaml`（含 cookie/csrf，已被 `.gitignore` 忽略）；字段说明与模板见 `config/config.example.yaml`。
- 下列命令都在技能根目录执行。

## 步骤（按顺序执行）

### 1. 校验配置与 cookie

```bash
python3 scripts/scan_leaks.py check
```

退出码为 0 才能进入下一步：

- 退出码 2（配置不完整）：把脚本列出的缺失项转述给用户，请其补全 `config/config.yaml`，或改用环境变量 `FOUR0S_CCA_COOKIE` / `FOUR0S_CCA_CSRF` 提供凭据，补齐后重新执行本步。
- 退出码 3（cookie 无效）：告知用户 cookie 已失效，请其重新从浏览器复制 `Cookie` 与 `x-csrf-token` 更新配置。不要试图绕过登录校验，也不要在 cookie 无效时继续抓取。

### 2. 扫描

```bash
python3 scripts/scan_leaks.py scan
```

- 脚本会先同步本地仓库（按需 stash → 检出配置分支 → pull），再抓取缺陷，并用 `git blame` 补全提交者。每条 git 命令都有超时（`sync.timeout`，默认 180 秒）并带 ssh 保活，远端静默不会让扫描挂死：拉取失败只在 `warnings` 里报告，该仓库降级为按本地现有代码分析。
- 用户不希望改动本地仓库时加 `--no-sync`；只想看某个仓库时加 `--project <仓库名>`；需要完整 JSON 时加 `--json`。
- 结果 JSON/CSV 默认写到命令运行时的当前目录，可用 `output.dir` 或 `--out-dir <目录>` 指定；JSON 含 `summary`、`leaks`、`warnings`，字段说明见 `references/workflow.md`。在会话中执行时建议把 `--out-dir` 指到本次任务的临时目录，避免产物留在仓库工作区。

### 3. 展示结果并停下来等确认（硬性要求）

把扫描结果整理成表格展示给用户：项目、问题类别（`rule`）、级别、文件:行、问题描述、提交者，以及 CCA 上的问题链接；同时给出四类问题的数量统计。

展示完毕后必须停下，明确询问用户是否现在修复（全部修复，还是只修指定项目/类别）。在用户答复之前，不要修改任何代码，也不要提交任何改动。

### 4. 修复（用户确认后）

- 动手前先读 `references/workflow.md` 的「修复规范」。
- 先按项目、再按文件聚合问题，每个问题都要先看代码上下文，只做消除缺陷所需的最小改动。
- 修完自检：能跑 lint／单测／构建就跑，并如实汇报结果。
- 不要自动提交或推送代码；用户要求提交时，按用户指示走 `git-push` 技能。
- 无法安全修复的问题（涉及接口契约、产品决策等）单独列出交给用户判断。

### 5. 归档（技能自进化）

修复完成后，把本轮修复的问题按 `references/fix-archive.md` 的规则追加进去：一个类别一条记录，只写「问题类别 + 通用解决方案」，并遵守该文件的脱敏要求（不写仓库名、文件路径、代码片段、行号、漏洞 ID、任务链接、分支名、提交者等）。

## 参考文件

- `references/workflow.md`：配置字段、命令与退出码、结果字段、修复规范、验收与排障。
- `references/fix-archive.md`：已积累的修复经验、归档格式与脱敏规则。
