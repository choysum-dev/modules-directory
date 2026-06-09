<!--
SPDX-FileCopyrightText: 2026-present Brian Wang <wangbuke@gmail.com>
SPDX-License-Identifier: LGPL-3.0-or-later
-->

# Choysum Modules Directory

[English](README.md) | 简体中文

📚 **Choysum 生态系统的官方模块发现中心与目录治理层。**

本仓库是所有 Choysum 模块体系的单一事实源（Single Source of Truth）。它负责管理指向 NPM 注册表的轻量级元数据指针，驱动全局的模块发现层。

这里不托管任何模块的具体源码。相反，自动化的持续集成流水线会解析这些指针，构建并向我们的 Serverless 边缘网络（`index.choysum.dev`）发布不可变的、极速响应的静态目录索引。

---

## 🛡️ 信任分级目录系统

为了保障生态的健康与安全，所有注册的模块都会被划分至三个清晰的受信任目录中：

*   📂 **`modules/official/`**: 由 Choysum 核心团队维护，提供开箱即用的企业级可靠性保障。
*   📂 **`modules/verified/`**: 高质量的合作方或第三方模块，维护者身份可核验，具备极高的稳定性保证。
*   📂 **`modules/community/`**: 承接来自全球开发者社区的自由开源提交，助力框架生态开放生长。

---

## 🏗️ 架构与产物 (Architecture)

本仓库不直接下发 npm 压缩包内容。当针对主分支的变更合并后，我们的 GitHub Actions 会自动编译所有的元数据，将其输出为静态的分层索引，并部署至全球化的边缘网络 (`index.choysum.dev`)。

下游消费端工具（例如 Choysum CLI 或周边 Web 控制台）直接读取这些极速、Serverless 化的索引数据以进行模块发现，在获知包的位置与信任等级后，实际的安装流程依旧直接与 NPM 官方直连。

---

## 🚀 提交你的模块

我们采用开放包容的治理模型。任何人都可以通过提交 Pull Request，将自己的 Choysum 模块注册进入全球目录。

准备好将你的模块分享给全世界了吗？

1.  阅读我们的 [贡献指南](CONTRIBUTING_zh-cn.md) 以了解完整的收录工作流。
2.  将你的模块包正式发布至 npm registry。
3.  在 `modules/community/` 目录下创建一个轻量的 JSON 指针文件，并向本仓库发起 Pull Request。

