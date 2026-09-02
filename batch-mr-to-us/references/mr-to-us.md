# MR → US 工作流：市场需求同步创建用户故事

## 概述

从指定的市场需求（MR）中提取字段值，创建用户故事（US）并关联为子级，实现字段自动同步。

## 前置条件

- 已安装并配置 `edw`（easy-develop-work）工具

## 字段映射关系

### 从 MR 同步的字段

| MR 字段                                | US 字段                                | 类型                | 说明                                                |
| -------------------------------------- | -------------------------------------- | ------------------- | --------------------------------------------------- |
| `System_Title`                         | `System_Title`                         | string              | 标题，直接继承                                      |
| `BelongReleaseVersion`                 | `BelongReleaseVersion`                 | advancedData        | 发布版本（MR 中可能为空）                           |
| `BelongProduct`                        | `BelongProduct`                        | advancedData        | 所属产品                                            |
| `ZTE_PDMApplication`                   | `ZTE_PDMApplication`                   | advancedData        | 所属PDM应用                                         |
| `System_IterationPath`                 | `System_IterationPath`                 | advancedData        | 迭代                                                |
| `Team`                                 | `Team`                                 | advancedData        | 团队                                                |
| `DevelopmentOwner`                     | `System_AppointedTo`                   | user[] → employeeNo | **多值数组**，每个开发负责人建一个 US，分别指派工号 |
| `DT_PP_RequirementClassification_XYDT` | `DT_PP_RequirementClassification_XYDT` | advancedData        | 需求分类                                            |

### 固定值字段（不来自 MR，直接使用固定值）

| US 字段            | 固定值 | 说明         |
| ------------------ | ------ | ------------ |
| `StoryPoints`      | `1`    | 故事点       |
| `PlanStartDate`    | `今天` | 计划开始日期 |
| `PlanFinishDate`   | `今天` | 计划完成日期 |
| `OriginalEstimate` | `1`    | 初始估计     |
| `RemainingWork`    | `1`    | 剩余工作     |
| `CompletedWork`    | `0`    | 已完成工作   |

## 取值规则

- **`string` 类型**：直接取 `value`
- **`advancedData` 类型**：取 `value.name` 或 `value.value`
- **`user` 类型（单值）**：`DevelopmentOwner.value` 为单个对象，取 `value.employeeNo` 工号
- **`user` 类型（多值数组）**：`DevelopmentOwner.value` 为数组，每个元素各取 `employeeNo`，**每个工号创建一个 US**
- **空值**：该字段跳过，不同步到 US

## 完整命令（两步）

### 第1步：查询市场需求信息

```bash
edw get_work_item \
  --work_item_id "<MR编号>" \
  --keywords "System_Title,BelongReleaseVersion,BelongProduct,ZTE_PDMApplication,System_IterationPath,Team,DevelopmentOwner,DT_PP_RequirementClassification_XYDT"
```

**输出示例（单开发负责人）**：

```json
{
	"fields": [
		{
			"key": "System_Title",
			"value": "【信通院过级】ZZ0590自定义页面支持数据源变量定义"
		},
		{ "key": "BelongReleaseVersion", "value": "" },
		{ "key": "BelongProduct", "value": { "name": "XYLCAP PRO300" } },
		{
			"key": "ZTE_PDMApplication",
			"value": { "name": "低代码应用研发平台/130000202115" }
		},
		{
			"key": "System_IterationPath",
			"value": { "name": "24M08S1(0729-0809)" }
		},
		{ "key": "Team", "value": "" },
		{
			"key": "DevelopmentOwner",
			"value": { "employeeNo": "0668001277", "name": "袁龙辉" }
		},
		{
			"key": "DT_PP_RequirementClassification_XYDT",
			"value": { "name": "需求分类名称" }
		}
	]
}
```

**输出示例（多开发负责人）**：

```json
{
	"fields": [
		{
			"key": "System_Title",
			"value": "【UAC2.0】云桌面策略管理态需迁移到UAC2.0的账号认证后台"
		},
		{
			"key": "BelongReleaseVersion",
			"value": { "name": "XYUAC-UAC300V1.26.31" }
		},
		{ "key": "BelongProduct", "value": { "name": "XYUAC UAC300" } },
		{
			"key": "ZTE_PDMApplication",
			"value": { "name": "统一访问控制/130000197144" }
		},
		{ "key": "System_IterationPath", "value": { "name": "UAC2631" } },
		{ "key": "Team", "value": { "name": "UAC账号&认证特性团队" } },
		{
			"key": "DevelopmentOwner",
			"value": [
				{ "employeeNo": "0668001277", "name": "袁龙辉" },
				{ "employeeNo": "0668001136", "name": "汤智茂" }
			]
		},
		{
			"key": "DT_PP_RequirementClassification_XYDT",
			"value": { "name": "需求分类名称" }
		}
	]
}
```

### 第2步：创建用户故事（关联为子级）

根据 `DevelopmentOwner` 的数量决定创建几个 US：

#### 场景A：单开发负责人 → 创建1个 US

```bash
edw create_work_item \
  --work_item_type "US" \
  --workspace "<工作空间>" \
  --related_id "<MR编号>" \
  --link_relation_name "父级" \
  --fields '[
    {"key":"System_Title","value":"<从MR获取的标题>"},
    {"key":"BelongProduct","value":"<从MR获取的所属产品名称>"},
    {"key":"ZTE_PDMApplication","value":"<从MR获取的PDM应用名称>"},
    {"key":"System_IterationPath","value":"<从MR获取的迭代名称>"},
    {"key":"Team","value":"<从MR获取的团队名称>"},
    {"key":"BelongReleaseVersion","value":"<从MR获取的发布版本名称>"},
    {"key":"DT_PP_RequirementClassification_XYDT","value":"<从MR获取的需求分类名称>"},
    {"key":"System_AppointedTo","value":"<开发负责人工号>"},
    {"key":"StoryPoints","value":"1"},
    {"key":"PlanStartDate","value":"<今天日期>"},
    {"key":"PlanFinishDate","value":"<今天日期>"},
    {"key":"OriginalEstimate","value":"1"},
    {"key":"RemainingWork","value":"1"},
    {"key":"CompletedWork","value":"0"}
  ]'
```

#### 场景B：多开发负责人 → 每人创建1个 US

有 N 个开发负责人，则创建 N 个 US，每个 US 标题增加人员后缀以示区分，`System_AppointedTo` 分别指派不同人。

使用 `awk` + `sed` 从 `edw get_work_item` 输出中提取开发负责人列表并逐个创建：

```bash
# 查询MR数据
mr_data=$(edw get_work_item --work_item_id "<MR编号>" --keywords "System_Title,DevelopmentOwner")

# 提取标题（string 类型："key":"System_Title","value":"<标题>"）
title=$(echo "$mr_data" | tr -d '\n' | sed -n 's/.*"key":"System_Title","value":"\([^"]*\)".*/\1/p')

# 提取 DevelopmentOwner 段，取 employeeNo + name 配对
# 单负责人或多负责人都能处理
echo "$mr_data" | awk '/"key":"DevelopmentOwner"/,/^\s*[]}]/' | \
  sed -n 's/.*"employeeNo":"\([^"]*\)","name":"\([^"]*\)".*/\1 \2/p' | \
  while read -r eno name; do

    edw create_work_item \
      --work_item_type "US" \
      --workspace "<工作空间>" \
      --related_id "<MR编号>" \
      --link_relation_name "父级" \
      --fields "$(cat <<JSONEOF
[
  {"key":"System_Title","value":"${title} - ${name}"},
  {"key":"BelongProduct","value":"<从MR获取的所属产品名称>"},
  {"key":"ZTE_PDMApplication","value":"<从MR获取的PDM应用名称>"},
  {"key":"System_IterationPath","value":"<从MR获取的迭代名称>"},
  {"key":"Team","value":"<从MR获取的团队名称>"},
  {"key":"BelongReleaseVersion","value":"<从MR获取的发布版本名称>"},
  {"key":"DT_PP_RequirementClassification_XYDT","value":"<从MR获取的需求分类名称>"},
  {"key":"System_AppointedTo","value":"${eno}"},
  {"key":"StoryPoints","value":"1"},
  {"key":"PlanStartDate","value":"<今天日期>"},
  {"key":"PlanFinishDate","value":"<今天日期>"},
  {"key":"OriginalEstimate","value":"1"},
  {"key":"RemainingWork","value":"1"},
  {"key":"CompletedWork","value":"0"}
]
JSONEOF
)"
    echo "已创建 ${name} 的 US"
  done
```

> **说明**：`awk '/"key":"DevelopmentOwner"/,/^\s*[]}]/'` 截取 DevelopmentOwner 字段块，`sed` 从中提取 `employeeNo` 和 `name`。
> 各字段提取也可参照同样方式，按字段路径定位：

> ```bash
> # advancedData 类型（如 BelongProduct），取 name 值
> product=$(echo "$mr_data" | tr -d '\n' | sed -n 's/.*"key":"BelongProduct"[^}]*"name":"\([^"]*\)".*/\1/p')
> # string 类型，取 value
> title=$(echo "$mr_data" | tr -d '\n' | sed -n 's/.*"key":"System_Title","value":"\([^"]*\)".*/\1/p')
> # user 类型（单负责人），取 employeeNo
> eno=$(echo "$mr_data" | tr -d '\n' | sed -n 's/.*"key":"DevelopmentOwner","value":{"employeeNo":"\([^"]*\)".*/\1/p')
> ```

## 幂等性检查（创建前必须执行）

每次创建 US 前，先查询 MR 下已有的子级工作项，避免重复创建。

### 查询命令

> **注意**：`edw query_related_work_items` 的 `--work_item_ids` 和 `--related_work_item_type_keys` 参数需传 JSON 数组。
> Linux / macOS / Windows Git Bash / WSL 均可用单引号包裹 JSON 数组。Windows cmd.exe 用户建议安装 Git Bash 或 WSL。

```bash
edw query_related_work_items \
  --work_item_ids '["<MR编号>"]' \
  --link_relation_name "son" \
  --related_work_item_type_keys '["US"]'
```

输出可通过 `grep` 过滤出 `visible: true` 的项及其关联 ID：

```bash
edw query_related_work_items \
  --work_item_ids '["<MR编号>"]' \
  --link_relation_name "son" \
  --related_work_item_type_keys '["US"]' | \
  grep -E '("relatedWorkItemId"|"visible": true)'
```

或提取所有 `visible: true` 的 US 编号：

```bash
edw query_related_work_items \
  --work_item_ids '["<MR编号>"]' \
  --link_relation_name "son" \
  --related_work_item_type_keys '["US"]' | \
  awk '/"relatedWorkItemId":/{id=$NF} /"visible": true/{print id}' | tr -d '",'
```

> **参数说明**：
> - `--work_item_ids`：传 JSON 数组字符串，如 `'["DT_PP-807946"]'`，不是 `DT_PP-807946`
> - `--link_relation_name`：固定为 `"son"`（子级关系），不是中文"子级"
> - `--related_work_item_type_keys`：传 `'["US"]'` 过滤只查 US

### 响应解析

返回值为对象数组，每个元素代表一个子级关联：

```json
[
  {
    "linkRelationName": "son",
    "relatedWorkItemId": "DT_PP-1397665",
    "relatedWorkItemTypeKey": "US",
    "visible": false,
    ...
  },
  {
    "linkRelationName": "son",
    "relatedWorkItemId": "DT_PP-1397664",
    "relatedWorkItemTypeKey": "US",
    "visible": true,
    ...
  }
]
```

**关键筛选规则**：
1. 只保留 `visible: true` 的项——`visible: false` 表示已删除/取消关联，不计入
2. 对每个 `visible: true` 的 US，调用 `get_work_item` 获取其标题和指派：
   ```bash
   edw get_work_item --work_item_id "<US编号>" --keywords "System_Title,System_AppointedTo"
   ```
3. 若某开发负责人的 US（标题+指派工号）已存在，则跳过该负责人的创建；完全不存在则创建

### 判断逻辑伪代码

```
for each MR:
    children = query_related_work_items(work_item_ids=[MR], link_relation_name="son", related_work_item_type_keys=["US"])
    visible_children = [c for c in children if c.visible == true]
    
    for each US to create (per owner):
        title_to_create = MR.title + (" - " + owner.name if multi-owner else "")
        appointed_to_create = owner.employeeNo
        
        existing = get_work_item(visible_child.relatedWorkItemId)
        match = existing.fields.System_Title == title_to_create 
             AND existing.fields.System_AppointedTo.employeeNo == appointed_to_create
        
        if match: skip  # 已存在，幂等
        else: create   # 不存在，创建
```

### 创建命令（Shell 方式）

> **注意**：`--fields` 参数接收 JSON 数组。Linux / macOS / Git Bash / WSL 下单引号包裹即可传递。
> 若 JSON 中包含变量，使用 heredoc 方式传递（参考上方场景B示例）。
> Windows cmd.exe 用户建议安装 Git Bash 或 WSL。

```bash
edw create_work_item \
  --work_item_type "US" \
  --workspace "<工作空间>" \
  --related_id "<MR编号>" \
  --link_relation_name "父级" \
  --fields '[
    {"key":"System_Title","value":"<标题>"},
    {"key":"BelongProduct","value":"<产品名称>"}
  ]'
```

**变量值使用 heredoc 嵌入：**

```bash
title="<标题>"
product="<产品名称>"
edw create_work_item \
  --work_item_type "US" \
  --workspace "<工作空间>" \
  --related_id "<MR编号>" \
  --link_relation_name "父级" \
  --fields "$(cat <<JSONEOF
[
  {"key":"System_Title","value":"${title}"},
  {"key":"BelongProduct","value":"${product}"}
]
JSONEOF
)"
```

## 注意事项

1. **`--fields` JSON 用单引号包裹传递**：Linux / macOS / Git Bash / WSL 下单引号 `'[...]'` 即可。含变量时用 heredoc 方式（参考场景B示例）。Windows cmd.exe 用户建议安装 Git Bash 或 WSL
2. **字段值类型**：`advancedData` → 取 `value.name`；`string` → 直接取 `value`；`user` → 取 `employeeNo` 工号
3. **`DevelopmentOwner` 多值判断**：检查 `value` 是数组还是单个对象，决定创建几个 US
4. **多 US 标题区分**：多开发负责人时标题追加 ` - 姓名` 后缀，避免重复
5. **关联关系**：每个 US 都通过 `link_relation_name: "父级"` 关联到同一个 MR 作为父级
6. **`System_Title`** 为必填字段，create时不可省略
7. **工作空间**：根据 MR 的实际工作空间确定（如 `LCAP`、`DT_PP` 等），从查询结果中的 `workspaceKey` 获取
8. 在创建工作项之前，务必列出需要创建的工作项信息，请求用户确认
9. **幂等性检查命令格式**：
   - `--work_item_ids` 传 JSON 数组：`'["DT_PP-807946"]'`，不是 `DT_PP-807946`
   - `--link_relation_name` 用 `"son"`，不是中文"子级"
   - `--related_work_item_type_keys` 传 `'["US"]'` 过滤只查 US
   - 返回结果必须筛 `visible: true` 的项，`visible: false` 的是已删除的无效关联
   - **Windows cmd.exe 不识别单引号**，建议改用 Git Bash 或 WSL 执行
