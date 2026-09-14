---
name: git-push-for-bug
description: 在提交缺陷代码并更新缺陷工作项字段的时候使用，用于提交代码并自动填写缺陷单字段
---

## 用户输入

```text
$ARGUMENTS
```

您**必须**在继续之前考虑用户输入，如果用户输入为空，则提示用户“请输入 RDC 单号，否则不可以执行该命令”。

## 命令执行约束

- 执行此命令时，不要读取当前对话的上下文，不要读取当前打开的页签内容，专注执行当前任务
- 不要查看项目代码，直接按照大纲内的命令**执行步骤**进行任务执行
- 只允许提交**暂存区**的代码！**禁止**提交**更改区**的代码!
- 使用 todo 模式执行该命令的任务
- 任务执行结束后需要在总结输出使用文字输出**git message**和**缺陷字段更新内容**，不要输出文件！

## 大纲

触发消息中用户在 `/git-push` 后输入的文本 $ARGUMENTS 中，包含如下内容

- 大写英文字母 + 短横杠 + n 位数字的内容即是 RDC 单号（必填，例如：LCAP-123456，DT_PP-1234567）
- 代码提交 id（选填，需要根据这个 id 去查找历史提交的代码。当前工作区有多个 git 项目时，还依赖【项目目录路径】）
- git message（选填，用户补充的提交信息，后续代码提交的 git message 时需要结合这个 message 来生成）

根据用户输入，执行以下操作：

1.  调用 `scripts/query_rdc_item.py` 脚本获取 RDC 单据详细信息（标题、描述、故障等级等信息）
    执行命令实例：

```bash
`python3 scripts/query_rdc_item.py DT_PP-123456`
```

2.  生成 git message
    - 如果用户提供了代码提交 id，则根据代码提交 id，查找历史提交的代码，然后结合 RDC 故障描述信息，生成 git message
    - 如果用户提供了 git message，则结合 RDC 单据详细信息、代码 diff 和用户提供的 git message，生成 git message

3.  提交代码
    - 执行 `git commit -m "git message"`
    - 执行 `git push origin HEAD:refs/for/$(git branch --show-current)`

4.  `scripts/update_rdc_fields.py` 脚本更新 RDC 缺陷【根本原因】、【波及影响分析】、【解决方案】等必填字段
    执行命令实例：

```bash
  python3 update_rdc_fields.py <RDC单号> <根本原因html> <功能交叉影响分析html> <解决方案描述html>
```

比如：

```bash
  python3 update_rdc_fields.py DT_PP-1412772 \
  '<p><strong>根因特性（1、同平台项目共有；2、项目特有）：</strong></p><p><br /></p><p><strong>根本原因涉及的具体模块/文件/接口等：</strong></p><p><br /></p><p><strong>具体描述：</strong></p><p>新增的关于我们入口是路由级改造，进入新路由后旧路由没有缓存就触发了刷新</p>' \
  '<p><strong>是否对其他系统/模块有影响？</strong></p><p><br /></p><p><strong>波及模块及具体波及影响：</strong></p><p><br /></p><p><strong>其他波及(性能/指标等)：</strong></p><p><br /></p><p><strong>其他可能的风险：</strong></p><p><br /></p>' \
  '<p><strong>故障定位过程及措施说明（定位步骤、使用工具）：</strong></p><p><br /></p><p><strong>解决方案说明：</strong></p><p><br /></p><p><strong>验证方法及用例：</strong></p><p><br /></p>'
```

5.  在总结里使用文字输出**git message**和**缺陷字段更新内容**，不要输出文件！

## git message 生成要求

- `RDC:RDC单号` 必须出现在 git message 中
- git message 中，描述和正文内容必须是中文，可以出现英文关键字
- 如果 RDC 单据详细信息与代码 diff 内容不一致，则以代码 diff 内容为准来生成 git message

### git message 格式：

```
类型(作用域): 描述

- 正文

RDC:RDC单号
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

RDC:LCAP-901705
```

## 【缺陷字段更新内容】生成要求

- [FILL_PLACEHOLDER]部分替换成实际的内容
- 输出的内容使用代码块输出，方便复制
- 验证方法及用例是面向测试同事输出的

### 1. 根本原因

```html
<p><strong>根因特性（1、同平台项目共有；2、项目特有）：</strong></p>
<p>[FILL_PLACEHOLDER]<br /></p>
<p><strong>根本原因涉及的具体模块/文件/接口等：</strong></p>
<p>[FILL_PLACEHOLDER]<br /></p>
<p><strong>具体描述：</strong></p>
<p>[FILL_PLACEHOLDER]</p>
```

### 2. 波及影响分析

```html
<p><strong>是否对其他系统/模块有影响？</strong></p>
<p>[FILL_PLACEHOLDER]<br /></p>
<p><strong>波及模块及具体波及影响：</strong></p>
<p>[FILL_PLACEHOLDER]<br /></p>
<p><strong>其他波及(性能/指标等)：</strong></p>
<p>[FILL_PLACEHOLDER]<br /></p>
<p><strong>其他可能的风险：</strong></p>
<p>[FILL_PLACEHOLDER]<br /></p>
```

### 3. 解决方案

```html
<p><strong>故障定位过程及措施说明（定位步骤、使用工具）：</strong></p>
<p>[FILL_PLACEHOLDER]<br /></p>
<p><strong>解决方案说明：</strong></p>
<p>[FILL_PLACEHOLDER]<br /></p>
<p><strong>验证方法及用例：</strong></p>
<p>[FILL_PLACEHOLDER]<br /></p>
```
