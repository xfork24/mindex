"""Embed shared UI styles so exported review pages work without the application."""

from pathlib import Path


def review_style() -> str:
    static = Path(__file__).parent / "static"
    return "\n".join((static / name).read_text(encoding="utf-8") for name in ("style.css", "review.css"))
