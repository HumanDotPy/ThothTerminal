from __future__ import annotations

from thoth_terminal.dataset_parts import install_dataset_parts


install_dataset_parts(globals())

__all__ = [name for name in globals() if not name.startswith("_") and name != "install_dataset_parts"]
