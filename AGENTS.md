# AGENTS.md

This repository uses `uv` for dependency management (`pyproject.toml` / `uv.lock`).

## Python Environment (Important)

**When executing any Python-related commands (run, test, install dependencies, etc.), always use the virtual environment interpreter in the project directory:**

```bash
.venv\Scripts\python.exe # Windows
# or
.venv/bin/python # Linux / macOS
```

For example:

```bash
# Run backend
.venv/bin/python web.py

# Install dependencies (uv preferred)
uv pip install -r requirements.txt

# List installed packages
uv pip list
```

Do not use system global `python` / `python3`, and do not create arbitrary new virtual environments.

## Frontend Build

The frontend is located in the `frontend/` directory, using `pnpm` or `npm` as package manager:

```bash
cd frontend
pnpm install # or npm install
pnpm build   # or npm run build
```

## Dependency Import Conventions

**All dependency imports (`import` / `from ... import ...`) must be placed at the top of the file (after module docstring, `__future__` imports, and `__all__`).**

The only exception: **When circular dependencies exist**, delayed local imports inside functions/methods are permitted, with an explanatory comment next to the import statement, e.g.:

```python
def some_func():
    # Circular dependency, delayed import
    from src.auth import verify_token
```

Except for the above exception, scattering import statements inside arbitrary function bodies or conditional branches is prohibited.
