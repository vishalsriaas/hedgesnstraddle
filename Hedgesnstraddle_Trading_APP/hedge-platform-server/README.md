# Hedge Platform Server Package

This archive is the single deployable package for the Hedge Platform. It contains:

- The `hedge_trader` Frappe app and embedded legacy hedge runtime.
- The `straddle_bot` Frappe app and embedded legacy straddle runtime.
- Runtime lifecycle, proxy, deployment, and health-check scripts.
- One installer for an existing Linux Frappe v15 bench.

## Server prerequisites

- A supported Linux server.
- An operational Frappe v15 bench with MariaDB, Redis, Node, Yarn, Python, and Bench.
- An existing Frappe site.
- Enough permissions for the deployment user to modify the bench.

The archive intentionally does not include the Windows virtual environment, databases, logs,
credentials, downloaded Frappe framework source, or backup copies.

## Install

```bash
tar -xzf hedge-platform-server.tar.gz
cd hedge-platform-server
./install.sh --bench /home/frappe/frappe-bench --site trading.example.com
```

The installer does not start market-connected runtimes. After configuring credentials and keeping
both strategies in paper mode, start them explicitly:

```bash
cd /home/frappe/frappe-bench
START_TRADING_WORKERS=1 FRAPPE_SITE=trading.example.com scripts/start_runtime_workers.sh
```

Run verification again at any time:

```bash
cd /home/frappe/frappe-bench
FRAPPE_SITE=trading.example.com scripts/health_check.sh
```

Existing copies of either custom app are backed up under the bench's `backups` directory before
replacement. Site databases are migrated but never deleted by this package.
