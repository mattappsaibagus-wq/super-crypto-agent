# Vendored: Kronos

Source: https://github.com/shiyu-coder/Kronos
Commit: 67b630e67f6a18c9e9be918d9b4337c960db1e9a (2026-04-13)
License: MIT (see LICENSE in this folder) — kept unmodified as required.

Only the `model/` package is vendored (inference code). Pre-trained weights
are downloaded from Hugging Face at runtime (NeoQuasar/Kronos-small +
NeoQuasar/Kronos-Tokenizer-base) and cached by the GitHub Actions workflow.

Copied from the same vendored snapshot used in mattappsaibagus-wq/stock-scanner.

To update: replace `model/*.py` with a newer commit's files and bump the
commit hash above.
