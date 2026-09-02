#!/bin/bash
# ====================================================================
# openclaw-pr-porter/scripts/pr-porter.sh
# 自动化脚本：解析iCenter文档 → 查询GitHub PR → 生成Markdown报告
#
# 用法：
#   方式1（推荐）：从 edw 命令的 JSON 输出中自动提取 contentBody
#     edw get_document_details --param_str "<URL>" --type markdown 2>&1 \
#       | bash pr-porter.sh --edw-json [--repo owner/repo] [--output /path/to/output.md]
#
#   方式2：传入 Markdown 文件
#     bash pr-porter.sh --markdown-file /path/to/doc.md [--repo owner/repo] [--output file.md]
#
#   方式3：管道传入纯 Markdown 内容
#     cat doc.md | bash pr-porter.sh --markdown-stdin [--repo owner/repo] [--output file.md]
#
# 输出：默认 stdout，可通过 --output 指定文件
# ====================================================================

set -euo pipefail

# ---------- 默认值 ----------
REPO="openclaw/openclaw"
OUTPUT_FILE=""
INPUT_MODE=""
TMPDIR=$(mktemp -d /tmp/pr-porter-XXXXXX)
trap 'rm -rf "$TMPDIR"' EXIT

# ---------- 解析参数 ----------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --edw-json)      INPUT_MODE="edw-json"; shift ;;
    --markdown-file) INPUT_MODE="markdown-file"; MARKDOWN_FILE="$2"; shift 2 ;;
    --markdown-stdin) INPUT_MODE="markdown-stdin"; shift ;;
    --repo)          REPO="$2"; shift 2 ;;
    --output)        OUTPUT_FILE="$2"; shift 2 ;;
    *)               echo "未知参数: $1"; exit 1 ;;
  esac
done

# ====================================================================
# 步骤 A：读取并解析文档中的表格
# ====================================================================

RAW_MD=""

case "$INPUT_MODE" in
  edw-json)
    # edw get_document_details 输出为 key=value 格式（非JSON）
    # 如: id='...' spaceId='...' title='...' contentBody='...'
    # 尝试 JSON 解析优先，失败则退回到 key=value 提取
    RAW_MD=$(cat | python3 -c "
import sys, re, json

text = sys.stdin.read().strip()

# 尝试 JSON 解析
try:
    data = json.loads(text)
    if isinstance(data, dict):
        print(data.get('contentBody', ''))
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and 'contentBody' in item:
                print(item['contentBody'])
    sys.exit(0)
except (json.JSONDecodeError, ValueError):
    pass

# key=value 格式提取 contentBody='...'
# contentBody 是最后一个字段，一直延伸到末尾
m = re.search(r\"contentBody='(.+)'\s*(\w+=\S+)*\s*$\", text, re.DOTALL)
if m:
    content = m.group(1)
    # 转义处理
    content = content.replace('\\\\n', '\n').replace('\\n', '\n').replace(\"\\\\'\", \"'\")
    print(content)
else:
    # 尝试更宽松的提取：contentBody= 之后到第一个 空格+key= 之前
    m = re.search(r'contentBody=([\\s\\S]+)', text)
    if m:
        val = m.group(1).strip()
        if val.startswith(\"'\"):
            val = val[1:]
        if val.endswith(\"'\"):
            val = val[:-1]
        val = val.replace('\\\\n', '\n').replace('\\n', '\n')
        print(val)
")
    ;;
  markdown-file)
    RAW_MD=$(cat "$MARKDOWN_FILE" 2>/dev/null || echo "")
    ;;
  markdown-stdin)
    RAW_MD=$(cat)
    ;;
  *)
    echo "错误：请指定输入方式 --edw-json / --markdown-file / --markdown-stdin"
    exit 1
    ;;
esac

if [ -z "$RAW_MD" ]; then
  echo "错误：未能读取到文档内容"
  exit 1
fi

# 预处理：拼接被 \n 打断的表格行（如张贵萍的账号在下一行）
# 规则：不以 | 开头的行，作为前一行的续行
RAW_MD=$(echo "$RAW_MD" | awk '
{
  if ($0 ~ /^[[:space:]]*\|/ || NR == 1) {
    if (NR > 1) print prev
    prev = $0
  } else {
    prev = prev " " $0
  }
}
END { print prev }
')

# 解析 Markdown 表格，提取 | 序号 | 部门 | 姓名 | github登录账号 | ...
# 规则：跳过表头行（包含 #、部门、姓名、github等关键词的行）和分隔行（包含 ---）
PARSED_DATA="$TMPDIR/parsed_entries.txt"
echo "$RAW_MD" | while IFS= read -r line; do
  # 只处理以 | 开头的行（真正的表格行，避免匹配 URL 中的 |）
  [[ ! "$line" =~ ^[[:space:]]*\| ]] && continue
  # 跳过表头行 (包含 #、部门、姓名 等关键词)
  [[ "$line" =~ [#] ]] && [[ "$line" =~ [部] ]] && continue
  [[ "$line" =~ [姓] ]] && [[ "$line" =~ [名] ]] && continue
  [[ "$line" =~ github ]] && continue
  # 跳过分隔行 (包含 ---)
  [[ "$line" =~ --- ]] && continue
  # 跳过空行
  [[ -z "$(echo "$line" | tr -d '|[:space:]')" ]] && continue

  # 提取各列：strip 首尾空格和可能的首尾 |
  clean_line=$(echo "$line" | sed 's/^[[:space:]]*|//; s/|[[:space:]]*$//')
  IFS='|' read -ra cols <<< "$clean_line"

  # 期望至少 4 列: 序号 | 部门 | 姓名 | github账号
  if [ ${#cols[@]} -ge 4 ]; then
    dept=$(echo "${cols[1]}" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
    name=$(echo "${cols[2]}" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
    accounts=$(echo "${cols[3]}" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
    # 去除 Markdown 加粗标记
    name=$(echo "$name" | sed 's/\*\*//g')
    accounts=$(echo "$accounts" | sed 's/\*\*//g')

    # 过滤无效行（如 <br>）
    [[ "$dept" =~ \< ]] && continue
    [[ "$name" =~ \< ]] && continue

    if [ -n "$dept" ] && [ -n "$name" ] && [ -n "$accounts" ]; then
      echo "$dept|$name|$accounts"
    fi
  fi
done > "$PARSED_DATA"

# 合并同名同团队的人员（文档中同一人可能占多行，如谭珊珊）
MERGED_DATA="$TMPDIR/merged_entries.txt"
> "$MERGED_DATA"
while IFS='|' read -r dept name accounts; do
  key="$dept|$name"
  if grep -qF "$key" "$MERGED_DATA" 2>/dev/null; then
    existing=$(grep -F "$key" "$MERGED_DATA" | head -1)
    old_accts=$(echo "$existing" | cut -d'|' -f3-)
    combined="$old_accts,$accounts"
    IFS=',，' read -ra acct_list <<< "$combined"
    seen=(); deduped=""
    for a in "${acct_list[@]}"; do
      a=$(echo "$a" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
      [ -z "$a" ] && continue
      found=0
      for s in "${seen[@]}"; do [ "$s" = "$a" ] && found=1 && break; done
      [ "$found" -eq 0 ] && seen+=("$a") && deduped="$deduped${deduped:+,}$a"
    done
    matched_line=$(grep -nF "$key" "$MERGED_DATA" | cut -d: -f1 | head -1)
    sed -i "${matched_line}s/.*/$dept|$name|$deduped/" "$MERGED_DATA"
  else
    echo "$dept|$name|$accounts" >> "$MERGED_DATA"
  fi
done < "$PARSED_DATA"
mv "$MERGED_DATA" "$PARSED_DATA"

ENTRY_COUNT=$(wc -l < "$PARSED_DATA")
if [ "$ENTRY_COUNT" -eq 0 ]; then
  echo "错误：未能从文档中解析出任何人员信息，请检查文档格式"
  exit 1
fi
echo "解析到 $ENTRY_COUNT 条人员记录"

# ====================================================================
# 步骤 B：收集所有唯一 GitHub 账号
# ====================================================================

declare -A ACCOUNT_OWNERS  # account -> 姓名|部门 (用于去重时保留第一个)
ACCOUNTS_LIST="$TMPDIR/all_accounts.txt"

while IFS='|' read -r dept name accounts; do
  IFS=',，' read -ra accts <<< "$accounts"
  for acct in "${accts[@]}"; do
    acct=$(echo "$acct" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
    [ -z "$acct" ] && continue
    if [ -z "${ACCOUNT_OWNERS[$acct]:-}" ]; then
      ACCOUNT_OWNERS[$acct]="$name|$dept"
    fi
    echo "$acct"
  done
done < "$PARSED_DATA" | sort -u > "$ACCOUNTS_LIST"

UNIQUE_COUNT=$(wc -l < "$ACCOUNTS_LIST")
echo "共 $UNIQUE_COUNT 个唯一 GitHub 账号，开始查询 PR 数据..."

# ====================================================================
# 步骤 C：串行查询每个账号的 PR 数据（含24h统计）
# ====================================================================

PR_DATA_DIR="$TMPDIR/pr_data"
mkdir -p "$PR_DATA_DIR"

while IFS= read -r acct; do
  [ -z "$acct" ] && continue

  # 包含空格的账号不是有效 GitHub 用户名，跳过
  if echo "$acct" | grep -q ' '; then
    echo '{"OPEN":0,"MERGED":0,"CLOSED":0,"CREATED_24H":0,"MERGED_24H":0}' > "$PR_DATA_DIR/$(echo "$acct" | md5sum | cut -c1-8).json"
    continue
  fi

  # 查询 PR（含创建时间和合并时间，用于计算24h统计）
  result=$(gh pr list --repo "$REPO" --author "$acct" --state all --limit 1000 \
    --json number,title,state,createdAt,mergedAt 2>/dev/null) || true

  if [ -z "$result" ] || [ "$result" = "[]" ] || [ "$result" = "null" ]; then
    echo '{"OPEN":0,"MERGED":0,"CLOSED":0,"CREATED_24H":0,"MERGED_24H":0}' > "$PR_DATA_DIR/$(echo "$acct" | md5sum | cut -c1-8).json"
  else
    echo "$result" | python3 -c "
import json, sys
from datetime import datetime, timezone, timedelta

data = json.load(sys.stdin)
cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

def parse_dt(s):
    if not s:
        return None
    s = s.replace('Z', '+00:00')
    try:
        return datetime.fromisoformat(s)
    except:
        return None

open_c = merged_c = closed_c = created_c = merged_24h = 0
for pr in data:
    s = pr.get('state', '')
    if s == 'OPEN':
        open_c += 1
    elif s == 'MERGED':
        merged_c += 1
    elif s == 'CLOSED':
        closed_c += 1

    ca = parse_dt(pr.get('createdAt'))
    if ca and ca >= cutoff:
        created_c += 1

    ma = parse_dt(pr.get('mergedAt'))
    if ma and ma >= cutoff:
        merged_24h += 1

print(json.dumps({
    'OPEN': open_c,
    'MERGED': merged_c,
    'CLOSED': closed_c,
    'CREATED_24H': created_c,
    'MERGED_24H': merged_24h
}))
" > "$PR_DATA_DIR/$(echo "$acct" | md5sum | cut -c1-8).json"
  fi
done < "$ACCOUNTS_LIST"

# ====================================================================
# 步骤 D：归并数据，按团队分组，生成 Markdown 报告
# ====================================================================

REPORT="$TMPDIR/report.md"

# --- 辅助函数：查询账号缓存数据 ---
lookup_account() {
  local acct="$1"
  # 包含空格直接返回0
  if echo "$acct" | grep -q ' '; then
    echo "0 0 0 0 0"
    return
  fi
  local hash=$(echo "$acct" | md5sum | cut -c1-8)
  local f="$PR_DATA_DIR/$hash.json"
  if [ -f "$f" ]; then
    local o=$(jq -r '.OPEN // 0' "$f")
    local m=$(jq -r '.MERGED // 0' "$f")
    local c=$(jq -r '.CLOSED // 0' "$f")
    local c24=$(jq -r '.CREATED_24H // 0' "$f")
    local m24=$(jq -r '.MERGED_24H // 0' "$f")
    echo "$o $m $c $c24 $m24"
  else
    echo "0 0 0 0 0"
  fi
}

# 收集所有团队列表（按原始出现顺序）
TEAMS=()
declare -A TEAM_SEEN
while IFS='|' read -r dept name accounts; do
  if [ -z "${TEAM_SEEN[$dept]:-}" ]; then
    TEAMS+=("$dept")
    TEAM_SEEN[$dept]=1
  fi
done < "$PARSED_DATA"

# 按人员归并数据（多账号求和）
# 输出为临时文件：姓名|部门|账号显示|OPEN|MERGED|CLOSED|合计|24h创建|24h合入
PERSON_RESULTS="$TMPDIR/person_results.txt"

while IFS='|' read -r dept name accounts; do
  IFS=',，' read -ra accts <<< "$accounts"
  total_open=0; total_merged=0; total_closed=0; total_created_24h=0; total_merged_24h=0
  acct_labels=()

  for acct in "${accts[@]}"; do
    acct=$(echo "$acct" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
    [ -z "$acct" ] && continue
    read -r o m c c24 m24 <<< "$(lookup_account "$acct")"
    total_open=$((total_open + o))
    total_merged=$((total_merged + m))
    total_closed=$((total_closed + c))
    total_created_24h=$((total_created_24h + c24))
    total_merged_24h=$((total_merged_24h + m24))
    acct_labels+=("$acct")
  done

  total=$((total_open + total_merged + total_closed))
  # 手动拼接账号显示（IFS 只取首字符，无法做多字符分隔）
  acct_display="${acct_labels[0]}"
  for ((i=1; i<${#acct_labels[@]}; i++)); do
    acct_display+=" / ${acct_labels[$i]}"
  done

  echo "$name|$dept|$acct_display|$total_open|$total_merged|$total_closed|$total|$total_created_24h|$total_merged_24h"
done < "$PARSED_DATA" > "$PERSON_RESULTS"

# --- 计算团队汇总 ---
declare -A TEAM_OPEN TEAM_MERGED TEAM_CLOSED TEAM_TOTAL TEAM_C24 TEAM_M24

while IFS='|' read -r name dept acct_display open merged closed total c24 m24; do
  co=${TEAM_OPEN[$dept]:-0}
  cm=${TEAM_MERGED[$dept]:-0}
  cc=${TEAM_CLOSED[$dept]:-0}
  ct=${TEAM_TOTAL[$dept]:-0}
  cc24=${TEAM_C24[$dept]:-0}
  cm24=${TEAM_M24[$dept]:-0}
  TEAM_OPEN[$dept]=$((co + open))
  TEAM_MERGED[$dept]=$((cm + merged))
  TEAM_CLOSED[$dept]=$((cc + closed))
  TEAM_TOTAL[$dept]=$((ct + total))
  TEAM_C24[$dept]=$((cc24 + c24))
  TEAM_M24[$dept]=$((cm24 + m24))
done < "$PERSON_RESULTS"

# --- 生成报告内容 ---
{
  echo "# openclaw-pr-porter-$(date +%Y-%m-%d)"
  echo ""
  echo "> 数据来源：兴云数科PR提交名单统计"
  echo "> 仓库：$REPO"
  echo "> 查询时间：$(date +%Y-%m-%d)"
  echo ""

  # 详细表格
  echo "## 详细表格"
  echo ""
  echo "| 姓名 | 团队 | GitHub账号 | OPEN | MERGED | CLOSED | 合计 | 合入率 | 24h创建 | 24h合入 |"
  echo "|------|------|-----------|------|--------|--------|------|--------|--------|--------|"

  for team in "${TEAMS[@]}"; do
    # 按合计降序排列该团队人员
    grep -F "|$team|" "$PERSON_RESULTS" | sort -t'|' -k7 -rn | while IFS='|' read -r name dept acct_display open merged closed total c24 m24; do
      rate="0.0%"
      if [ "$total" -gt 0 ]; then
        rate=$(awk "BEGIN {printf \"%.1f%%\", $merged * 100 / $total}")
      fi
      echo "| $name | $dept | $acct_display | $open | $merged | $closed | $total | $rate | $c24 | $m24 |"
    done
  done

  echo ""

  # 全局汇总
  echo "## 全局汇总"
  echo ""
  echo "| 团队 | OPEN | MERGED | CLOSED | 合计 | 合入率 | 24h创建 | 24h合入 |"
  echo "|------|------|--------|--------|------|--------|--------|--------|"

  GRAND_OPEN=0; GRAND_MERGED=0; GRAND_CLOSED=0; GRAND_TOTAL=0; GRAND_C24=0; GRAND_M24=0

  for team in "${TEAMS[@]}"; do
    o=${TEAM_OPEN[$team]:-0}
    m=${TEAM_MERGED[$team]:-0}
    c=${TEAM_CLOSED[$team]:-0}
    t=${TEAM_TOTAL[$team]:-0}
    c24=${TEAM_C24[$team]:-0}
    m24=${TEAM_M24[$team]:-0}
    rate="0.0%"
    [ "$t" -gt 0 ] && rate=$(awk "BEGIN {printf \"%.1f%%\", $m * 100 / $t}")
    echo "| $team | $o | $m | $c | $t | $rate | $c24 | $m24 |"
    GRAND_OPEN=$((GRAND_OPEN + o))
    GRAND_MERGED=$((GRAND_MERGED + m))
    GRAND_CLOSED=$((GRAND_CLOSED + c))
    GRAND_TOTAL=$((GRAND_TOTAL + t))
    GRAND_C24=$((GRAND_C24 + c24))
    GRAND_M24=$((GRAND_M24 + m24))
  done

  GRAND_RATE="0.0%"
  [ "$GRAND_TOTAL" -gt 0 ] && GRAND_RATE=$(awk "BEGIN {printf \"%.1f%%\", $GRAND_MERGED * 100 / $GRAND_TOTAL}")
  echo "| **总计** | **$GRAND_OPEN** | **$GRAND_MERGED** | **$GRAND_CLOSED** | **$GRAND_TOTAL** | **$GRAND_RATE** | **$GRAND_C24** | **$GRAND_M24** |"

  echo ""
  echo "> 说明："
  echo "> - 数据来源为兴云数科PR提交名单统计文档中的 GitHub 账号列表"
  echo "> - 包含空格的账号非有效 GitHub 用户名，查询按空处理"
  echo "> - 同一人员有多个账号时已归并统计"
  echo "> - 团队内部按 PR 合计数量降序排列"
  echo "> - 24h创建/24h合入 为最近24小时内的创建和合并PR数量"
} > "$REPORT"

# ====================================================================
# 步骤 E：输出
# ====================================================================

if [ -n "$OUTPUT_FILE" ]; then
  cp "$REPORT" "$OUTPUT_FILE"
  echo "报告已输出到: $OUTPUT_FILE"
else
  cat "$REPORT"
fi
