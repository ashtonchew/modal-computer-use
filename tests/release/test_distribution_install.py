from __future__ import annotations

import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = runpy.run_path(str(ROOT / "scripts" / "smoke_distribution_install.py"))
select_distributions = SCRIPT["select_distributions"]


def test_select_distributions_requires_exact_pair(tmp_path: Path) -> None:
    wheel = tmp_path / "modal_computer_use-1.1.0-py3-none-any.whl"
    sdist = tmp_path / "modal_computer_use-1.1.0.tar.gz"
    wheel.touch()
    sdist.touch()

    assert select_distributions(tmp_path) == (wheel, sdist)


@pytest.mark.parametrize("extra_name", [None, "duplicate.whl", "duplicate.tar.gz"])
def test_select_distributions_rejects_missing_or_duplicate_files(
    tmp_path: Path, extra_name: str | None
) -> None:
    if extra_name is not None:
        (tmp_path / "modal_computer_use-1.1.0-py3-none-any.whl").touch()
        (tmp_path / "modal_computer_use-1.1.0.tar.gz").touch()
        (tmp_path / extra_name).touch()

    with pytest.raises(ValueError, match="expected one wheel and one sdist"):
        select_distributions(tmp_path)
