"""Exercise the legacy backtest loop with synthetic prices, not real trades.

Load the unchanged class via AST; stub logging and summary metrics only. The
trade/risk loop is untouched. The comparison adds one synthetic HOLD event,
which has no buy/sell action but causes the original loop to check risk.
"""
import ast
import datetime
import hashlib
import json
from pathlib import Path

SOURCE = Path('/home/lxx/Openclaw-stock/openclaw/skills/backtesting/enhanced_backtest.py')
OUTPUT = Path(__file__).with_name('old-backtest-cadence-probe-2026-11-11.json')


class NullLogger:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


def main():
    raw = SOURCE.read_bytes()
    tree = ast.parse(raw)
    found = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'EnhancedBacktest']
    if len(found) != 1:
        raise ValueError('expected exactly one EnhancedBacktest')
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), found[0]], type_ignores=[])
    namespace = {'logger':NullLogger(), 'datetime':datetime.datetime}
    exec(compile(ast.fix_missing_locations(module), str(SOURCE), 'exec'), namespace)
    engine_cls = namespace['EnhancedBacktest']
    # The summary method requires numpy. This stub affects only the returned
    # summary, which is explicitly not reported as an investment metric.
    engine_cls._calculate_metrics = lambda self: {'total_return':0, 'win_rate':0}
    candles = [{'timestamp':'2024-01-01T10:00:00','close':100.0},
               {'timestamp':'2024-01-01T10:30:00','close':80.0},
               {'timestamp':'2024-01-01T11:00:00','close':100.0}]
    buy = {'symbol':'SYNTHETIC','action':'BUY','price':100.0,'timestamp':'2024-01-01T10:00:00'}
    hold = {'symbol':'SYNTHETIC','action':'HOLD','price':80.0,'timestamp':'2024-01-01T10:30:00'}

    def run(signals):
        engine = engine_cls(initial_capital=1000, slippage_pct=0, commission_pct=0)
        engine.run_backtest({'SYNTHETIC':candles}, signals, max_position_size=0.2)
        return {'closed_trades':[{'reason':t['exit_reason'],'time':t['exit_time'],'price':t['exit_price']} for t in engine.closed_trades],
                'alerts':[{'type':a['type'],'time':a['timestamp']} for a in engine.alerts_triggered]}

    record = {'checked_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'source':str(SOURCE),'source_sha256':hashlib.sha256(raw).hexdigest(),
              'method':'Original run_backtest trade/risk loop via AST; summary metrics and logging stubbed; synthetic data only',
              'candles':candles,'buy_signal':buy,'added_nontrading_event':hold,
              'parameters':{'initial_capital':1000,'slippage_pct':0,'commission_pct':0,'max_position_size':0.2},
              'without_hold_event':run([buy]),'with_hold_event':run([buy,hold]),
              'limitations':['The HOLD event is a test stimulus, not a trading recommendation or proof production receives HOLD events.',
                             'Summary metrics were stubbed and are not evidence; no historical performance conclusion.',
                             'Synthetic timestamps do not establish any vendor bar-time semantics.']}
    with OUTPUT.open('x') as stream:
        json.dump(record, stream, indent=2)
        stream.write('\n')
    print(json.dumps(record, indent=2))


if __name__ == '__main__':
    main()
