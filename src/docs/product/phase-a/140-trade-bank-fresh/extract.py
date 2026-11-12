from html.parser import HTMLParser
from pathlib import Path
from app.research_uploads import _extract_text

HERE=Path(__file__).resolve().parent
class ArticleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True);self.depth=0;self.skip=0;self.items=[]
    def handle_starttag(self,tag,attrs):
        if tag=='article':self.depth+=1
        if self.depth:
            if tag in {'script','style','svg'}:self.skip+=1
            if not self.skip and tag in {'p','br','div','tr','li','h1','h2','h3','h4','table'}:self.items.append('\n')
            if not self.skip and tag in {'td','th'}:self.items.append(' | ')
    def handle_endtag(self,tag):
        if self.depth and tag in {'script','style','svg'} and self.skip:self.skip-=1
        if self.depth and not self.skip and tag in {'p','div','tr','li','h1','h2','h3','h4','table'}:self.items.append('\n')
        if tag=='article' and self.depth:self.depth-=1
    def handle_data(self,data):
        if self.depth and not self.skip:self.items.append(data)
parser=ArticleText();parser.feed((HERE/'bea-trade.html').read_text())
text=''.join(parser.items)
assert '$68.9 billion' in text and '$263.0 billion' in text and '$331.9 billion' in text
(HERE/'bea-trade-source.txt').write_text(text)
pdf=(HERE/'fed-stress.pdf').read_bytes()
assert len(pdf)<=3_000_000
(HERE/'fed-stress-source.txt').write_text(_extract_text(pdf,text_order='reading'))
