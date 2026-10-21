"""Embed shared UI styles so exported review pages work without the application."""

from mindex.shared.web_assets import frontend_root


def review_style() -> str:
    static = frontend_root() / "shared"
    return "\n".join((static / name).read_text(encoding="utf-8") for name in ("style.css", "review.css"))
