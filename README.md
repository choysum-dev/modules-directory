<!--
SPDX-FileCopyrightText: 2026-present Brian Wang <wangbuke@gmail.com>
SPDX-License-Identifier: LGPL-3.0-or-later
-->

# Choysum Modules Directory

English | [简体中文](README_zh-cn.md)

📚 **The official module discovery registry and catalog governance center for the Choysum ecosystem.**

This repository serves as the single source of truth for all Choysum modules. It manages lightweight metadata pointers to NPM registries, driving the global module discovery layer. 

No source code is hosted here. Instead, automated CI/CD pipelines resolve these pointers to build and publish immutable, blazing-fast static indices to our serverless edge network (`index.choysum.dev`).

---

## 🛡️ Trust Tier System

To maintain a healthy and secure ecosystem, registered modules are organized into three directories based on their trust tier:

*   📂 **`modules/official/`**: Maintained by the Choysum core team, offering out-of-the-box enterprise-grade reliability.
*   📂 **`modules/verified/`**: High-quality partner or third-party modules with verified maintainable identities and strong stability guarantees.
*   📂 **`modules/community/`**: Open submissions from the worldwide developer community, expanding the ecosystem freely.

---

## 🏗️ Architecture & Output

This repository does not serve npm packages directly. When changes to the main branch are merged, our GitHub Actions build a compiled, static list of metadata and deploy it to a global edge network (`index.choysum.dev`). 

Downstream tooling (such as the Choysum CLI or Web Dashboards) consumes this fast, serverless index to discover available modules, their configured trust tiers, and package locations, fetching the actual package tarballs directly from NPM.

---

## 🚀 Submitting Your Module

We embrace an open governance model driven by the community. Any developer can submit a pull request to index their Choysum modules globally.

Ready to share your module with the world?

1.  Read our [Contributing Guide](CONTRIBUTING.md) to understand the submission workflow.
2.  Publish your module to the npm registry.
3.  Submit a Pull Request adding a lightweight JSON pointer under the `modules/community/` directory.

