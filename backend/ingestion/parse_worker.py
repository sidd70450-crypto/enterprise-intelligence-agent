"""Entry point of the parser subprocess: ``python -m backend.ingestion.parse_worker``.

Reads one JSON request from stdin, parses the file, and writes one JSON result
to stdout. It is started by sandbox.py with CPU and memory limits and without
the application's secrets in its environment.
"""

import json
import sys
from dataclasses import asdict
from pathlib import Path

from backend.ingestion.parsing import ParseError, parse_file


def main() -> int:
    request = json.loads(sys.stdin.read())
    try:
        pages = parse_file(Path(request["path"]), request["kind"], int(request["max_pdf_pages"]))
        result: dict[str, object] = {"ok": True, "pages": [asdict(page) for page in pages]}
    except ParseError as exc:
        result = {"ok": False, "code": exc.code, "detail": exc.detail}
    except MemoryError:
        result = {"ok": False, "code": "too_large", "detail": "The file needs too much memory."}
    sys.stdout.write(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
