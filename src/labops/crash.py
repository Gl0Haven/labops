import os
import sys

from .config import is_test_instance


def crashpoint(name: str) -> None:
    if os.environ.get("LABOPS_CRASHPOINT") == name and is_test_instance():
        sys.stderr.flush()
        os._exit(97)
