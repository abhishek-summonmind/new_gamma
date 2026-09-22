import os
import sys
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

os.environ['NIFTY_SECURITY_ID'] = '21690'
os.environ['NIFTY_INDEX_SYMBOL'] = 'DIXON'
os.environ['NSE_EQUITY_EXCHANGE_SEGMENT'] = 'NSE_EQ'
os.environ['NSE_EQUITY_INSTRUMENT'] = 'EQUITY'

from app.config import get_settings
from app.services.dhan_client import DhanClient

settings = get_settings()
print('effective_nifty_security_id=', settings.dhan.nifty_security_id)
print('effective_equity_exchange_segment=', settings.dhan.equity_exchange_segment)
print('effective_equity_instrument=', settings.dhan.equity_instrument)
print('effective_nifty_index_symbol=', settings.nifty_index_symbol)
print('effective_market_timezone=', settings.market_timezone)

now_market = datetime.now(ZoneInfo(settings.market_timezone))
fetch_from = now_market - timedelta(days=max(1, settings.intraday_fetch_days))
payload = {
    'securityId': str(settings.dhan.nifty_security_id),
    'exchangeSegment': settings.dhan.equity_exchange_segment,
    'instrument': settings.dhan.equity_instrument,
    'interval': '1',
    'oi': False,
    'fromDate': fetch_from.strftime('%Y-%m-%d %H:%M:%S'),
    'toDate': now_market.strftime('%Y-%m-%d %H:%M:%S'),
}
print('payload=', payload)
client = DhanClient(settings)
try:
    response = client._service.post_json('/charts/intraday', payload)
    print('response_keys=', list(response.keys()) if isinstance(response, dict) else type(response))
    data = response.get('data') if isinstance(response, dict) else None
    print('data type=', type(data))
    if isinstance(data, dict):
        print('data keys=', list(data.keys()))
        counts = {k: len(data.get(k) or []) for k in ['open', 'high', 'low', 'close', 'volume', 'timestamp']}
        print('counts=', counts)
        ts = data.get('timestamp') or []
        closes = data.get('close') or []
        print('first_ts=', ts[0] if ts else None)
        print('last_ts=', ts[-1] if ts else None)
        print('latest_close=', closes[-1] if closes else None)
    else:
        print('no data object')
except Exception as exc:
    import traceback
    traceback.print_exc()
