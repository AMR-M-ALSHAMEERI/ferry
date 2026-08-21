"""Regenerate schemas/ucs-<version>.json from the pydantic models.

Run after any change to ferry.ucs.models:

    python scripts/gen_schema.py

CI does not run this; the checked-in schema is verified by
tests/test_schema_current.py, which fails if it drifts from the models.
"""

import json
from pathlib import Path

from ferry.ucs import UCS_VERSION, Conversation

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"


def schema_path() -> Path:
    return SCHEMA_DIR / f"ucs-{UCS_VERSION}.json"


def build_schema() -> dict[str, object]:
    schema = Conversation.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = f"Ferry Universal Conversation Schema v{UCS_VERSION}"
    return schema


def main() -> None:
    SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
    dest = schema_path()
    dest.write_text(json.dumps(build_schema(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {dest}")


if __name__ == "__main__":
    main()
