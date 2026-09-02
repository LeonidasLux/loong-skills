---
name: openclaw-pr-porter
description: 当用户需要获取openclaw项目pr合入情况时使用。用户可能会说"查询pr情况，获取pr报告"。此技能自动读取文档、解析账号、查询PR、输出汇总表格。
---

# openclaw-pr-porter

通过 `edw` 工具读取 iCenter 空间文档（https://i.zte.com.cn/#/shared/634e29d20dc148e3b22afc3e90383c9d/wiki/page/f735352d79ef11f1a3b95b928b07a7a1/view）中的 GitHub 账号列表，自动查询这些账号在指定 GitHub 仓库（https://github.com/openclaw/openclaw）的 PR 提交情况，并以汇总表格输出，报告最终通过`edw`工具输出到（https://i.zte.com.cn/index/ispace/#/space/857231f2d76d4785b6da48ee8256f5e8/wiki/page/b870826679a211f189970bac3369a68a/view）的子页面下，标题为：openclaw-pr-porter-[当前时间]

---

## 前置条件

- **edw 命令行工具**：已安装且已登录
- **gh (GitHub CLI)**：已安装并已认证
- **jq**：已安装（用于 JSON 解析）
- **Python 3**：已安装（用于解析 edw 输出）

---

## 执行流程

### 步骤 1：检查 gh 认证

```bash
gh auth status
```

- **已认证**：继续执行
- **未认证**：
  1. 告知用户需要 GitHub 认证
  2. 运行 `gh auth login --web`（设置 timeout 300000ms），提示用户在浏览器中输入设备码完成登录
  3. 认证完成后继续

### 步骤 2：读取文档 → 查询 PR → 生成报告（一键执行）

调用脚本 `@pr-porter.sh` 完成**文档解析、PR 查询、报告生成**全流程：

```bash
SCRIPT="/home/0668001277/.claude/skills/openclaw-pr-porter/scripts/pr-porter.sh"
edw get_document_details --param_str "<iCenter文档完整URL>" --type markdown 2>&1 \
  | bash "$SCRIPT" --edw-json --repo openclaw/openclaw --output /tmp/openclaw-pr-porter-report.md
```

**参数说明：**
- `--edw-json`：从 stdin 读取 edw 输出的 key=value 格式，自动提取 contentBody
- `--repo owner/repo`：指定 GitHub 仓库（默认 `openclaw/openclaw`）
- `--output /path/to/file.md`：将报告写入文件

**脚本功能详解（`@scripts/pr-porter.sh`）：**
1. 从 edw 输出中提取文档 Markdown 内容
2. 拼接被 `\n` 打断的表格行
3. 解析 Markdown 表格，提取「部门、姓名、GitHub 账号」
4. 同一人多账号时合并账号并去重
5. 过滤无效行（`<br>` 等）
6. 对每个唯一 GitHub 账号执行 `gh pr list` 查询
7. 按人员归并多账号 PR 数据（OPEN / MERGED / CLOSED 分别求和）
8. 生成 Markdown 格式的详细表格和全局汇总
9. 按团队分组，内部按 PR 合计降序排列

### 步骤 3：发布报告

```bash
edw create_article \
  --title "openclaw-pr-porter-$(date +%Y-%m-%d)" \
  --file_path "/tmp/openclaw-pr-porter-report.md" \
  --parent_document "<目标空间父页面URL>" \
  --space_id "<空间ID>"
```

---

## 脚本文件

- `@scripts/pr-porter.sh`：核心自动化脚本（解析文档 → 查询 PR → 生成报告）

脚本位于技能目录下，无需临时创建，直接引用即可。

---

## 注意事项

1. **API 限流**：账号较多时串行查询较慢，但非认证用户有严格速率限制，建议使用已认证的 gh
2. **文档更新**：脚本每次运行会实时读取 iCenter 文档最新内容，自动适配表格变化
3. **特殊账号**：含空格的账号（如 `Silas Qiao`）非有效 GitHub 用户名，会自动跳过
4. **行拼接**：文档中的 `\n` 可能导致表格行被打断，脚本会自动拼接续行
5. **排序规则**：团队内部按 PR 合计数量降序排列
6. **失败处理**：单个账号查询失败不影响其他账号，按 0 处理
