from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass


class BestmateUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class BestmateResult:
    ok: bool
    output: str
    error: str = ""


def _binary() -> str:
    path = shutil.which("bestmate")
    if not path:
        raise BestmateUnavailable("bestmate CLI not found on PATH")
    return path


def status() -> BestmateResult:
    try:
        p = subprocess.run([_binary(), "status"], capture_output=True, text=True, timeout=15)
        return BestmateResult(ok=p.returncode == 0, output=p.stdout.strip(), error=p.stderr.strip())
    except Exception as e:
        return BestmateResult(ok=False, output="", error=str(e))


def ask(query: str, *, target: str | None = None, timeout: int = 60) -> BestmateResult:
    try:
        cmd = [_binary(), "ask"]
        if target:
            cmd += ["--target", target]
        cmd.append(query)
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return BestmateResult(ok=p.returncode == 0, output=p.stdout.strip(), error=p.stderr.strip())
    except Exception as e:
        return BestmateResult(ok=False, output="", error=str(e))


def ingest(content: str, *, title: str, tags: list[str] | None = None,
           visibility: str = "private", source: str = "forever22",
           timeout: int = 60) -> BestmateResult:
    try:
        cmd = [_binary(), "ingest", "--title", title, "--source", source, f"--{visibility}"]
        if tags:
            cmd += ["--tags", ",".join(tags)]
        p = subprocess.run(cmd, input=content, capture_output=True, text=True, timeout=timeout)
        return BestmateResult(ok=p.returncode == 0, output=p.stdout.strip(), error=p.stderr.strip())
    except Exception as e:
        return BestmateResult(ok=False, output="", error=str(e))
