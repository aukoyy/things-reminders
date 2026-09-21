"""Load and save Things UUID ↔ remctl numeric reminder id."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from config import STATE_PATH, ensure_dirs


@dataclass
class State:
    mapping: dict[str, str] = field(default_factory=dict)

    def dump(self) -> dict:
        return {"mapping": self.mapping}

    @classmethod
    def load(cls, path: Path = STATE_PATH) -> State:
        ensure_dirs()
        if not path.exists():
            return cls()
        with path.open(encoding="utf-8") as fh:
            raw = json.load(fh)
        mapping = dict(raw.get("mapping") or {})
        return cls(mapping={str(key): str(value) for key, value in mapping.items()})

    def save(self, path: Path = STATE_PATH) -> None:
        ensure_dirs()
        payload = json.dumps(self.dump(), indent=2, sort_keys=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".mapping.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.write("\n")
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
