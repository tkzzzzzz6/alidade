from __future__ import annotations

import os
from pathlib import Path

import pytest

from alidade.datalake.workspace import Workspace
from alidade.security.path_safety import ConfinedDir


pytestmark = [pytest.mark.backend]


class _ScratchOnlyWorkspace:
    def __init__(self, scratch: Path):
        self.confined_scratch = ConfinedDir(scratch)


def test_prune_scratch_uses_backend_neutral_confined_directory(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    workspace = _ScratchOnlyWorkspace(scratch)
    oldest = scratch / "oldest.bin"
    newest = scratch / "newest.bin"
    oldest.write_bytes(b"a" * 8)
    newest.write_bytes(b"b" * 8)
    os.utime(oldest, (1, 1))
    os.utime(newest, (2, 2))

    freed = Workspace.prune_scratch(workspace, max_bytes=8)

    assert freed == 8
    assert not oldest.exists()
    assert newest.exists()

