# Security Policy (SECURITY.md)

> How API keys and secrets are handled in this project.

---

## TL;DR

- **Never paste API keys into chat, issues, screenshots, or commit messages.**
- This repo is **public**. Any key that ever lands in a tracked file is **compromised**.
- Use the `setup_env.ps1` script for safe local entry.

---

## Where keys live

| Location | Tracked by git? | Safe? |
|---|---|---|
| `.env` (local) | NO - in `.gitignore` | YES |
| `.env.example` (template) | YES - no real values | YES |
| `config.yaml` | YES - no real values | YES |
| Source code (`agents/`, `tools/`) | YES | YES only if hardcoded |
| Issue tracker / chat / screenshots | depends | NEVER |

---

## Initial setup (Windows / PowerShell)

```powershell
# Run once. You will be prompted to type each key;
# input is hidden, not echoed, not stored in shell history.
.\scripts\setup_env.ps1
```

## What setup_env.ps1 does

1. Checks if `.env` already exists; if yes, refuses to overwrite.
2. Prompts for `DEEPSEEK_API_KEY` and `MiniMax_API_KEY` using `Read-Host -AsSecureString`.
3. Writes them to `.env` only.
4. Prints a reminder to **rotate** the keys if they were ever shared.

---

## If a key has been leaked

**Treat it as compromised immediately.** Do not just delete the file.

1. Go to the provider's console and **revoke / rotate** the key.
2. Search git history for the leaked key prefix:
   ```bash
   git log -p -S "sk-XXXX" --all
   ```
3. If the key is found in any commit (even old), use `git filter-repo` or BFG to purge history, then force-push.
4. Update `.env` with the new key via `setup_env.ps1`.

---

## In code

Loading keys in code must always go through `os.getenv` after `.env` is loaded:

```python
import os
from dotenv import load_dotenv
load_dotenv()

deepseek_key = os.getenv("DEEPSEEK_API_KEY")
minimax_key  = os.getenv("MiniMax_API_KEY")
```

Never `os.environ[...] = "sk-..."` in source files.
Never commit `*.env`, `*.key`, `*.secret`, `config.local.yaml`.

---

## Reporting leaks

If you accidentally leak a key:

1. **Rotate it now** at the provider.
2. File a private issue (not public) describing the leak window.
3. Wait for confirmation that the new key works, then continue.

---

Last reviewed: 2026-09-12