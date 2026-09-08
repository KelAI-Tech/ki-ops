"""Build-time git SHA embedding.

Everything declarative lives in pyproject.toml; this file exists only to
write ki_ops/_build_info.py (GIT_SHA of the built checkout) into the wheel,
so any wheel — released or ad-hoc — is traceable to its exact source commit
via `ki_ops.__git_sha__` / `ki-ops --version`.

The module is generated into the build tree only, never into src/, and is
absent from source checkouts (ki_ops/__init__.py falls back to "unknown",
e.g. when building from an sdist without git metadata).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py


def _git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).parent,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


class BuildPyWithGitSha(build_py):
    def run(self) -> None:
        super().run()
        target = Path(self.build_lib) / "ki_ops" / "_build_info.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            '"""Generated at build time by setup.py; not in source control."""\n'
            f'GIT_SHA = "{_git_sha()}"\n'
        )


setup(cmdclass={"build_py": BuildPyWithGitSha})
