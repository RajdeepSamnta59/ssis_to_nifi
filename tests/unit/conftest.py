"""Shared fixtures. The corpus is the test data; there is no synthetic input."""

from __future__ import annotations

import pathlib

import pytest

from ssis2nifi.catalog.support import annotate
from ssis2nifi.dtsx.parse import parse_file

ROOT = pathlib.Path(__file__).resolve().parents[2]
CORPUS = ROOT / "corpus" / "packages"

# Genuine Visual Studio packages only. The two negative fixtures are named
# explicitly so a new real package can be dropped in without editing tests.
NEGATIVE = {"negative_synthetic.dtsx", "negative_synthetic_wellformed.dtsx"}


def analyze(path) -> "object":
    """Parse + annotate: what the CLI does, so tests see the same object."""
    return annotate(parse_file(str(path)))


def real_packages() -> list[pathlib.Path]:
    return sorted(p for p in CORPUS.glob("*.dtsx") if p.name not in NEGATIVE)


@pytest.fixture(scope="session")
def l1():
    return analyze(CORPUS / "L1.dtsx")


@pytest.fixture(scope="session")
def l1_guid():
    return analyze(CORPUS / "L1_guid_dialect.dtsx")
