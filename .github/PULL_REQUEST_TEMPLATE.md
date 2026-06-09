<!--
SPDX-FileCopyrightText: 2026-present Brian Wang <wangbuke@gmail.com>
SPDX-License-Identifier: LGPL-3.0-or-later
-->

## Module Submission / Update

**Description:**
<!-- Briefly describe the module you are adding or the content of the update. -->

### Checklist

Please confirm the following before submitting your PR (refer to [CONTRIBUTING.md](../CONTRIBUTING.md) / [中文指南](../CONTRIBUTING_zh-cn.md) for guidance):

- [ ] **Ownership Declaration**: I declare that I am the owner of this NPM package, or I have explicit authorization to list it in the Choysum ecosystem.
- [ ] **Published**: My package has been successfully published to the [npm registry](https://npmjs.com) and is currently discoverable.
- [ ] **Correct Format**: The `*.json` file structure strictly follows `schemas/catalog-entry.schema.json` (contains only `package`, `trust`, and `maintainers`; no redundant data like version numbers).
- [ ] **Tier Alignment**: The JSON file is placed under the matching directory tier (`official` / `verified` / `community`).
- [ ] **Identifier Consistency**: The JSON file name (i.e. moduleName) matches the `choysum.moduleName` specified in the npm metadata.

<!-- If this is a `verified` module application, please provide plain text links or explanations here to prove your maintainer identity/organizational relationship. -->
