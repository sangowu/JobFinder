"""Extract the main inline UI script and validate it with Node.js."""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


def main() -> None:
    root = Path(__file__).parents[1]
    html = (root / "jobradar" / "templates" / "index.html").read_text(encoding="utf-8")
    start = html.rfind("<script>")
    end = html.rfind("</script>")
    if start < 0 or end <= start:
        raise SystemExit("Main inline script block was not found")
    with tempfile.TemporaryDirectory() as directory:
        script = Path(directory) / "jobradar-ui.js"
        script.write_text(html[start + len("<script>"):end], encoding="utf-8")
        subprocess.run(["node", "--check", str(script)], check=True)


if __name__ == "__main__":
    main()
