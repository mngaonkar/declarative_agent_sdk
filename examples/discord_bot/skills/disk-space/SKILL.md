---
name: disk-space
description: Check free and used disk space on the host. Use when the user asks
  about disk space, storage, free space, or how full a volume is.
---

# Disk space

## Procedure

1. Run a single shell command to inspect mounted filesystems.
2. Prefer human-readable sizes.

Use:

```
exec_command with command: df -h
```

On macOS, `df -h` is fine. On Linux the same. Summarise the result for the user:

- Highlight volumes that are **>85% full**.
- Give total / used / available for the main volume(s).
- Do **not** dump raw tables only — add a short plain-language summary.

## Do not

- Do not invent numbers without running the tool.
- Do not run destructive commands.
