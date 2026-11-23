import json
from pathlib import Path
# Each group has one source and one question. The first candidate is supported;
# the second has a changed conclusion. These are fictional, independently authored exhibits.
groups = [
('rights','Can holders transfer Oriole notes?','Oriole notes may be transferred without consent.','Oriole notes may be transferred without consent.','Oriole notes may not be transferred without consent.'),
('rights','Can Bracken borrowers prepay without a fee?','Bracken borrowers may prepay without a fee.','Bracken borrowers may prepay without a fee.','Bracken borrowers must pay a prepayment fee.'),
('rights','Who may redeem Cedar units?','Only institutional accounts may redeem Cedar units.','Only institutional accounts may redeem Cedar units.','All retail accounts may redeem Cedar units.'),
('policy','When does the Willow rule take effect?','The Willow rule was announced on 2025-01-02 and takes effect on 2025-07-01.','The Willow rule takes effect on 2025-07-01.','The Willow rule takes effect on 2025-01-02.'),
('policy','Does the Pine rule cover existing contracts?','The Pine rule covers new contracts only; existing contracts are exempt.','The Pine rule does not cover existing contracts.','The Pine rule covers existing contracts.'),
('timing','When does the Maple deposit mature?','The Maple deposit matures on 2027-06-30.','The Maple deposit matures on 2027-06-30.','The Maple deposit matures on 2026-06-30.'),
('currency','In what currency is the Larch payment?','The Larch payment is denominated in EUR, not USD.','The Larch payment is denominated in EUR.','The Larch payment is denominated in USD.'),
('network','Which network supports the Rowan token?','The Rowan token is issued only on Network Azure, not Network Amber.','The Rowan token is issued on Network Azure.','The Rowan token is issued on Network Amber.'),
('measurement','What was Birch closing price?','Birch closing price was 12 dollars; its market capitalization was not disclosed.','Birch closing price was 12 dollars.','Birch market capitalization was 12 dollars.'),
('measurement','What was Hazel transaction volume?','Hazel transaction volume was 90 units; its transaction value was not disclosed.','Hazel transaction volume was 90 units.','Hazel transaction value was 90 dollars.'),
('causation','Did fuel cause Aspen profit decline?','Aspen profit fell while fuel costs rose; the cause was not established.','Aspen profit fell while fuel costs rose; causation was not established.','Fuel caused the Aspen profit decline.'),
('conditional','Is the Elm fee waived?','The Elm fee is waived only for accounts with balances above 500 units.','The Elm fee is waived for accounts with balances above 500 units.','The Elm fee is waived for every account.'),
('personal','Can the Spruce fund guarantee a payment in one year?','The Spruce fund has no principal guarantee and its value can fall.','The Spruce fund cannot guarantee principal in one year.','The Spruce fund guarantees the payment in one year.'),
('definition','What is Fir duration?','Fir duration is a measure of bond price sensitivity to interest rate changes.','Fir duration measures bond price sensitivity to interest rate changes.','Fir duration guarantees a bond will mature within one year.'),
('chinese','榆树合同允许提前赎回吗？','榆树合同不允许提前赎回。','榆树合同不允许提前赎回。','榆树合同允许提前赎回。'),
]
cases=[]
for i,(kind,q,source,positive,negative) in enumerate(groups,1):
    for suffix,claim,expected in [('a',positive,True),('b',negative,False)]:
        cases.append(dict(id=f'g{i:02d}{suffix}',group=f'g{i:02d}',kind=kind,question=q,source=source,claim=claim,expected=expected))
Path('/tmp/foresight-quality-private/cases.json').write_text(json.dumps(cases,ensure_ascii=False,indent=2)+'\n')
