"""Select the requested half of paired Chinese / English analysis, retaining legacy content."""

import re


def select_analysis(value: str, language: str = "zh") -> str:
    for match in re.finditer(r"\s+/\s+", value):
        zh, en = value[: match.start()].strip(), value[match.end() :].strip()
        if (
            re.search(r"[\u3400-\u9fff]", zh)
            and re.match(r"[^\w]*[A-Za-z0-9]", en)
            and not en.startswith(("http://", "https://"))
        ):
            return en if language == "en" else zh
    return value
