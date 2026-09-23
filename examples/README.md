# Work memory examples

These examples use synthetic content and a local MemoryPalace service. Start `eventmem serve --root ./example-data`, then run:

```sh
EVENTMEM_HOME=./example-data uv run python examples/workflow.py
```

The workflow receives a procedure, saves a session checkpoint, and recalls both in a bounded work query. It uses the generated Python HTTP client and the local token in the selected root.

`callback.py` is an optional local reminder callback. Run `uvicorn examples.callback:app --host 127.0.0.1 --port 8320` and set `EVENTMEM_CALLBACK_DB` to a private SQLite path. The callback records delivery IDs locally; it does not send a message to a recipient. See [work reminders](../docs/reminders.md).
