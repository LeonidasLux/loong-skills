#!/usr/bin/env bash
#
# link-skills.sh
#   把本仓库里的技能目录（根目录下的一级子目录，且内部包含 SKILL.md）
#   软链接到各个 agent 的 skills 目录。
#
# 用法：
#   ./link-skills.sh            交互式选择技能与目标 agent
#   ./link-skills.sh --force    替换同名项：软链接重新指向；实体目录/文件先备份再链接
#   ./link-skills.sh --plain    纯文本模式（终端不支持光标控制时用这个）
#   ./link-skills.sh -h         查看帮助
#
# 新增 agent：只在下面 AGENT_PATHS 里加一行即可（一个 agent 可以有多个目录）。

set -u -o pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# 目标 agent 列表（对象：键 = agent 名称，值 = 技能目录，多个目录用空格分隔）
# ---------------------------------------------------------------------------
declare -A AGENT_PATHS=(
  ["claude-code"]="$HOME/.claude/skills"
  ["codex"]="$HOME/.codex/skills"
  ["co-mind"]="$HOME/.icodemate/cli/skills $HOME/.icodemate/skills"
  ["openclaw"]="$HOME/.icodemate/cli/skills $HOME/.openclaw/workspace/skills"
)

# agent 在界面里的展示顺序（没列出来的 agent 会自动追加到末尾）
AGENT_ORDER=("claude-code" "codex" "co-mind")

FORCE=0

# UI_TUI=1 时用备用屏幕重画菜单；终端不支持光标控制时回退成纯文本模式
UI_TUI=1
[[ -n ${LINK_SKILLS_PLAIN:-} ]] && UI_TUI=0
[[ -n ${TERM:-} && ${TERM:-} != dumb ]] || UI_TUI=0
UI_ACTIVE=0

# --force 替换同名实体目录/文件时，原内容会移动到 <备份目录>/<时间戳>/<agent>/ 下
BACKUP_ROOT="${LINK_SKILLS_BACKUP_DIR:-$HOME/.link-skills-backup}"
RUN_BACKUP_DIR=""
BACKUP_LAST=""

# 纯文本模式下状态行使用的短标签（长度与选项数一致时生效）
UI_SHORT_LABELS=()

if [[ -t 1 ]]; then
  C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'
  C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_CYAN=$'\033[36m'
else
  C_RESET=""; C_BOLD=""; C_DIM=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_CYAN=""
fi

die() { printf '%s%s%s\n' "$C_RED" "$*" "$C_RESET" >&2; exit 1; }

usage() {
  sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

ui_restore() {
  (( UI_ACTIVE )) || return 0
  UI_ACTIVE=0
  printf '\033[?25h' 2>/dev/null || true
  (( UI_TUI )) && { printf '\033[?1049l' 2>/dev/null || true; }
  return 0
}

# 把已存在的 dst 移动到备份目录，备份路径写入 BACKUP_LAST
backup_existing() {
  local agent="$1" dst="$2" target base suffix=0
  if [[ -z $RUN_BACKUP_DIR ]]; then
    RUN_BACKUP_DIR="$BACKUP_ROOT/$(date +%Y%m%d-%H%M%S)"
  fi
  target="$RUN_BACKUP_DIR/$agent"
  mkdir -p "$target" 2>/dev/null || return 1
  base="$(basename "$dst")"
  while [[ -e "$target/$base" || -L "$target/$base" ]]; do
    suffix=$(( suffix + 1 ))
    base="$(basename "$dst").$suffix"
  done
  mv -- "$dst" "$target/$base" || return 1
  BACKUP_LAST="$target/$base"
  return 0
}

trap 'ui_restore' EXIT
trap 'ui_restore; exit 130' INT TERM

# ---------------------------------------------------------------------------
# 交互式多选（结果写入全局数组 UI_SELECTED，存放选中项的下标）
#   ↑/↓ 移动、空格 选中/取消、a 全选/全不选、Enter 确认、q 取消
# ---------------------------------------------------------------------------
ui_multiselect() {
  local title="$1" subtitle="$2"; shift 2
  local -a items=("$@")
  local n=${#items[@]}
  (( n > 0 )) || return 1

  local -a sel=()
  local i
  for (( i = 0; i < n; i++ )); do sel[i]=0; done
  local cur=0 cnt=0
  local ptr mark flag
  local hint="↑/↓ 移动 · 空格 选中/取消 · a 全选/全不选 · Enter 确认 · q 取消"

  UI_ACTIVE=1
  if (( UI_TUI )); then
    # 进入备用屏幕：退出时原终端内容会自动恢复，界面重画也不会污染历史
    printf '\033[?1049h\033[?25l\033[H\033[J'
  else
    printf '%s\n' "$title"
    [[ -n $subtitle ]] && printf '%s\n' "$subtitle"
    printf '%s\n\n' "$hint"
    for (( i = 0; i < n; i++ )); do printf '  %d. %s\n' "$(( i + 1 ))" "${items[i]}"; done
    printf '\n'
  fi

  local key=""
  while true; do
    cnt=0
    for (( i = 0; i < n; i++ )); do (( sel[i] )) && cnt=$(( cnt + 1 )); done

    if (( UI_TUI )); then
      # 光标归位 + 清除到屏幕末尾，不依赖行数计算
      printf '\033[H\033[J'
      printf '%s%s%s  %s已选 %d/%d%s\n' \
        "$C_BOLD" "$title" "$C_RESET" "$C_DIM" "$cnt" "$n" "$C_RESET"
      [[ -n $subtitle ]] && printf '%s%s%s\n' "$C_DIM" "$subtitle" "$C_RESET"
      printf '%s%s%s\n\n' "$C_DIM" "$hint" "$C_RESET"
      for (( i = 0; i < n; i++ )); do
        ptr="  "; mark="[ ]"
        (( i == cur )) && ptr="${C_CYAN}> ${C_RESET}"
        (( sel[i] )) && mark="${C_GREEN}[x]${C_RESET}"
        printf '%s%s %s\n' "$ptr" "$mark" "${items[i]}"
      done
    else
      flag="未选中"; (( sel[cur] )) && flag="已选中"
      local shown="${items[cur]}"
      (( ${#UI_SHORT_LABELS[@]} == n )) && shown="${UI_SHORT_LABELS[cur]}"
      printf '\r\033[K  → 第 %d/%d 项 %s（%s）  已选 %d/%d ' \
        "$(( cur + 1 ))" "$n" "$shown" "$flag" "$cnt" "$n"
    fi

    key=""
    if ! IFS= read -rsn1 key; then
      ui_restore
      (( UI_TUI )) || printf '\n'
      return 1
    fi
    if [[ $key == $'\033' ]]; then # 方向键等转义序列
      local rest=""
      IFS= read -rsn2 -t 0.05 rest || true
      key="$key$rest"
    fi

    case "$key" in
      $'\033[A'|k) (( cur > 0 )) && cur=$(( cur - 1 )) ;;
      $'\033[B'|j) (( cur < n - 1 )) && cur=$(( cur + 1 )) ;;
      ' ')         if (( sel[cur] )); then sel[cur]=0; else sel[cur]=1; fi ;;
      a|A)         if (( cnt == n )); then
                     for (( i = 0; i < n; i++ )); do sel[i]=0; done
                   else
                     for (( i = 0; i < n; i++ )); do sel[i]=1; done
                   fi ;;
      ''|$'\n'|$'\r') break ;;
      q|Q|$'\033') ui_restore; (( UI_TUI )) || printf '\n'; return 1 ;;
    esac
  done

  ui_restore
  (( UI_TUI )) || printf '\n'
  UI_SELECTED=()
  for (( i = 0; i < n; i++ )); do
    (( sel[i] )) && UI_SELECTED+=("$i")
  done
  return 0
}

# ---------------------------------------------------------------------------
# 收集本仓库中的技能目录及其描述
# ---------------------------------------------------------------------------

# 技能目录里的 SKILL.md（优先一级目录下的），没有则无输出
skill_md_path() {
  local dir="$1" found
  if [[ -f "$dir/SKILL.md" ]]; then
    printf '%s\n' "$dir/SKILL.md"
    return 0
  fi
  found="$(find "$dir" -mindepth 2 -name SKILL.md -not -path '*/.git/*' -print -quit 2>/dev/null)"
  [[ -n $found ]] && printf '%s\n' "$found"
  return 0
}

# 读取 SKILL.md 元数据里的 description，输出单行文本（支持多行、引号写法）
skill_description() {
  local file="$1"
  [[ -f $file ]] || return 0
  awk '
    NR == 1 {
      if ($0 !~ /^---[ \t]*$/) exit
      in_fm = 1
      next
    }
    in_fm {
      if ($0 ~ /^---[ \t]*$/) exit
      if (!found) {
        if ($0 ~ /^description:[ \t]*/) {
          line = $0
          sub(/^description:[ \t]*/, "", line)
          desc = line
          found = 1
        }
        next
      }
      # 续行：缩进行或不像 "key:" 的行
      if ($0 ~ /^[ \t]/ || $0 !~ /^[A-Za-z_][A-Za-z0-9_-]*:/) {
        desc = desc " " $0
        next
      }
      exit
    }
    END {
      if (!found) exit
      sub(/^[|>][-+0-9]*[ \t]*/, "", desc)
      gsub(/[ \t]+/, " ", desc)
      sub(/^[ \t]+/, "", desc)
      sub(/[ \t]+$/, "", desc)
      if (desc ~ /^["\047].*["\047]$/) desc = substr(desc, 2, length(desc) - 2)
      print desc
    }
  ' "$file"
}

# 按终端显示宽度截断文本（中文等全角字符按 2 列计）
clip_text() {
  local text="$1" max="$2" out="" i=0 n ch w width=0
  text="${text//$'\n'/ }"
  n=${#text}
  while (( i < n )); do
    ch="${text:i:1}"
    if [[ $ch == [$'\x20'-$'\x7e'] ]]; then w=1; else w=2; fi
    if (( width + w > max )); then
      printf '%s…' "$out"
      return 0
    fi
    out+="$ch"
    width=$(( width + w ))
    i=$(( i + 1 ))
  done
  printf '%s' "$out"
}

collect_skills() {
  local d name md
  SKILLS=()
  SKILL_DESCS=()
  for d in "$ROOT_DIR"/*/; do
    [[ -d $d ]] || continue
    d="${d%/}"
    name="${d##*/}"
    [[ $name == .* ]] && continue
    md="$(skill_md_path "$d")"
    [[ -n $md ]] || continue
    SKILLS+=("$name")
    SKILL_DESCS+=("$(skill_description "$md")")
  done
}

# ---------------------------------------------------------------------------

main() {
  while (( $# > 0 )); do
    case "$1" in
      -f|--force) FORCE=1 ;;
      -p|--plain) UI_TUI=0 ;;
      -h|--help)  usage; exit 0 ;;
      *)          die "未知参数：$1（-h 查看帮助）" ;;
    esac
    shift
  done

  [[ -t 0 && -t 1 ]] || die "需要在交互式终端里运行本脚本。"

  collect_skills
  (( ${#SKILLS[@]} > 0 )) || die "在 $ROOT_DIR 下没有找到包含 SKILL.md 的技能目录。"

  # 技能列表：名称 + SKILL.md 里的 description
  local i name_w=0 cols desc_max
  local name desc
  for name in "${SKILLS[@]}"; do (( ${#name} > name_w )) && name_w=${#name}; done
  cols="$(tput cols 2>/dev/null || true)"
  [[ $cols =~ ^[0-9]+$ ]] || cols="${COLUMNS:-110}"
  (( cols > 20 )) || cols=110
  desc_max=$(( cols - name_w - 8 ))
  (( desc_max < 12 )) && desc_max=12
  local -a skill_labels=()
  for (( i = 0; i < ${#SKILLS[@]}; i++ )); do
    name="${SKILLS[i]}"; desc="${SKILL_DESCS[i]}"
    if [[ -n $desc ]]; then
      skill_labels+=("$(printf '%s%*s  %s%s%s' "$name" "$(( name_w - ${#name} ))" '' \
        "$C_DIM" "$(clip_text "$desc" "$desc_max")" "$C_RESET")")
    else
      skill_labels+=("$name")
    fi
  done

  local -a chosen_skills=()
  UI_SHORT_LABELS=("${SKILLS[@]}")
  ui_multiselect "请选择要创建软链接的技能" "$ROOT_DIR" "${skill_labels[@]}" \
    || die "已取消。"
  UI_SHORT_LABELS=()
  for i in ${UI_SELECTED[@]+"${UI_SELECTED[@]}"}; do chosen_skills+=("${SKILLS[i]}"); done
  (( ${#chosen_skills[@]} > 0 )) || { printf '%s未选择任何技能，退出。%s\n' "$C_YELLOW" "$C_RESET"; exit 0; }

  # 组装 agent 多选项：名称 + 目标目录
  local -a agent_names=() agent_labels=()
  local name path label
  for name in "${AGENT_ORDER[@]}"; do
    [[ -n ${AGENT_PATHS[$name]+x} ]] || continue
    [[ " ${agent_names[*]-} " == *" $name "* ]] && continue
    label=""
    for path in ${AGENT_PATHS[$name]}; do label+="${label:+, }${path/#$HOME/\~}"; done
    agent_names+=("$name")
    agent_labels+=("$(printf '%-11s -> %s' "$name" "$label")")
  done
  for name in "${!AGENT_PATHS[@]}"; do # AGENT_ORDER 里没写的 agent 追加到末尾
    [[ " ${agent_names[*]-} " == *" $name "* ]] && continue
    label=""
    for path in ${AGENT_PATHS[$name]}; do label+="${label:+, }${path/#$HOME/\~}"; done
    agent_names+=("$name")
    agent_labels+=("$(printf '%-11s -> %s' "$name" "$label")")
  done

  local -a chosen_agents=()
  ui_multiselect "请选择目标 agent" "" "${agent_labels[@]}" || die "已取消。"
  for i in ${UI_SELECTED[@]+"${UI_SELECTED[@]}"}; do chosen_agents+=("${agent_names[i]}"); done
  (( ${#chosen_agents[@]} > 0 )) || { printf '%s未选择任何 agent，退出。%s\n' "$C_YELLOW" "$C_RESET"; exit 0; }

  # -------------------------------------------------------------------------
  # 创建软链接
  # -------------------------------------------------------------------------
  printf '\n'
  local -a results=()
  local created=0 exists=0 skipped=0 failed=0 target_dirs=0
  local skill src dst cur_target
  RUN_BACKUP_DIR=""
  for name in "${chosen_agents[@]}"; do
    for path in ${AGENT_PATHS[$name]}; do
      target_dirs=$(( target_dirs + 1 ))
      if ! mkdir -p "$path" 2>/dev/null; then
        for skill in "${chosen_skills[@]}"; do
          results+=("$name|$path|$skill|failed|目录不可创建")
          failed=$(( failed + 1 ))
        done
        continue
      fi
      for skill in "${chosen_skills[@]}"; do
        src="$ROOT_DIR/$skill"
        dst="$path/$skill"
        if [[ -L $dst ]]; then
          cur_target="$(readlink "$dst")"
          if [[ $cur_target == "$src" ]]; then
            results+=("$name|$path|$skill|exists|")
            exists=$(( exists + 1 ))
          elif (( FORCE )); then
            if ln -sfn "$src" "$dst"; then
              results+=("$name|$path|$skill|created|覆盖了旧链接")
              created=$(( created + 1 ))
            else
              results+=("$name|$path|$skill|failed|覆盖失败")
              failed=$(( failed + 1 ))
            fi
          else
            results+=("$name|$path|$skill|skip|已指向 $cur_target")
            skipped=$(( skipped + 1 ))
          fi
        elif [[ -e $dst ]]; then
          if (( FORCE )); then
            if ! backup_existing "$name" "$dst"; then
              results+=("$name|$path|$skill|failed|备份失败，原内容未改动")
              failed=$(( failed + 1 ))
            elif ln -s "$src" "$dst" 2>/dev/null; then
              results+=("$name|$path|$skill|created|原内容备份到 ${BACKUP_LAST/#$HOME/\~}")
              created=$(( created + 1 ))
            else
              results+=("$name|$path|$skill|failed|已备份到 ${BACKUP_LAST/#$HOME/\~}，但创建链接失败")
              failed=$(( failed + 1 ))
            fi
          else
            results+=("$name|$path|$skill|skip|同名文件/目录已存在（加 --force 可备份后替换）")
            skipped=$(( skipped + 1 ))
          fi
        elif ln -s "$src" "$dst" 2>/dev/null; then
          results+=("$name|$path|$skill|created|")
          created=$(( created + 1 ))
        else
          results+=("$name|$path|$skill|failed|创建失败")
          failed=$(( failed + 1 ))
        fi
      done
    done
  done

  # -------------------------------------------------------------------------
  # 输出结果
  # -------------------------------------------------------------------------
  printf '%s已完成%s\n' "$C_BOLD" "$C_RESET"
  local last="" entry status detail
  for entry in ${results[@]+"${results[@]}"}; do
    IFS='|' read -r name path skill status detail <<<"$entry"
    if [[ "$name|$path" != "$last" ]]; then
      printf '  %s%s%s -> %s\n' "$C_BOLD" "$name" "$C_RESET" "${path/#$HOME/\~}"
      last="$name|$path"
    fi
    case "$status" in
      created) printf '    %s+ %s%s%s\n' "$C_GREEN" "$skill" "$C_RESET" "${detail:+（$detail）}" ;;
      exists)  printf '    %s= %s%s（已存在）\n' "$C_DIM" "$skill" "$C_RESET" ;;
      skip)    printf '    %s! %s%s（%s）\n' "$C_YELLOW" "$skill" "$C_RESET" "$detail" ;;
      failed)  printf '    %sx %s%s（%s）\n' "$C_RED" "$skill" "$C_RESET" "$detail" ;;
    esac
  done
  printf '\n技能 %d 个 × 目标目录 %d 个：%s新增 %d%s，已存在 %d，跳过 %d，失败 %d\n' \
    "${#chosen_skills[@]}" "$target_dirs" "$C_GREEN" "$created" "$C_RESET" "$exists" "$skipped" "$failed"
  if [[ -n $RUN_BACKUP_DIR && -d $RUN_BACKUP_DIR ]]; then
    printf '被替换的原内容已备份到 %s（确认无误后可自行删除）\n' "${RUN_BACKUP_DIR/#$HOME/\~}"
  fi
}

main "$@"
