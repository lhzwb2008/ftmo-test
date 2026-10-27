import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
import pandas as pd

import prob_aqua_vs_ftmo as m

m.load_days(pd.Timestamp('2020-01-02').date())
rows = []
stop = m.AQUA_SIZE * m.AQUA_DAILY_LOSS * (1 - m.AQUA_DAILY_BUFFER)
for i in range(len(m.DAYS)):
    capped, *_ = m.run_day(i, m.AQUA_SIZE, 2.0, m.AQUA_SIZE, stop, m.AQUA_DAY_CAP, m.AQUA_HARD_SL)
    free, *_ = m.run_day(i, m.AQUA_SIZE, 2.0, m.AQUA_SIZE, stop, 0.0, m.AQUA_HARD_SL)
    raw, *_ = m.run_day(i, m.AQUA_SIZE, 2.0, m.AQUA_SIZE, 0.0, 0.0, 0.0)
    rows.append((m.DAYS[i][0], capped, free, raw))
df = pd.DataFrame(rows, columns=['date', 'capped', 'no_cap', 'raw'])
df.to_csv(os.path.join(os.path.dirname(__file__), 'asym_daily.csv'), index=False)
for col in ['capped', 'no_cap', 'raw']:
    s = df[col]
    win, loss = s[s > 0], s[s < 0]
    print(f'{col:7s} 天数{len(s)} 均值/天 {s.mean():8.1f}  胜率 {len(win) / len(s):.1%}  '
          f'均盈 {win.mean():7.1f}  均亏 {loss.mean():7.1f}  最大盈 {s.max():8.1f}  最大亏 {s.min():8.1f}  '
          f'>=1000天 {(s >= 1000).sum()}  总和 {s.sum():10.0f}')
print('被帽削掉的利润合计', (df['no_cap'] - df['capped']).sum().round(0))
print('触帽天数', (df['capped'] >= 1099).sum(), ' 日盈>=1000 天数比例', ((df['capped'] >= 1000).mean()).round(3))
