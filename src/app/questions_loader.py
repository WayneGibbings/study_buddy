"""Load and parse exam JSON files from the questions directory."""

import glob
import json
import os

QUESTIONS_DIR = os.getenv(
    "QUESTIONS_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "questions"),
)


def list_exam_files() -> list[dict]:
    """Return sorted list of available exam files with metadata."""
    files = []
    for path in glob.glob(os.path.join(QUESTIONS_DIR, "*.json")):
        if "Zone.Identifier" in path:
            continue
        try:
            with open(path) as f:
                data = json.load(f)
            files.append(
                {
                    "path": path,
                    "filename": os.path.basename(path),
                    "exam_name": data.get("exam", os.path.basename(path)),
                    "total_questions": len(data.get("questions", [])),
                }
            )
        except Exception:
            pass
    return sorted(files, key=lambda x: x["exam_name"])


def load_exam(path: str) -> dict:
    """Load and return a parsed exam file."""
    with open(path) as f:
        return json.load(f)
