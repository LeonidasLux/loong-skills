# skills

## 把技能软链接到各 agent 的 skills 目录

```bash
./link-skills.sh            # 交互式选择技能与目标 agent
./link-skills.sh --force    # 替换同名项：软链接重新指向；实体目录/文件先备份再链接
./link-skills.sh --plain    # 纯文本模式（终端不支持光标控制时使用）
```

- 技能判定：根目录下的一级子目录内包含 `SKILL.md` 即视为一个技能。
- 目标 agent 与其 skills 目录在 `link-skills.sh` 顶部的 `AGENT_PATHS` 中维护，一个 agent 可配置多个目录。
- 不加 `--force` 时，同名实体目录/文件只会提示跳过；加上后原内容会被移动到 `~/.link-skills-backup/<时间戳>/<agent>/`，确认无误后可自行删除（备份位置可用环境变量 `LINK_SKILLS_BACKUP_DIR` 修改）。
