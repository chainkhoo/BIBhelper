# coding=utf-8
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent
CORE_SRC = REPO_ROOT / "packages" / "bib_core" / "src"
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bib_core import *  # noqa: F401,F403
from apps.cli.main import main as cli_main


def main():
    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
