"""Opt-in public-data audit. Never starts engines or opens the application database.

Run from repository root: python tests/manual_binance_trade_audit.py --seconds 300
Requires the already-installed websockets package; not part of offline discovery.
"""
import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
import websockets
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.core import futures_targets as ft
from app.core.binance_client import get_futures_aggregate_trades
from app.models.schema import Base, HedgeTradeOrder, HedgeSessionEvent

WS = 'wss://fstream.binance.com/market/ws/btcusdt@aggTrade'


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2), encoding='utf-8')


def mismatch(a, b):
    return [k for k in ('p', 'q', 'nq', 'T', 'f', 'l', 'm')
            if k in a and k in b and
            (Decimal(a[k]) != Decimal(b[k]) if k in ('p', 'q', 'nq') else a[k] != b[k])]


async def run(seconds, output):
    output.mkdir(parents=True, exist_ok=False)
    reference, processed, visits, logs = {}, {}, Counter(), []
    ready, stop = asyncio.Event(), asyncio.Event()
    started = time.monotonic()
    ws_errors, rest_errors = [], []
    source_hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in map(Path, ['app/core/futures_targets.py', 'app/core/binance_client.py'])}
    raw_ws = (output / 'websocket.jsonl').open('w', encoding='utf-8')
    raw_rest = (output / 'rest_pages.jsonl').open('w', encoding='utf-8')
    log_file = (output / 'progress.jsonl').open('w', encoding='utf-8')

    def log(kind, **fields):
        record = dict(kind=kind, elapsed=round(time.monotonic()-started, 3), **fields)
        logs.append(record)
        log_file.write(json.dumps(record)+'\n'); log_file.flush()
        print(json.dumps(record), flush=True)

    async def reference_stream():
        while not stop.is_set():
            try:
                async with websockets.connect(WS, open_timeout=15, max_queue=8192) as ws:
                    log('websocket_connected')
                    while not stop.is_set():
                        try:
                            data = json.loads(await asyncio.wait_for(ws.recv(), 2))
                        except asyncio.TimeoutError:
                            continue
                        if data.get('e') != 'aggTrade' or data.get('s') != 'BTCUSDT':
                            raise ValueError('Unexpected reference event')
                        raw_ws.write(json.dumps(data)+'\n'); raw_ws.flush()
                        reference[data['a']] = data
                        ready.set()
            except Exception as exc:
                ws_errors.append(str(exc)); log('websocket_error', error=str(exc))
                await asyncio.sleep(1)

    capture = asyncio.create_task(reference_stream())
    await asyncio.wait_for(ready.wait(), 45)
    async with httpx.AsyncClient(timeout=10) as client:
        before = time.time()*1000
        response = await client.get('https://fapi.binance.com/fapi/v1/time')
        response.raise_for_status()
        offset = response.json()['serverTime'] - (before + time.time()*1000)/2
    active = datetime.now(timezone.utc)
    started = time.monotonic()
    end_time = active + timedelta(seconds=seconds)
    db_engine = create_engine('sqlite:///' + str(output / 'audit.sqlite3'))
    Base.metadata.create_all(db_engine)
    DB = sessionmaker(bind=db_engine, autoflush=False)
    db = DB()
    order = HedgeTradeOrder(session_id=1, symbol='BTC-USDT-FUTURES', trader_leg='audit',
        side='SELL', qty=1, price=1e12, status='PENDING', order_type='LIMIT')
    event = ft.create_target(db, order, HedgeSessionEvent, active)
    db.commit()
    log('start', active_ms=ft.millis(active), duration_seconds=seconds, server_clock_offset_ms=round(offset, 2))
    current_rows, outage_counts = [], Counter()
    restarted = False

    class ObservedRows(list):
        def __iter__(self):
            for row in super().__iter__():
                current_rows.append(row)
                yield row

    async def observed_fetch(symbol, **kwargs):
        elapsed = time.monotonic() - started
        for name, lo, hi in [('25s_timeout', 45, 70), ('45s_disconnect', 135, 180)]:
            if lo <= elapsed < hi:
                outage_counts[name] += 1
                raise ValueError('INJECTED_DATA_GAP: ' + name)
        rows = await get_futures_aggregate_trades(symbol, **kwargs)
        raw_rest.write(json.dumps(dict(elapsed=elapsed, params=kwargs, rows=rows))+'\n'); raw_rest.flush()
        return ObservedRows(rows)

    with patch.object(ft, 'get_futures_aggregate_trades', observed_fetch):
        # Includes a drain period so the reference and REST boundary are closed.
        while time.monotonic()-started < seconds+15:
            current_rows.clear()
            try:
                previous = json.loads(event.payload_json)['last_id']
                result = await ft.replay_target(order, event, datetime.now(timezone.utc), until=end_time)
                if result is not None:
                    raise AssertionError('Unreachable audit target filled')
                db.commit()
                state = json.loads(event.payload_json)
                for row in current_rows:
                    if (state['last_id'] is not None and row['a'] <= state['last_id']
                            and (previous is None or row['a'] > previous)
                            and ft.millis(active) < row['T'] <= ft.millis(end_time)):
                        visits[row['a']] += 1
                        processed[row['a']] = row
                log('poll', reference=len(reference), processed=len(processed),
                    last_id=state['last_id'], checked_ms=state['checked_ms'])
            except Exception as exc:
                db.rollback()
                rest_errors.append(str(exc)); log('poll_error', error=str(exc))
            if not restarted and time.monotonic()-started >= 105:
                db.close(); db_engine.dispose()
                db_engine = create_engine('sqlite:///' + str(output / 'audit.sqlite3'))
                DB = sessionmaker(bind=db_engine, autoflush=False)
                db = DB()
                order, event = ft.target_for_session(db, HedgeTradeOrder, HedgeSessionEvent, 1)
                restarted = True
                log('restart', state=json.loads(event.payload_json))
            await asyncio.sleep(5)
    stop.set()
    await capture
    raw_ws.close(); raw_rest.close(); log_file.close()
    state = json.loads(event.payload_json)
    db.close(); db_engine.dispose()
    reference = {a:r for a,r in reference.items() if ft.millis(active) < r['T'] <= ft.millis(end_time)}
    ids = sorted(reference)
    baseline = {}
    # Independent historical fetch after the run, using explicit ID pagination.
    if ids:
        async with httpx.AsyncClient(timeout=15) as client:
            cursor = ids[0]
            for page in range(100):
                response = await client.get('https://fapi.binance.com/fapi/v1/aggTrades',
                    params=dict(symbol='BTCUSDT', fromId=cursor, limit=1000))
                response.raise_for_status()
                rows = response.json()
                if not rows:
                    break
                for row in rows:
                    if ids[0] <= row['a'] <= ids[-1]:
                        baseline[row['a']] = row
                cursor = rows[-1]['a']+1
                if cursor > ids[-1]:
                    break
                await asyncio.sleep(.1)
    write_json(output / 'rest_processed.json', processed)
    write_json(output / 'independent_history.json', baseline)
    gaps = [(x+1,y-1) for x,y in zip(ids,ids[1:]) if y != x+1]
    differences = {a:mismatch(reference[a],processed[a]) for a in reference.keys() & processed.keys()}
    differences = {a:d for a,d in differences.items() if d}
    report = dict(duration_seconds=seconds, active_utc=active.isoformat(), end_utc=end_time.isoformat(),
        reference_records=len(reference), processed_records=len(processed), independent_records=len(baseline),
        first_id=ids[0] if ids else None, last_id=ids[-1] if ids else None,
        websocket_gaps=gaps, websocket_errors=ws_errors,
        missing_from_polling=sorted(reference.keys()-processed.keys()),
        extra_in_polling=sorted(processed.keys()-reference.keys()),
        independent_history_missing_from_polling=sorted(baseline.keys()-processed.keys()),
        independent_history_missing_from_websocket=sorted(baseline.keys()-reference.keys()),
        mismatched_fields=differences,
        independent_field_mismatches={a:mismatch(baseline[a],processed[a]) for a in baseline.keys() & processed.keys() if mismatch(baseline[a],processed[a])},
        duplicate_processing=sum(n-1 for n in visits.values()),
        injected_outage_attempts=dict(outage_counts), durable_restart=restarted,
        real_rest_errors=[e for e in rest_errors if 'INJECTED_DATA_GAP' not in e],
        recovered_outage_events={name:sum(ft.millis(active)+lo*1000 <= r['T'] < ft.millis(active)+hi*1000 for r in processed.values())
                                for name,lo,hi in [('25s_timeout',45,70),('45s_disconnect',135,180)]},
        final_state=state, source_sha256=source_hashes, server_clock_offset_ms=offset)
    write_json(output / 'summary.json', report)
    print('FINAL_SUMMARY ' + json.dumps(report), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--seconds', type=int, default=300)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    destination = args.output or Path('backups') / ('live-binance-audit-' + datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S'))
    print('ARTIFACT_DIRECTORY', destination.resolve(), flush=True)
    asyncio.run(run(args.seconds, destination.resolve()))
