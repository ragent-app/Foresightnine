import { Button, Select } from './Controls';
import { useLanguage } from '../i18n';
import { reachedStudyStage, type StudyEntry } from '../study-library';
import type { Stage } from '../types';
interface Props {
 readonly entries:readonly StudyEntry[]; readonly busy:boolean; readonly onOpen:(id:string)=>void;
 readonly onSave:()=>void; readonly onNew:()=>void; readonly notice:string; readonly error:boolean;
}
const stages:Record<Stage,readonly [string,string]>={thesis:['观点草稿','Draft'],challenge:['反方审查','Review'],worldline:['世界线','Worldlines'],fm:['因子计算','Factors'],portfolio:['配置方案','Allocation'],nav:['组合路径','Paths'],console:['控制台','Console']};
export function StudySwitcher({entries,busy,onOpen,onSave,onNew,notice,error}:Props){
 const {language}=useLanguage();const en=language==='en';
 return <section className="study-switcher" aria-label={en?'Research library':'研究记录'}>
   <div className="study-switcher-controls"><Select value="" aria-label={en?'Open saved research':'打开已存研究'} disabled={busy||entries.length===0} onChange={e=>{if(e.target.value)onOpen(e.target.value);}}>
     <option value="">{en?'Saved research':'已存研究'} · {entries.length}</option>
     {entries.map(e=><option key={e.id} value={e.id}>{e.workspace.state.viewpoint.trim().slice(0,52)||(en?'Untitled draft':'未命名草稿')} · {`${en ? "Reached " : "已达"}${stages[reachedStudyStage(e.workspace.state)][en?1:0]}`} · {new Date(e.workspace.savedAt).toLocaleDateString(en?'en-US':'zh-CN')}</option>)}
   </Select><Button variant="quiet" disabled={busy} onClick={onSave}>{en?'Save research':'保存研究'}</Button><Button variant="secondary" disabled={busy} onClick={onNew}>{en?'New research':'新建研究'}</Button></div>
   {notice?<p role={error?'alert':'status'}>{notice}</p>:null}
 </section>;
}
