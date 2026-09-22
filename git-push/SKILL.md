---
name: git-push
description: 当用户提及“提交代码”“提交”“push 到 github”等信息的时候，读取暂存区代码，生成 git message 并自动提交代码到 Gerrit 或 GitHub 仓库
---

# 生成 git message 并自动提交代码

## 用户输入

```text
$ARGUMENTS
```

## 执行约束

- 执行时，不要读取当前对话的无关上下文，不要读取当前打开的IDE页签内容，专注执行当前任务
- 不要查看其他无关项目代码，直接按照大纲内的命令**执行步骤**进行任务执行
- 只允许提交**暂存区**的代码！**禁止**提交**更改区**的代码!
- 默认读取当前会话目录下的暂存区代码
- 如果用户未提供RDC编号，则“RDC:[用户提供的RDC编号]”这部分信息不要出现在git-message中
- 推送前必须确认 remote 和推送目标，不要向未确认的仓库推送

## 大纲

触发消息中用户在 `/git-push` 后输入的文本 $ARGUMENTS 中，可能包含如下内容

- RDC编号（选填，将其填到git-message的RDC编号占位里，如果用户未提供，则“RDC:[用户提供的RDC编号]”这部分信息不要出现在git-message中）
- 项目目录路径（选填，根据这个路径去查找暂存区代码）
- git message（选填，用户补充的提交信息，后续代码提交的 git message 时需要结合这个 message 来生成）
- 推送目标（选填，GitHub 或 Gerrit。用户提到“push 到 github”“提交到 github”“推 github”等即视为 GitHub；提到 Gerrit、`refs/for` 即视为 Gerrit）
- 推送 remote / 分支名（选填，用户指定时以用户指定为准）

根据用户输入，执行以下操作：

1. **确定推送目标**
   - 如果用户提供了项目目录路径，则切换到对应目录
   - 用户明确指定了推送目标时，直接采用用户指定的目标
   - 用户未指定时，执行 `git remote -v` 查看 remote 地址：地址包含 `github.com` 的按 GitHub 仓库处理，其余按 Gerrit 仓库处理
   - 多个 remote、地址无法判断或判断结果与用户描述冲突时，向用户确认，不要猜测

2. **生成 git message**
   - 根据对应项目的暂存区代码 diff 生成 git message
   - 如果用户提供了 git message，则结合代码 diff 和用户提供的 message 生成 git message

3. **提交代码**
   - 执行 `git commit -m "git message"`

4. **推送代码**

   Gerrit 仓库：

   ```bash
   git push origin HEAD:refs/for/$(git branch --show-current)
   ```

   GitHub 仓库：

   ```bash
   git push -u origin HEAD:$(git branch --show-current)
   ```

   推送规则：
   - GitHub 仓库不能使用 Gerrit 的 `refs/for/<分支>` 推送，反之亦然，两个命令不可混用
   - 用户指定了 remote 或分支名时，替换命令中的 `origin` 和 `$(git branch --show-current)`
   - GitHub 仓库当前分支为 `main`、`master` 等主干分支时，先向用户确认可以直推主干，确认前不要推送；一般应推到独立分支后再发起 PR
   - 推送失败时输出原始错误信息，不要自动切换另一种推送方式重试

## git message 生成要求

- git message 中，描述和正文内容必须是中文，可以出现英文关键字
- 重点关注实现逻辑，而非代码细节；
- 整体字数不超过100字。

### git message 格式：

```
类型(作用域): 描述

- 正文

RDC:[用户提供的RDC编号]

```

**类型主要有：**

- feat: 新增功能
- fix: 修复 bug
- refactor: 重构代码
- chore: 其他杂项
- style: 样式调整
- docs: 文档更新
- test: 测试相关

### git message 示例：

```
feat(steps): 在步骤组件中用 IconCheck 替换 Select 图标

- 更新 item.vue 文件，使用 IconCheck 而不是 Select 来表示成功状态指示。
- 添加了新的 icon-check.svg 文件用于 IconCheck 组件。

RDC:DT_PP-1376595

```
