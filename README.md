# tinyaisense

## Development

The Python packages live in one uv workspace under `packages/`:

| Package | Python | Path |
|---|---|---|
| `tinyaisense-wire` | ≥3.11 | `packages/wire` |
| `tinyaisense-hub` | ≥3.14 | `packages/hub` |
| `tinyaisense-sim` | ≥3.14 | `packages/sim` |
| `tinyaisense` (SDK) | ≥3.11 | `packages/sdk` |

```sh
uv sync --all-packages
uv run ruff check && uv run ruff format --check
uv run pyright
uv run pyright --pythonversion 3.11 packages/wire packages/sdk
uv run pytest
```
