import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { SourceContext } from './components/SourceContext';

const load = async () => ({title:'Report',before:'',selected:'',after:'',page_start:null,page_end:null});
describe('ambiguous PDF citation', () => {
  it('shows a same-page hint while retaining all candidate locations', () => {
    const html = renderToStaticMarkup(createElement(SourceContext, {sourceId:'s', en:false, load,
      location:{status:'ambiguous',matches:[{character_start:10,character_end:20,page:1},{character_start:30,character_end:40,page:2}],same_page_candidate_index:1}}));
    expect(html).toContain('候选位置 2 与另一条引文位于同页附近');
    expect(html).toContain('不能证明位置正确或支持结论');
    expect(html).toContain('查看候选位置 1');
    expect(html).toContain('查看候选位置 2');
  });
  it('ignores malformed hint indexes', () => {
    const html = renderToStaticMarkup(createElement(SourceContext, {sourceId:'s', en:true, load,
      location:{status:'ambiguous',matches:[{character_start:10,character_end:20}],same_page_candidate_index:7}}));
    expect(html).not.toContain('is near another citation');
    expect(html).toContain('View location 1');
  });
});
