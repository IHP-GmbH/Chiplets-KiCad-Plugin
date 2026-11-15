# Contributing

How the repository is laid out, and how a new plugin is added.

## Layout

```
Chiplets-KiCad-Plugin/
├── plugins/
│   ├── chiplet_export/       one self-contained pcbnew plugin (Python package)
│   │   ├── __init__.py       registers the ActionPlugin with pcbnew
│   │   ├── ...               action, dialog, pipeline/, writers/, tests/
│   │   ├── requirements.txt  that plugin's worker venv deps
│   │   └── README.md         that plugin's docs
│   └── resizer_passive_elements/   same shape, one plugin per directory
├── docs/                     pages adk-docs includes; docs/reference/ is generated
├── scripts/                  write_cli_help.py writes docs/reference/
├── .github/workflows/        CI, one matrix entry per plugin
├── pyproject.toml            test configuration for every plugin's suite
├── conftest.py               sibling checkouts for those suites
├── LICENSE                   GPL-3.0-or-later, applies repo-wide
├── README.md                 the repository's front page
└── CONTRIBUTING.md           this file
```

Each plugin directory is a Python package in its own right: its `__init__.py`
registers the pcbnew action and its modules use relative imports, so the
package name is whatever the directory is called (`chiplet_export`). Nothing at
the repository root is a Python package; the root only carries shared metadata.

## Adding a new plugin

1. Create `plugins/<new_name>/` with its own `__init__.py`, an `ActionPlugin`
   subclass that registers on import, a `README.md`, and a `tests/` suite. Use
   relative imports inside the package so it is loadable under `<new_name>`.
2. If it ships tests, give it a `requirements.txt`, add its requirements to
   the root `pyproject.toml` dependencies and its `tests` directory to
   `testpaths` there, and add `<new_name>` to the `matrix.plugin` list in
   `.github/workflows/tests.yml`. Every suite runs from the repository root
   under the one pytest configuration in `pyproject.toml`.
3. Keep the plugin self-contained: shared code, if it ever appears, is a
   separate decision, not an implicit dependency between plugin directories.
