# UniFi Relative-Import Gotcha

## Summary
A plugin test `conftest.py` that passes `submodule_search_locations=[]` to `spec_from_file_location` will silently load **two copies** of any submodule that uses intra-plugin relative imports, producing two distinct class objects and breaking every `isinstance` check and `except` clause that spans them. Omit the kwarg.

## Details

### The symptom

Running the unifi plugin's tests showed two failures in `TestGracefulDegradation`:

```
gilbert_plugin_unifi.client.UniFiConnectionError: down
```

— even though `presence.py` had an explicit `except UniFiConnectionError` around the call. The exception class raised by the mock and the class named in the `except` clause looked identical, but the `except` wasn't catching.

### The cause

The conftest was loading each submodule like this:

```python
_spec = importlib.util.spec_from_file_location(
    f"{_pkg_name}.{_mod_name}",
    _plugin_dir / f"{_mod_name}.py",
    submodule_search_locations=[],   # <-- the culprit
)
```

Passing **any** value (even `[]`) for `submodule_search_locations` marks the module as a **package** — it gets a `__path__` attribute. Once `presence.py` is itself treated as a package with `__path__ = []`, its `__package__` becomes `gilbert_plugin_unifi.presence` (self) instead of `gilbert_plugin_unifi` (parent). When presence.py then runs `from .client import UniFiConnectionError`, Python resolves the relative import against `__package__`, which means `gilbert_plugin_unifi.presence.client` — a different module entry than `gilbert_plugin_unifi.client`. Python re-loads `client.py` from disk under the new name, creating a second `UniFiConnectionError` class object.

Both classes have the same name, same repr, same module-dotted-path in the traceback — but `isinstance(instance_of_first, second_class)` is `False`.

### The fix

Don't pass `submodule_search_locations` to `spec_from_file_location` when loading plain submodules in a conftest. Let Python default it to `None`, which keeps the module a plain module whose `__package__` is the parent plugin:

```python
_spec = importlib.util.spec_from_file_location(
    f"{_pkg_name}.{_mod_name}",
    _plugin_dir / f"{_mod_name}.py",
    # no submodule_search_locations — we want plain modules, not packages
)
```

Verified by diagnostic: after the fix, `from gilbert_plugin_unifi.client import UniFiConnectionError as A; from gilbert_plugin_unifi.presence import UniFiConnectionError as B; assert A is B` passes.

### Who this affects

Any plugin whose modules do **intra-plugin relative imports** from each other (e.g. `from .client import X` inside `presence.py`). Single-module plugins like `tesseract` or `ngrok` don't hit this because they have no cross-module references. Multi-module plugins are the at-risk group — `unifi`, `google`, and any future plugin that factors shared code into its own module.

## Related
- `unifi/tests/conftest.py` — has the fix in place with a long comment explaining why
- `unifi/presence.py` — the entry point for the broken import chain
- `unifi/client.py` — where `UniFiConnectionError` is defined
- Gilbert root `CLAUDE.md` — plugin development guidelines
