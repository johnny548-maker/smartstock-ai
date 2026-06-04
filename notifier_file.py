# -*- coding: utf-8 -*-
"""Write the daily report to disk as Markdown (+ a simple HTML view).
The local file is the always-on base channel — written before email so a
mail misconfig never costs you the report."""
import logging
import os

from config import REPORT_DIR

log = logging.getLogger(__name__)

_HTML = (
    "<!DOCTYPE html><html lang='zh-Hant'><head><meta charset='utf-8'>"
    "<meta name='viewport' content='width=device-width, initial-scale=1'>"
    "<title>SmartStock {date}</title>"
    "<style>body{{font-family:-apple-system,'Segoe UI',sans-serif;max-width:760px;"
    "margin:24px auto;padding:0 16px;line-height:1.6;color:#1a1a1a}}"
    "pre{{white-space:pre-wrap;word-wrap:break-word;font-family:inherit}}</style>"
    "</head><body><pre>{body}</pre></body></html>"
)


def write_report(markdown, date_str, out_dir=REPORT_DIR):
    """Write reports/<date>.md and reports/<date>.html. Return the .md path."""
    os.makedirs(out_dir, exist_ok=True)
    md_path = os.path.join(out_dir, f"{date_str}.md")
    html_path = os.path.join(out_dir, f"{date_str}.html")

    with open(md_path, "w", encoding="utf-8") as f:
        f.write(markdown)

    safe = markdown.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(_HTML.format(date=date_str, body=safe))

    log.info("report file written: %s", md_path)
    return md_path
