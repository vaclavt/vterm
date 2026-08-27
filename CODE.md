# Code Style and Rules

## Python

### Logging

Use module-level loggers:

```python
import logging

LOG = logging.getLogger(__name__)
```

Use structured and parameterized logging:

```python
LOG.info("Export started: export_type=%s", export_type)
```

Guidelines:

* Log important execution flow and state changes
* Log errors with enough context for debugging
* Use parameterized logging (`%s`) instead of f-strings
* Do not log secrets, passwords, tokens, personal data, or large payloads
* Use appropriate log levels:
  * `debug` — detailed diagnostic information
  * `info` — normal application flow
  * `warning` — unexpected but recoverable situations
  * `error` — failed operations
  * `exception` — errors with stack traces

An `except` block that swallows a failure and continues must preserve the stack, with
`LOG.exception(...)` or `LOG.warning(..., exc_info=True)`. Logging only `str(exc)` loses
which failure mode fired and where.

Message form:

* `"<stage>.<step>: <what>"` for progress — `LOG.info("asset_render.symbols: generating symbol=%s", name)`
* `key=%s` pairs once a message carries more than one value; a single value may read as prose
* bracketed markers for lifecycle transitions — `[start]`, `[run]`, `[done]`, `[loading]`

`mmgen/cli.py` (pipeline start) is the reference for a multi-value message.

For function tracing, log only meaningful arguments and results when useful for debugging:

```python
LOG.debug("Creating export: design size=%s", len(design))
result = exporter.run()
LOG.debug("Export completed: items=%s", len(result))
```

### Doc strings

**Module doc strings.** The first line is a concise summary of what the module is for:

```python
""" Simple minislot images generation """
```

Further paragraphs are allowed, and encouraged, only to record *why* the module exists or a
non-obvious constraint it encodes — a failure observed in practice, an external format
requirement, a decision a future reader would otherwise undo. Naming config keys, context
keys or sibling modules is fine when that is the constraint being explained. Do not restate
the module's API. See `mmgen/asset_design/framing.py` for a justified long one.

**Function and method doc strings** use this format:

```python
def update_columns_by_filter_cond(
    table: Base,
    filter_cond: Any,
    columns: dict,
    session: Optional[Session] = None,
) -> list[Base]:
    """
    Update specified columns of all rows in the database matching a given filter condition.

    Args:
        table: The database table to update.
        filter_cond: SQLAlchemy filter condition to select rows.
        columns: A dictionary containing column names as keys and new values.
        session: The database session to use.

    Returns:
        list[Base]: A list of refreshed ORM instances reflecting the updates.

    Raises:
        RuntimeError: If the table is not mapped.
    """
```

* Types belong in the signature, not in `Args:` — a doc string type is a second source of
  truth that drifts, and pyright already checks the real one
* `Returns:` leads with the type, because there is no parameter name to carry it
* `Raises:` documents the exceptions a caller is expected to handle
* Omit `Args:`/`Returns:` on a private helper whose signature is self-evident; a one-line
  summary is enough
* A `Step.run()` override needs no doc string when the class doc string says what the step
  does — the contract is documented once on the base class in `mmgen/pipeline.py`

### Type hints

 - when optional type use Optional. Eg. do not use "dict | None", use "Optional[dict]"
 - annotate every parameter and return type; parameterize containers (`dict[str, int]`, not
   bare `dict`) where the element types are known
 - no suppression comments (`# type: ignore`, `# pyright: ignore`) — solve the typing
   problem instead

### Code comments

Use comments only when they add non-obvious context.

Guidelines:

* Prefer clear naming and small functions over comments
* Comment:
  * business logic
  * important decisions
  * workarounds or edge cases
  * logical sections in long functions
* Keep comments short and explain **why**, not **what**
* Update comments when code changes
* Avoid comments that restate the code
* Don't add and don't remove comments to a block of code if you did not create it or modify it.

Form:

* A comment sits on its own line above the code it describes — never trailing
* No `# ---- section ----` banners
* A comment above a constant explains what the *value* means; it must not paraphrase the
  identifier

### Errors and failures

* No custom exception classes
* `RuntimeError` for operational failures — missing context prerequisites, missing files,
  unknown names, unsupported configs
* `ValueError` for value and domain validation — pydantic validators, pure helpers
* Guard-clause-then-raise at the top of `run()`, naming the stage that should have run first:

```python
section = ctx.design.get("symbols_section")
if section is None:
    raise RuntimeError("symbols_section not in context; run the game_design stage first")
```

* Let failures propagate. `Pipeline.run` does not catch, so a failing step aborts the run
* Catch narrowly, and only to continue with a degraded result. Re-raise wrapped with
  `from exc` (see `mmgen/shapes/registry.py`)
* Error message form: `"<context>: <problem>; <what to do>"`, listing the valid options where
  there is a closed set. f-strings are correct here, unlike in logging — the message is built
  once, at the point of failure
* Validate cheaply before expensive work, so a typo fails before any paid LLM call
  (`prompts/registry.validate()`, `shapes/registry.validate()` run at stage head)

### Imports and paths

* Absolute imports only. No relative imports, no star imports
* Three groups, blank-line separated, alphabetized within group: stdlib → third-party →
  `mmgen.*`
* `__init__.py` holds a doc string only — no re-exports. Import from the concrete module
* `pathlib` only, never `os.path`. Join with `/`, prefer `Path.read_text(...)`, and always
  pass `encoding="utf-8"` explicitly

### Tests

There is no unit-test suite. Correctness is checked by running the pipeline end to end.

If you add tests, put them in `tests/` with pytest and add pytest to the `dev` dependency
group.

### Tooling

`black` (line-length 140) is the formatter and `pyright` is the type checker. No linter is
configured. See the *Dev tools* section of `README.md` for the commands.
