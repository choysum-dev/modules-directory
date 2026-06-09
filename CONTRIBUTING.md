<!--
SPDX-FileCopyrightText: 2026-present Brian Wang <wangbuke@gmail.com>
SPDX-License-Identifier: LGPL-3.0-or-later
-->

# Contributing to Choysum Modules Directory

> [🇨🇳 查看中文贡献指南 (View Chinese Guide)](CONTRIBUTING_zh-cn.md)

Welcome to the Choysum Modules Directory! This repository acts as the single source of truth for the Choysum module discovery layer, managing metadata pointers that resolve to the NPM registry.

## Ownership Declaration

By submitting a Pull Request to this registry, you confirm that you are the owner of the specified NPM package or have explicit authorization to list it in the Choysum ecosystem. You will be asked to confirm this via a checkbox in the Pull Request template. To lower the barrier for contribution, we do not require complex terminal configuration or digital signatures.

## Updating or Removing Modules

* **Updating Meta**: If you need to add/remove maintainers, or apply to upgrade a module from `community` to `verified`, simply modify the corresponding JSON file and submit a PR.
* **Deprecation / Removal**: If you wish to remove your module from the global directory, delete the corresponding JSON file and submit a PR.
* **Package Transfers / Renaming**: You are strictly forbidden from modifying the `package` field directly to point to a different NPM package. If an NPM package changes hands or name, treat this as a "New Module Submission + Old Module Deletion". This ensures the historical traceability of downstream dependency graphs.

## Submission Process

1. **Publish to NPM First**: Ensure your module package contains the standard Choysum metadata (e.g., `choysum.moduleName`) and has been successfully published to the npm registry.
2. **Create Pointer JSON**: Fork this repository and create a new file named `<moduleName>.json` under `modules/community/` (or your targeted trust tier).
    * Do NOT include version information or redundant data in this file.
    * Allowed fields are strictly restricted to: `package`, `trust`, and `maintainers`.
3. **Submit a PR**: Open a Pull Request to the main repository and fill out the provided Checklist in the PR template.
4. **Validation & Merge**: The base CI will automatically validate your JSON format and run structural checks. For `verified` and `official` modules, mandatory manual reviews defined in `CODEOWNERS` are required before merging.

## Local Validation

Before submitting a PR, you can run automated checks locally:

```bash
# Activate Python environment ( >= 3.12 ) and install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install check-jsonschema

# Validate JSON format against the schema
check-jsonschema --schemafile schemas/catalog-entry.schema.json modules/*/*.json

# Run the full directory tree structural check
python scripts/validate_catalog.py
```
