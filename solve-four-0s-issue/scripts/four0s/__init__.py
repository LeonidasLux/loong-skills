# -*- coding: utf-8 -*-
"""solve-four-0s-issue 的 Python 实现模块。

模块划分：
    constants  环境变量名、占位符、API 前缀、分页与超时等常量
    config     配置加载、校验、项目筛选
    cca        CCA 开放 API 客户端（只带 X-Emp-No + X-Uac-Token）
    issues     Klocwork / Coverity 缺陷解析与抓取
    hub        Hub（开源组件漏洞）版本统计与报告 Excel 解析
    gitops     本地仓库同步与 git blame
    report     结果汇总、落盘与终端输出

CLI 入口仍是 scripts/scan_leaks.py，用法与退出码保持不变。
"""
