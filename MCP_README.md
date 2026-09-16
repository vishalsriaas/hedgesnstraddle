# Read-only application MCP

This local stdio server gives MCP-compatible agents access to saved Hedges &
Straddle trading data. It runs independently of FastAPI and the trading engines.
Python 3.10+ is required.

## Install

From this repository, using uv:

```powershell
uv venv .mcp-venv
uv pip install --python .mcp-venv/Scripts/python.exe -r requirements-mcp.txt
```

The separate environment keeps MCP dependencies independent of the application.
The server uses the official [MCP Python SDK v1](https://py.sdk.modelcontextprotocol.io/v1/).

## Connect an agent

Merge this entry into your MCP client's `mcpServers` configuration (adjust paths
if you move the repository). The client launches the process over stdin/stdout;
there is no network port or application login to configure.

```json
{
  "mcpServers": {
    "hedges-straddle-readonly": {
      "command": "D:/desktop/Testing/Hedgesnstraddle/HnS_application_11aug/.mcp-venv/Scripts/python.exe",
      "args": [
        "-B",
        "D:/desktop/Testing/Hedgesnstraddle/HnS_application_11aug/readonly_mcp.py",
        "--database",
        "D:/desktop/Testing/Hedgesnstraddle/HnS_application_11aug/hedgesnstraddle.db"
      ]
    }
  }
}
```

Client configuration formats vary; use the same executable and arguments in
clients with a different configuration layout. Restart/reload the client's MCP
connections after adding the entry. No client configuration is changed by installation.

Example agent request: "Use hedges-straddle-readonly to list datasets, inspect
the latest hedge orders and positions, and summarize saved runtime status."

## Tools and access boundaries

- `list_datasets`: approved dataset names, columns, and configuration keys.
- `read_records`: saved sessions, orders, fills, positions, ledgers, PnL,
  strategy settings, and runtime/health records. Defaults to 50 rows, maximum
  200, newest ID first. Pass the returned `next_offset` to continue. Child
  records can be filtered by `session_id`.
- `read_configuration`: approved configuration keys for `hedge` or `straddle`.

SQLite connections use `mode=ro`, `query_only`, and an authorizer that permits
only SELECT and approved column reads. Queries use fixed projections and bound
values. Missing database files fail without creating a database. The process
does not import the application, start engines, contact exchanges, execute shell
commands, expose arbitrary SQL, read arbitrary files, or offer mutation tools.
Accounts/password hashes, credential settings, free-form notes, event payloads,
and audit values are excluded. New tables/columns/configuration keys must be
explicitly added to the allowlists in `readonly_mcp.py`.

The agent can see financial records exposed by these tools. Local access is
controlled by who can launch the process and read the database using OS
permissions; this server does not apply the application's user roles. Restrict
those permissions if multiple people use this machine. The MCP restrictions
apply to these tools, not to other shell/filesystem tools your agent may have.

Values are persisted snapshots: prices, PnL, and heartbeats may be stale.
Separate calls can observe different database states while trading is active.
A schema mismatch or locked/unavailable database produces a tool error rather
than initializing or migrating it. This server supports the app's SQLite
database only. Use a client with local stdio support.

## Verify

```powershell
.\.mcp-venv\Scripts\python.exe -m unittest discover -s tests -p test_readonly_mcp.py -v
```

Tests use a temporary database and include an actual MCP stdio handshake/tool
call, pagination, secret exclusion, invalid input, and rejected database writes.
