# Chiplets-KiCad-Plugin

KiCad (pcbnew) plugins for the IHP heterogeneous-integration ecosystem. This
repository hosts every current and future KiCad plugin built for chiplet and
interposer design on IHP SG13G2, so they share one license, one CI setup and
one place to look. Each plugin is self-contained under `plugins/<name>/` and is
installed independently.

## Plugins

| Plugin | What it does |
|--------|--------------|
| [`plugins/chiplet_export`](plugins/chiplet_export/README.md) | pcbnew action plugin that drives the HYP -> GDS -> canonical `.chiplet` pipeline in one click. Produces the canonical `.chiplet`, the interposer GDS, the driving Hyperlynx `.hyp`, and optionally a complete-assembly GDS. Consumed by adk-tools as a submodule. |

## Layout

```
Chiplets-KiCad-Plugin/
├── plugins/
│   └── chiplet_export/       one self-contained pcbnew plugin (Python package)
│       ├── __init__.py       registers the ActionPlugin with pcbnew
│       ├── ...               action, dialog, pipeline/, writers/, tests/
│       ├── requirements.txt  that plugin's worker venv deps
│       └── README.md         that plugin's docs
├── .github/workflows/        CI, one matrix entry per plugin
├── LICENSE                   GPL-3.0-or-later, applies repo-wide
└── README.md                 this file
```

Each plugin directory is a Python package in its own right: its `__init__.py`
registers the pcbnew action and its modules use relative imports, so the
package name is whatever the directory is called (`chiplet_export`). Nothing at
the repository root is a Python package; the root only carries shared metadata.

## Installing a plugin

Symlink (recommended for development) or copy the specific plugin directory,
not the whole repository, into KiCad's scripting plugins folder. For
`chiplet_export` on Linux with KiCad 9.0:

```bash
ln -s /path/to/Chiplets-KiCad-Plugin/plugins/chiplet_export \
      ~/.config/kicad/9.0/scripting/plugins/chiplet_export
```

Then follow that plugin's own README for its worker venv and dependencies
(`plugins/chiplet_export/README.md`).

## Adding a new plugin

1. Create `plugins/<new_name>/` with its own `__init__.py`, an `ActionPlugin`
   subclass that registers on import, a `README.md`, and a `tests/` suite. Use
   relative imports inside the package so it is loadable under `<new_name>`.
2. If it ships tests, give it a `requirements.txt` and add `<new_name>` to the
   `matrix.plugin` list in `.github/workflows/tests.yml`; CI runs each plugin's
   suite from its own directory.
3. Keep the plugin self-contained: shared code, if it ever appears, is a
   separate decision, not an implicit dependency between plugin directories.

## License

GPL-3.0-or-later for the repository. See `LICENSE`. Individual plugins may
vendor code under compatible licenses and note it in their own README (for
example `chiplet_export` vendors a GPL-2.0-or-later Hyperlynx writer derived
from KiCad, redistributed under the "or later" clause).
