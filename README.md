# skills

## 把技能软链接到各 agent 的 skills 目录

```bash
./link-skills.sh            # 交互式选择技能与目标 agent
./link-skills.sh --force    # 同名软链接已指向别处时，重新指向本仓库
```

- 技能判定：根目录下的一级子目录内包含 `SKILL.md` 即视为一个技能。
- 目标 agent 与其 skills 目录在 `link-skills.sh` 顶部的 `AGENT_PATHS` 中维护，一个 agent 可配置多个目录。
