"""Compare original risk path on synthetic bars with and without a later bar.

The unchanged old class is loaded by AST with a no-op logger; no external
packages, real data, orders, or source-repository writes are involved.
"""
import ast
import datetime
import hashlib
import json
from pathlib import Path

SOURCE = Path('/home/lxx/Openclaw-stock/openclaw/skills/backtesting/enhanced_backtest.py')
OUTPUT = Path(__file__).with_name('old-backtest-risk-probe-2026-11-11.json')


class NullLogger:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


def main():
    raw = SOURCE.read_bytes()
    tree = ast.parse(raw)
    cls = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'EnhancedBacktest']
    if len(cls) != 1:
        raise ValueError('expected exactly one class')
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), cls[0]], type_ignores=[])
    namespace = {'logger':NullLogger(), 'datetime':datetime.datetime}
    exec(compile(ast.fix_missing_locations(module), str(SOURCE), 'exec'), namespace)
    engine_cls = namespace['EnhancedBacktest']
    signal = {'symbol':'SYNTHETIC', 'action':'BUY', 'price':100.0, 'timestamp':'2024-01-01T10:00:30'}
    first = {'timestamp':'2024-01-01T10:00:00', 'close':100.0}
    later = {'timestamp':'2024-01-01T10:01:00', 'close':80.0}

    def run(bars):
        engine = engine_cls(initial_capital=1000, slippage_pct=0, commission_pct=0)
        engine._process_signal(signal, {'SYNTHETIC':bars}, max_position_size=0.2)
        return {'position_open': 'SYNTHETIC' in engine.positions,
                'closed_trades':[{'exit_reason':t['exit_reason'],'exit_time':t['exit_time'],'exit_price':t['exit_price']} for t in engine.closed_trades],
                'alerts':[{'type':a['type'],'timestamp':a['timestamp']} for a in engine.alerts_triggered]}

    without_future, with_future = run([first]), run([first, later])
    record = {'checked_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'source':str(SOURCE),'source_sha256':hashlib.sha256(raw).hexdigest(),
              'method':'Original class via AST; same synthetic signal and parameters; only later candle inclusion changes',
              'signal':signal,'bars_before_signal':[first],'added_later_bar':later,
              'parameters':{'initial_capital':1000,'slippage_pct':0,'commission_pct':0,'max_position_size':0.2},
              'without_later_bar':without_future,'with_later_bar':with_future,
              'limitations':['Synthetic single signal and direct _process_signal call, not full historical performance.',
                             'Original class loaded with a no-op logger; external dependencies and full metric path not exercised.',
                             'Source-specific bar timestamp and publication semantics need separate verification.']}
    with OUTPUT.open('x') as stream:
        json.dump(record, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps(record, indent=2))


if __name__ == '__main__':
    main()
