<!--
SPDX-FileCopyrightText: 2026-present Brian Wang <wangbuke@gmail.com>
SPDX-License-Identifier: LGPL-3.0-or-later
-->

> [🇺🇸 View English Guide (查看英文贡献指南)](CONTRIBUTING.md)


# Choysum Modules Directory 贡献指南

欢迎向 Choysum Modules Directory 提交模块收录申请。本仓库是 Choysum 模块发现层的单一事实源，负责管理指向 NPM registry 的元数据指针。

## 收录免责声明 (Ownership Declaration)

向本注册表提交 Pull Request 即表示，你确认你是该 NPM 包的所有者，或者拥有明确的授权将其收录至 Choysum 生态。为了降低协作门槛，我们不强制要求复杂的数字签名或终端配置声明，但你需要在提交 PR 时，于模板中显式勾选相应的所有权确认声明。

## 模块更新与移除

* **更新信息**：如果你需要新增/移除 maintainers、申请将模块从 `community` 升级至 `verified`，只需修改对应的 JSON 文件并提交 PR 即可。
* **废弃与移除**：如果你希望将模块从全局检索库中移除，直接删除对应的 JSON 文件并提交 PR。
* **转移 / 换包名**：绝不允许为了更换底层包名而直接篡改原 JSON 文件内的 `package` 字段内容。如果底层 NPM 包易主或变更名字，请将其视为“一次新模块的收录 + 原旧模块的移除”，从而保障下游依赖图谱的历史溯源性。

## 收录流程

1. **先发 NPM**：请确保你的模块已包含标准 Choysum metadata (`choysum.moduleName` 等)，并已成功发布到 npm registry。
2. **建指针 JSON**：Fork 本仓库，并在 `modules/community/` 下以你的 `moduleName.json` 为名新建文件。
    * 文件里绝不允许填写 version 等信息；
    * 字段仅包含：`package`、`trust` 与 `maintainers`。
3. **提交 PR**：发起提交到主仓的 PR，填充相关 Checklist 描述。
4. **验证与合并**：基础门禁 CI 会对 JSON 格式与依赖断链等进行自动化验证。对于 `verified` 与 `official`，将引入 CODEOWNERS 人工审核要求。

## 本地格式与 Schema 校验

在提交 PR 前，可以在本地跑一遍自动化校验：

```bash
# 激活 python 环境 ( >= 3.12 ) 并安装依赖
python3 -m venv .venv
source .venv/bin/activate
pip install check-jsonschema

# 对 JSON 格式开展校验
check-jsonschema --schemafile schemas/catalog-entry.schema.json modules/*/*.json

# 执行完整目录树结构检查与聚合能力检查
python scripts/validate_catalog.py
```
