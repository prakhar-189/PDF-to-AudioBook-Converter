"""Entry point for `python -m pdf_audiobook`.

Exists so the tool runs straight from a clone with nothing installed -
no console script, no PATH entry, no `pip install`. The installed
`pdf-audiobook` command (see pyproject.toml) calls the same `main()`.
"""

from .cli import main

raise SystemExit(main())
