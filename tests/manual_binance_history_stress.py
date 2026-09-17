"""Opt-in mature Binance history replay through the actual five-page recovery cap."""
import asyncio
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from app.core import futures_targets as ft
from app.core.binance_client import get_futures_aggregate_trades


async def main(output):
    end = int(datetime.now(timezone.utc).timestamp()*1000)-2000
    start = end-2*3600000
    reference = []
    params = dict(symbol='BTCUSDT', startTime=start, endTime=start+1800000, limit=1000)
    async with httpx.AsyncClient(timeout=15) as client:
        for _ in range(7):
            response = await client.get('https://fapi.binance.com/fapi/v1/aggTrades', params=params)
            response.raise_for_status()
            rows = response.json()
            if not rows: break
            reference.extend(rows)
            params = dict(symbol='BTCUSDT', fromId=rows[-1]['a']+1, limit=1000)
            await asyncio.sleep(.25)
        if not reference: raise RuntimeError('No baseline data')
        end = reference[-1]['T']
        response = await client.get('https://fapi.binance.com/fapi/v1/aggTrades', params=params)
        response.raise_for_status()
        reference.extend(r for r in response.json() if r['T'] <= end)
    start = reference[0]['T']-1
    order = SimpleNamespace(id=1, status='PENDING', symbol='BTC-USDT-FUTURES',
                            price=1e12, qty=1., side='SELL')
    event = SimpleNamespace(payload_json=json.dumps(dict(order_id=1, symbol='BTCUSDT',
        active_ms=start, checked_ms=start, last_id=None, last_trade_ms=None, last_poll_ms=0,
        limit_price=1e12, qty=1., side='SELL')), message='')
    received, cycles, fetched = {}, [], []
    async def observed(symbol, **kwargs):
        rows = await get_futures_aggregate_trades(symbol, **kwargs)
        fetched.extend(rows)
        return rows
    now = datetime.now(timezone.utc)
    until = datetime.fromtimestamp(end/1000, timezone.utc)
    with patch.object(ft, 'get_futures_aggregate_trades', observed):
        for cycle in range(20):
            fetched.clear()
            hit = await ft.replay_target(order,event,now+timedelta(seconds=cycle*6),until=until)
            assert hit is None
            state=json.loads(event.payload_json)
            for row in fetched:
                if start < row['T'] <= end and row['a'] <= state['last_id']:
                    received[row['a']]=row
            cycles.append(dict(cycle=cycle, processed=len(received), last_id=state['last_id'],
                checked_ms=state['checked_ms'], complete=ft.replay_complete(event,until)))
            if ft.replay_complete(event,until): break
    baseline={r['a']:r for r in reference}
    result=dict(reference_records=len(baseline), replayed_records=len(received),
        first_id=reference[0]['a'], last_id=reference[-1]['a'], cycles=cycles,
        missing=sorted(baseline.keys()-received.keys()), extra=sorted(received.keys()-baseline.keys()),
        field_mismatches=[a for a in baseline.keys() & received.keys() if any(baseline[a][k]!=received[a][k] for k in ('p','q','T','f','l','m'))],
        note='Mature historical data; replay clock advances 6 seconds per cycle to exercise the production throttle without sleeping.')
    output.mkdir(parents=True,exist_ok=True)
    (output/'historical_stress.json').write_text(json.dumps(result,indent=2))
    (output/'historical_reference.json').write_text(json.dumps(reference))
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    asyncio.run(main(Path(sys.argv[1])))
