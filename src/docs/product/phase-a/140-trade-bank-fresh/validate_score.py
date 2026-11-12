from common import get_task
from score import evaluate

def candidate(source,claim,quote):
 a=source.text.index(quote)
 return {'text':claim,'citations':[{'source_id':source.source_id,'quotation':quote,'location':{'character_start':a,'character_end':a+len(quote)}}]}
_,sources=get_task('TRADE_FEB');s=sources[0]
a=candidate(s,'Deficit 68.9, prior 67.6 billion.','goods and services deficit was $68.9 billion in February, up $1.3 billion from $67.6 billion in January')
b=candidate(s,'Exports 263.0 and imports 331.9 billion.','February exports were $263.0 billion, $5.8 billion more than January exports. February imports were $331.9 billion')
c=candidate(s,'Goods deficit fell while services surplus fell.','a decrease in the goods deficit of $0.3 billion to $91.4 billion and a decrease in the services surplus of $1.6 billion')
out={'claims':[a,b],'counterarguments':[c]}
good=evaluate('TRADE_FEB',out,s)
assert all(good['signals'].values()) and good['all_claims_located']
out['claims'][1]['citations'][0]['location']['character_start']+=1
bad=evaluate('TRADE_FEB',out,s)
assert not bad['all_claims_located'] and not bad['signals']['four_values']
print('score fixture checks passed; numeric role and semantic entailment are not tested')
