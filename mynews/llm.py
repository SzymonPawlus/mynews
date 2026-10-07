"""LLM backends. Default: headless Claude Code on the user's subscription."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import DATA_DIR
from .db import now_iso


class LLMError(RuntimeError):
    pass


@dataclass
class LLMResult:
    data: dict
    usage: dict


class LLMBackend(Protocol):
    def complete(self, *, system: str, user: str, schema: dict, model: str,
                 label: str) -> LLMResult: ...


class ClaudeCLIBackend:
    """Runs `claude -p` with a replaced system prompt, no tools and structured output.

    Auth comes from the CLI's own login, or CLAUDE_CODE_OAUTH_TOKEN (from
    `claude setup-token`) on a headless server.
    """

    def __init__(self, binary: str | None = None, timeout: int = 900):
        self.binary = binary or os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "claude"
        self.timeout = timeout

    def complete(self, *, system: str, user: str, schema: dict, model: str,
                 label: str) -> LLMResult:
        # run in an empty dir so no CLAUDE.md or project settings leak into the context
        with tempfile.TemporaryDirectory(prefix="mynews-") as tmp:
            sp = Path(tmp) / "system.md"
            sp.write_text(system)
            cmd = [
                self.binary, "-p",
                "--model", model,
                "--output-format", "json",
                "--system-prompt-file", str(sp),
                "--json-schema", json.dumps(schema),
                "--tools", "",
                "--strict-mcp-config",
                "--setting-sources", "",
                "--no-session-persistence",
            ]
            try:
                proc = subprocess.run(cmd, input=user, capture_output=True, text=True,
                                      cwd=tmp, timeout=self.timeout)
            except subprocess.TimeoutExpired as e:
                raise LLMError(f"{label}: claude timed out after {self.timeout}s") from e
        try:
            out = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise LLMError(f"{label}: non-JSON output (exit {proc.returncode}): "
                           f"{(proc.stderr or proc.stdout)[:500]}") from e
        if out.get("is_error") or out.get("structured_output") is None:
            raise LLMError(f"{label}: {out.get('subtype')} {out.get('result', '')[:500]}")
        usage = {
            "label": label, "at": now_iso(), "model": model,
            "input": out["usage"].get("input_tokens", 0)
                     + out["usage"].get("cache_creation_input_tokens", 0)
                     + out["usage"].get("cache_read_input_tokens", 0),
            "output": out["usage"].get("output_tokens", 0),
            "equiv_cost_usd": out.get("total_cost_usd"),
            "duration_s": round(out.get("duration_ms", 0) / 1000, 1),
        }
        log_usage(usage)
        return LLMResult(out["structured_output"], usage)


def log_usage(usage: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(DATA_DIR / "usage.jsonl", "a") as f:
        f.write(json.dumps(usage) + "\n")


def get_backend() -> LLMBackend:
    kind = os.environ.get("MYNEWS_LLM_BACKEND", "claude-cli")
    if kind == "claude-cli":
        return ClaudeCLIBackend()
    raise ValueError(f"unknown MYNEWS_LLM_BACKEND {kind!r}")
