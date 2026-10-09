"""
OpsBot Web UI Template Module.
Loads the HTML template for the Web Operations Center.
"""

import os

TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "templates", "index.html")

if os.path.exists(TEMPLATE_PATH):
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as _f:
        HTML_TEMPLATE = _f.read()
else:
    HTML_TEMPLATE = "<!DOCTYPE html><html><body><h1>GreyOrange OpsBot UI</h1></body></html>"
