'use strict';
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let project = null, groupSelection = new Set(), questionSelection = new Set(), previewURL = null, previewID = null, pollTimer = null, previewPage = 0, previewPages = 1;
let editingGroups = false, jumpTarget = null, modelReady = null, apiPending = null, autoDraft = null;
const reviewDrafts = new Map();
const scopedGroups = () => project.groups.filter(g=>project.scope.group_ids.includes(g.id));
const scopedQuestions = () => (project.rubric?.questions||[]).filter(q=>project.scope.question_ids.includes(q.id));
function setJump(target){jumpTarget=target;$('jump-review').hidden=!target;if(target){const group=project?.groups.find(g=>g.id===target.group_id);$('jump-review').textContent=`前往 ${group?.name||'未确认小组'} · ${target.question_id}`;}}
function reportError(error){message(error.message,true);setJump(error.target);}
function checkScope(){if(JSON.stringify([...groupSelection].sort())!==JSON.stringify([...project.scope.group_ids].sort())||JSON.stringify([...questionSelection].sort())!==JSON.stringify([...project.scope.question_ids].sort())||$('scope-questions').value.split(/[,，、\s]+/).filter(Boolean).join(',')!==project.scope.question_ids.join(','))throw new Error('范围有未保存修改，请先点击更新小组选择或更新题目选择。');}

const statusNames = {pending:'尚未下载', ready:'材料已齐', missing:'缺交 / 无法读取', unsupported:'需补充文件', incomplete:'材料待补齐', conflict:'提交存在冲突'};

function message(value, error=false) { setJump(null); $('message-text').textContent=value; $('message').className=error?'error':''; $('message').hidden=!value; }
async function api(path, body, options={}) {
  const init={...options,headers:{'X-Grader-Local':'1',...(options.headers||{})}};
  if (body !== undefined) {
    init.method=init.method||'POST';
    if(body instanceof FormData) init.body=body;
    else {init.headers['Content-Type']='application/json';init.body=JSON.stringify(body);}
  }
  const response=await fetch('/api/'+path,init);
  if(!response.ok){let error;try{error=await response.json();}catch{error={detail:'请求失败，请检查本机服务是否运行。'};}const failure=new Error(error.detail||'操作失败。');failure.target=error.target;throw failure;}
  return response;
}
async function json(path,body){return (await api(path,body)).json();}
function saveBlob(blob,name){const link=document.createElement('a');const url=URL.createObjectURL(blob);link.href=url;link.download=name;link.click();setTimeout(()=>URL.revokeObjectURL(url),60000);}
async function download(path,body){const r=await api(path,body);const name=(r.headers.get('Content-Disposition')||'').match(/filename="([^"]+)"/)?.[1]||'download';const local=r.headers.get('X-Local-Folder')||r.headers.get('X-Local-File');if(local){$('local-output').hidden=false;$('local-output-path').value=decodeURIComponent(local);}saveBlob(await r.blob(),name);}
function path(suffix=''){if(!project)throw new Error('请先打开项目。');return `projects/${project.id}${suffix}`;}
function selectedGroups(){return [...groupSelection];}
function selectedQuestions(){return [...questionSelection];}
function checked(container){return [...$(container).querySelectorAll('input:checked')].map(x=>x.value);}
function options(items,placeholder){return (placeholder?`<option value="">${esc(placeholder)}</option>`:'')+items.map(i=>`<option value="${esc(i.id)}">${esc(i.name)}</option>`).join('');}
function bind(id,event,fn){$(id).addEventListener(event,async e=>{if(event==='submit')e.preventDefault();const button=event==='submit'?e.target.querySelector('button[type=submit],button:not([type])'):e.currentTarget;try{if(button.tagName==='BUTTON')button.disabled=true;await fn(e);}catch(error){reportError(error);}finally{if(button.tagName==='BUTTON'&&id!=='next-group')button.disabled=false;}});}

async function projectList(){const previousTrash=$('trash-list').value;const list=await json('projects');const trash=await json('trash');$('trash-list').innerHTML=options(trash,'选择已删除项目');renderProjectChecks('project-checks',list);renderProjectChecks('trash-checks',trash);if([...$('trash-list').options].some(o=>o.value===previousTrash))$('trash-list').value=previousTrash;if(project&&!list.some(p=>p.id===project.id)){project=null;clearTimeout(pollTimer);$('workspace').hidden=true;$('local-output').hidden=true;}}
function connection(data){$('identity').textContent=data.identity.name;$('course').innerHTML=options(data.courses,'请选择课程');$('token').value='';}
async function loadProject(id){project=await json(`projects/${id}`);autoDraft=null;reviewDrafts.clear();$('grading-mode').value='file';$('api-review-request').hidden=true;apiPending=null;$('model-address').value=project.api_settings?.address||'';$('model-name').value=project.api_settings?.model||'';groupSelection=new Set(project.scope.group_ids);questionSelection=new Set(project.scope.question_ids);editingGroups=!groupSelection.size;$('scope-questions').value=project.scope.question_ids.join(',');previewID=null;$('instructions').value=project.instructions||'';renderRubric(scopedQuestions());render();await projectList();}
function render(){
  $('workspace').hidden=false;
  $('project-title').textContent=project.name;$('project-name').value=project.name;
  $('project-meta').textContent=`满分 ${project.maximum} · ${scopedGroups().length} 个已选小组 · 本机项目 ${project.id.slice(0,8)}`;
  let confirmed=0;scopedGroups().forEach(g=>scopedQuestions().forEach(q=>{if(project.results[g.id]?.[q.id]?.confirmed)confirmed++;}));
  $('progress').textContent=`已确认 ${confirmed} / ${scopedGroups().length*project.scope.question_ids.length} 题`;
  $('description').textContent=project.description||'未读取到题目文字，可手动补充 PDF。';
  $('material-issues').textContent=project.material_issues.join('\n');
  $('download-status').textContent=(project.busy?'下载中 · ':'')+project.message;
  $('download').disabled=project.busy||Boolean(project.source.demo);
  const fileButtons=files=>files.map(f=>`<button type="button" class="secondary small" data-file="${esc(f.id)}">${esc(f.name)}</button>`).join(' ');
  $('materials').innerHTML=fileButtons(project.files.filter(f=>f.role!=='submission'&&f.active!==false));
  $('groups').innerHTML=(editingGroups?project.groups:scopedGroups()).map(g=>`<tr><td><input type="checkbox" aria-label="选择 ${esc(g.name)}" data-group="${esc(g.id)}" ${groupSelection.has(g.id)?'checked':''}></td><td>${esc(g.name)}</td><td>${esc(g.submission.attempt??'—')}</td><td>${esc(statusNames[g.status]||g.status)}<div class="notice">${esc(g.issue)}</div></td><td><div class="file-links">${fileButtons(project.files.filter(f=>f.group_id===g.id&&f.active!==false))}</div>${g.status!=='ready'?`<button type="button" class="secondary small" data-ready="${esc(g.id)}">确认材料已齐</button>`:''}</td></tr>`).join('');
  const prevFileGroup=$('file-group').value,prevReview=$('review-group').value;
  $('file-group').innerHTML=options(scopedGroups());$('file-group').value=scopedGroups().some(g=>g.id===prevFileGroup)?prevFileGroup:scopedGroups()[0]?.id||'';
  $('review-group').innerHTML=options(scopedGroups());$('review-group').value=scopedGroups().some(g=>g.id===prevReview)?prevReview:scopedGroups()[0]?.id||'';
  $('rubric-version').textContent=project.rubric?`当前版本 v${project.rubric.version}`:'尚未确认';
  const mergeSelected=new Set(checked('merge-checks'));
  $('task-scope').textContent=`已保存范围：${project.scope.group_ids.length} 个小组 · ${project.scope.question_ids.join('、')||'未选题目'}`;
  $('question-checks').innerHTML=project.scope.question_ids.map(qid=>`<label><input type="checkbox" value="${esc(qid)}" ${questionSelection.has(qid)?'checked':''}>${esc(qid)}</label>`).join('');
  $('merge-checks').innerHTML=scopedQuestions().map(q=>`<label><input type="checkbox" value="${esc(q.id)}" ${mergeSelected.has(q.id)?'checked':''}>${esc(q.id)}</label>`).join('');
  $('tasks').innerHTML=[...project.tasks].filter(t=>!t.retired).reverse().map(t=>`<div class="task"><div><strong>任务 ${t.version}</strong> · 标准 v${t.rubric_version} · ${t.group_ids.length} 组 · ${esc(t.question_ids.join('、'))}<div class="muted">已导入 ${t.imported_pairs.length} / ${t.group_ids.length*t.question_ids.length} 条结果 · ${esc(new Date(t.created).toLocaleString())}</div></div><button class="secondary small" data-task="${esc(t.id)}">下载任务包</button></div>`).join('');
  $('sample-results').hidden=!project.source.demo;
  renderReview();renderApi();
  clearTimeout(pollTimer);
  if(project.busy||(project.api_jobs||[]).some(j=>['queued','running','stopping'].includes(j.status))){pollTimer=setTimeout(async()=>{try{project=await json(path());render();}catch(e){message(e.message,true);}},1800);}
}

function rubricValues(){return [...$('rubric-rows').children].map(row=>({id:row.querySelector('[data-qid]').value.trim(),points:row.querySelector('[data-points]').value,criteria:row.querySelector('[data-criteria]').value}));}
function renderRubric(rows){$('rubric-rows').innerHTML='';for(const row of rows)addRubricRow(row);if(!rows.length)addRubricRow({id:'E1',points:'',criteria:''});rubricTotal();}
function addRubricRow(q={id:'',points:'',criteria:''}){const row=document.createElement('div');row.className='rubric-row';row.innerHTML=`<label>题号<input data-qid value="${esc(q.id)}" maxlength="60"></label><label>满分<input data-points type="number" min="0" step="0.0001" value="${esc(q.points)}"></label><label>评分细则<textarea data-criteria rows="3">${esc(q.criteria)}</textarea></label><button type="button" class="secondary small" data-remove>移除</button>`;$('rubric-rows').append(row);}
function rubricTotal(){const sum=rubricValues().reduce((s,q)=>s+(Number(q.points)||0),0);$('rubric-total').textContent=`已分配 ${Math.round(sum*10000)/10000} / ${project?.maximum||'—'} 分`;}

function renderReview(){
 const gid=$('review-group').value,oldFile=$('review-file').value;
 const groups=scopedGroups(),index=groups.findIndex(g=>g.id===gid);$('next-group').disabled=index<0||index>=groups.length-1;$('next-group').textContent=index===groups.length-1?'已到最后一组':'下一组';$('group-position').textContent=groups.length?`${index+1} / ${groups.length} 组`:'';
 const files=project.files.filter(f=>f.group_id===gid&&f.active!==false);
 $('review-file').innerHTML=options(files,'选择原件');$('review-file').value=files.some(f=>f.id===oldFile)?oldFile:files[0]?.id||'';
 $('review-questions').innerHTML=scopedQuestions().map(q=>{
  const c=project.results[gid]?.[q.id]||{candidates:[],confirmed:null};
  const picked=reviewDrafts.get(JSON.stringify([gid,q.id]))||c.candidates.find(x=>x.id===c.confirmed)||c.candidates.at(-1);
  return `<article class="review-card ${c.confirmed?'confirmed':'pending'}" data-review-q="${esc(q.id)}"><div class="review-title"><h3>${esc(q.id)} / ${esc(q.points)} 分</h3><span class="badge">${c.confirmed?'已确认':c.candidates.length?'待复核':'未批阅'}</span></div><details><summary>评分细则</summary><p>${esc(q.criteria)}</p></details><details ${c.candidates.length>1&&!c.confirmed?'open':''}><summary>${c.candidates.length} 条候选 / 历史记录${c.candidates.length>1&&!c.confirmed?'，请核对冲突':''}</summary>${[...c.candidates].reverse().map(v=>`<div class="candidate">${esc(v.source.kind)} · 建议扣分 ${esc(v.deduction??'未确定')}<br>${esc(v.reason_zh||'暂无中文说明，请补填')}<br>学生评语：${esc(formatFeedback(v.deduction,v.reason,q.id)||'零扣分，留空')}<br>依据：${esc(v.evidence)}<br>疑点：${esc(v.doubts||'无')}<br><button type="button" class="secondary small" data-candidate="${esc(v.id)}">采用此候选进行复核</button></div>`).join('')}</details><input type="hidden" data-picked value="${esc(picked?.id||'')}"><label>确认扣分（零扣分也需明确填写）<input data-score type="number" min="0" max="${esc(q.points)}" step="0.0001" value="${esc(picked?.deduction??'')}" placeholder="未批阅"></label><label>中文扣分说明（仅供教师查看）<textarea data-reason-zh rows="3">${esc(picked?.reason_zh||'')}</textarea></label><label>给学生的扣分评语（与上传表一致）<textarea maxlength="1035" data-reason rows="2">${esc(picked?.upload_comment??formatFeedback(picked?.deduction,picked?.reason,q.id))}</textarea></label><p class="hint">填写格式：-扣分, 题号(小问): 英文扣分原因。多处扣分用分号列出小问、各自扣分和原因。原因最多 100 个英文词、1000 个字符。零扣分留空。</p><label>页码 / 判断依据<textarea data-evidence rows="2">${esc(picked?.evidence||'')}</textarea></label><p data-doubts class="notice">${esc(picked?.doubts||'')}</p><button type="button" data-confirm>保存修改并确认本题</button></article>`;
 }).join('')||'<p class="muted">请先确认评分标准。</p>';
 if($('review-file').value){showFile($('review-file').value).catch(e=>message(e.message,true));}
 else{if(previewURL)URL.revokeObjectURL(previewURL);previewID=null;previewURL=null;$('page-controls').hidden=true;$('image-preview').hidden=true;$('download-original').hidden=true;$('preview-label').textContent='该组尚无原文件';}
}
async function showFile(fid){
 if(previewID===fid)return;
 const f=project.files.find(f=>f.id===fid);if(!f)return;
 previewID=fid;previewPage=0;previewPages=1;$('preview-label').textContent=f.name;$('image-preview').hidden=true;$('download-original').hidden=false;
 try{if(f.ext==='.pdf'){const info=await json(path(`/files/${fid}/info`));if(previewID!==fid)return;previewPages=info.pages;}await renderPage();}catch(error){$('page-controls').hidden=true;throw error;}
}
async function renderPage(){const fid=previewID;if(!fid)return;const f=project.files.find(f=>f.id===fid);const page=previewPage;const r=await api(path(f.ext==='.pdf'?`/files/${fid}/pages/${page}`:`/files/${fid}`));const blob=await r.blob();if(previewID!==fid||previewPage!==page)return;if(previewURL)URL.revokeObjectURL(previewURL);previewURL=URL.createObjectURL(blob);$('image-preview').src=previewURL;$('image-preview').hidden=false;$('page-controls').hidden=f.ext!=='.pdf';$('page-number').textContent=`${page+1} / ${previewPages}`;$('previous-page').disabled=page===0;$('next-page').disabled=page>=previewPages-1;}

bind('connect-form','submit',async()=>{message('正在连接 Canvas…');connection(await json('connect',{base_url:$('base-url').value,token:$('token').value}));message('已连接。请选择课程和作业。');});
bind('disconnect','click',async()=>{await json('disconnect',{});$('identity').textContent='已断开';$('course').innerHTML=options([],'先连接 Canvas');$('assignment').innerHTML=options([],'先选择课程');message('已断开 Canvas，本地项目仍可继续复核。');});
bind('course','change',async()=>{if(!$('course').value)return;$('assignment').innerHTML=options(await json(`courses/${$('course').value}/assignments`),'请选择作业');});
bind('create-project','click',async()=>{if(!$('course').value||!$('assignment').value)throw new Error('请选择课程和作业。');message('正在读取小组和提交清单…');const p=await json('projects',{course_id:Number($('course').value),assignment_id:Number($('assignment').value)});await loadProject(p.id);message('项目已创建。先选择少量小组下载材料。');});

bind('demo','click',async()=>{const p=await json('demo',{});await loadProject(p.id);groupSelection=new Set(p.groups.map(g=>g.id));render();message('匿名演示已准备好。可下载任务包交给 Agent，或下载演示结果，再在第 04 步导入。');});
bind('select-all','click',async()=>{groupSelection=new Set(project.groups.map(g=>g.id));editingGroups=true;render();});
bind('select-none','click',async()=>{groupSelection.clear();editingGroups=true;render();});
$('groups').addEventListener('change',e=>{if(e.target.dataset.group){if(e.target.checked)groupSelection.add(e.target.dataset.group);else groupSelection.delete(e.target.dataset.group);}});
bind('groups','click',async e=>{if(e.target.dataset.ready){project=await json(path(`/groups/${e.target.dataset.ready}/ready`),{});render();message('已确认本组材料齐全。');}});
document.addEventListener('click',async e=>{if(e.target.dataset.file){try{await showFile(e.target.dataset.file);$('preview-label').scrollIntoView({behavior:'smooth',block:'center'});}catch(error){reportError(error);}}});
bind('download','click',async()=>{message('正在核对当前提交并开始下载…');await json(path('/download'),{group_ids:selectedGroups()});project=await json(path());render();message('下载已开始，每个文件完成后会保存进度。');});
bind('file-form','submit',async()=>{const files=[...$('material-file').files];if(!files.length)throw new Error('请选择文件。');for(const f of files){const form=new FormData();form.append('file',f);form.append('role',$('file-role').value);form.append('group_id',$('file-group').value);project=await json(path('/files'),form);}render();$('material-file').value='';message('文件已补充；小组作业需核对后点击“确认材料已齐”。');});
bind('rubric-packet','click',async()=>{await saveQuestionScope();await download(path('/rubric-packet'));message('准备包已下载。解压后请 Agent 阅读 START_HERE.md 并生成 rubric.json。');});
bind('rubric-file','change',async()=>{const f=$('rubric-file').files[0];if(!f)return;const body=JSON.parse(await f.text());if(!Array.isArray(body.questions))throw new Error('文件缺少 questions。');if(body.project_id!==project.id){const s=body.source||{};if(s.base_url!==project.source.base_url||String(s.course_id)!==String(project.source.course_id)||String(s.assignment_id)!==String(project.source.assignment_id))throw new Error('评分标准所属项目不匹配。同事共享标准请使用本工具的“导出标准”功能。');}renderRubric(body.questions.filter(q=>project.scope.question_ids.includes(q.id)));message('草案已填入。请检查题号、分值及细则，再点击保存并确认。');$('rubric-file').value='';});
bind('rubric-export','click',async()=>{await download(path('/rubric-file'));message('评分标准已导出并保存在本机，可将此文件交给同事。');});
bind('add-question','click',async()=>{const ids=new Set(rubricValues().map(q=>q.id));let n=1;while(ids.has(`E${n}`))n++;addRubricRow({id:`E${n}`,points:'',criteria:''});syncRubricIds();});
function syncRubricIds(){$('scope-questions').value=rubricValues().map(q=>q.id).filter(Boolean).join(',');questionSelection=new Set(rubricValues().map(q=>q.id).filter(Boolean));}
$('rubric-rows').addEventListener('click',e=>{if(e.target.hasAttribute('data-remove')){e.target.closest('.rubric-row').remove();syncRubricIds();rubricTotal();}});
$('rubric-rows').addEventListener('input',e=>{if(e.target.hasAttribute('data-qid'))syncRubricIds();rubricTotal();});
bind('save-rubric','click',async()=>{await saveQuestionScope(false);project=await json(path('/rubric'),{project_id:project.id,questions:rubricValues()});reviewDrafts.clear();questionSelection=new Set(project.scope.question_ids);renderRubric(scopedQuestions());render();message('已选题目的评分标准已确认，可以生成批阅任务。');});
$('question-checks').addEventListener('change',()=>{questionSelection=new Set(checked('question-checks'));$('scope-questions').value=selectedQuestions().join(',');});
bind('create-task','click',async()=>{checkScope();if(JSON.stringify(rubricValues())!==JSON.stringify(scopedQuestions()))throw new Error('页面上的评分标准有未保存修改，请先保存并确认。');const t=await json(path('/tasks'),{group_ids:selectedGroups(),question_ids:selectedQuestions(),instructions:$('instructions').value});project=await json(path());render();await download(path(`/tasks/${t.id}/packet`));message('任务包已生成，本机副本已解压。将页面上方的输出文件夹交给 Agent，完成后导入 results.json。');});
bind('tasks','click',async e=>{if(e.target.dataset.task)await download(path(`/tasks/${e.target.dataset.task}/packet`));});
bind('sample-results','click',async()=>download(path('/demo-results')));
bind('result-form','submit',async()=>{const form=new FormData();form.append('file',$('result-file').files[0]);const r=await json(path('/results'),form);project=r.project;render();$('result-file').value='';message(`已导入 ${r.count} 条初评。请核对原文件后逐题确认。`);});
bind('merge-form','submit',async()=>{const form=new FormData();form.append('file',$('merge-file').files[0]);form.append('completed_questions',JSON.stringify(checked('merge-checks')));const r=await json(path('/merge'),form);project=r.project;render();message(`已合并 ${r.count} 条结果，已有结果保留为候选，冲突需人工选择。`);});
bind('review-group','change',async()=>renderReview());
bind('next-group','click',async()=>{const groups=scopedGroups();const index=groups.findIndex(g=>g.id===$('review-group').value);if(index>=0&&index<groups.length-1){$('review-group').value=groups[index+1].id;renderReview();$('review-group').scrollIntoView({behavior:'smooth',block:'center'});}});
bind('review-file','change',async()=>{if($('review-file').value)await showFile($('review-file').value);});
bind('previous-page','click',async()=>{if(previewPage>0){previewPage--;await renderPage();}});
bind('next-page','click',async()=>{if(previewPage<previewPages-1){previewPage++;await renderPage();}});
function stashDraft(card){reviewDrafts.set(JSON.stringify([$('review-group').value,card.dataset.reviewQ]),{id:card.querySelector('[data-picked]').value,deduction:card.querySelector('[data-score]').value,upload_comment:card.querySelector('[data-reason]').value,reason_zh:card.querySelector('[data-reason-zh]').value,evidence:card.querySelector('[data-evidence]').value,doubts:card.querySelector('[data-doubts]').textContent});}
$('review-questions').addEventListener('input',e=>{const card=e.target.closest('[data-review-q]');if(card){syncFeedback(card,e.target);stashDraft(card);}});
bind('review-questions','click',async e=>{
 const card=e.target.closest('[data-review-q]');if(!card)return;const qid=card.dataset.reviewQ,gid=$('review-group').value;
 if(e.target.dataset.candidate){const v=project.results[gid][qid].candidates.find(v=>v.id===e.target.dataset.candidate);card.querySelector('[data-picked]').value=v.id;card.querySelector('[data-score]').value=v.deduction??'';card.querySelector('[data-reason]').value=formatFeedback(v.deduction,v.reason,qid);card.querySelector('[data-reason-zh]').value=v.reason_zh||'';card.querySelector('[data-evidence]').value=v.evidence;card.querySelector('[data-doubts]').textContent=v.doubts;stashDraft(card);}
 if(e.target.hasAttribute('data-confirm')){const score=card.querySelector('[data-score]').value;if(score==='')throw new Error('请明确填写扣分，零扣分填写 0。');e.target.disabled=true;try{project=await json(path('/confirm'),{group_id:gid,question_id:qid,deduction:score,upload_comment:card.querySelector('[data-reason]').value,reason_zh:card.querySelector('[data-reason-zh]').value,evidence:card.querySelector('[data-evidence]').value,candidate_id:card.querySelector('[data-picked]').value||null});reviewDrafts.delete(JSON.stringify([gid,qid]));render();message(`${qid} 已确认。`);}finally{e.target.disabled=false;}}
});
bind('download-original','click',async()=>{const f=project.files.find(f=>f.id===previewID);if(f){const r=await api(path(`/files/${f.id}`));const local=r.headers.get('X-Local-File');if(local){$('local-output').hidden=false;$('local-output-path').value=decodeURIComponent(local);}saveBlob(await r.blob(),f.name);}});
for(const kind of ['review','xlsx','csv'])bind(`export-${kind}`,'click',async()=>{checkScope();await download(path('/export'),{kind,group_ids:selectedGroups(),question_ids:selectedQuestions()});message(kind==='review'?'部分批阅表已导出。可交给同事合并，请勿直接上传为总成绩。':'已选范围的英文扣分表已导出。分题批阅时请先合并其他题目的结果，再由上传器提交。');});
bind('export-audit','click',async()=>{await download(path('/export'),{kind:'audit'});message('全部批阅记录已导出，包含当前结果和历次归档。');});
(async()=>{try{await projectList();}catch(e){message(e.message,true);}try{connection(await json('session'));}catch{/* Offline review is available without a Canvas connection. */}})();

async function saveQuestionScope(rerender=true){
 const ids=$('scope-questions').value.split(/[,，、\s]+/).filter(Boolean);
 const rows=rubricValues();
 project=await json(path('/scope'),{question_ids:ids});questionSelection=new Set(project.scope.question_ids);
 $('scope-questions').value=project.scope.question_ids.join(',');
 if(rerender){renderRubric(project.scope.question_ids.map(id=>rows.find(q=>q.id===id)||scopedQuestions().find(q=>q.id===id)||{id,points:'',criteria:''}));render();}
}
bind('update-questions','click',async()=>{await saveQuestionScope();$('local-output').hidden=true;message('题目范围已更新，后续任务和复核只包含这些题。');});
bind('edit-groups','click',async()=>{editingGroups=!editingGroups;render();});
bind('update-groups','click',async()=>{project=await json(path('/scope'),{group_ids:selectedGroups()});groupSelection=new Set(project.scope.group_ids);editingGroups=false;reviewDrafts.clear();$('local-output').hidden=true;previewID=null;render();message('小组范围已更新；取消小组的本地材料和旧导出包已移出项目。');});
function checkedProjects(id){return [...$(id).querySelectorAll('input:checked')].map(e=>e.value);}
function renderProjectChecks(id,rows){const selected=new Set(checkedProjects(id));$(id).innerHTML=rows.map(p=>`<label ${id==='project-checks'?`class="project-row ${project?.id===p.id?'current-project':''}" tabindex="0" data-project-id="${esc(p.id)}"`:''}><input type="checkbox" value="${esc(p.id)}" ${selected.has(p.id)?'checked':''}> ${esc(p.name)}</label>`).join('')||'<p class="hint">暂无项目</p>';}
async function batchProjects(box,action){const ids=checkedProjects(box);if(!ids.length)throw new Error('请先勾选项目。');if(action==='purge'&&!window.confirm(`永久删除所选 ${ids.length} 个项目及其材料、记录和导出文件？此操作无法恢复。`))return;let done=0;try{for(const id of ids){await json(`projects/${id}/${action}`,action==='purge'?{confirmation:id}:{});done++;}}finally{await projectList();message(`已处理 ${done} / ${ids.length} 个项目。`);}}
bind('select-projects','click',async()=>{await projectList();$('project-checks').querySelectorAll('input').forEach(e=>e.checked=true);});
bind('delete-project','click',()=>batchProjects('project-checks','delete'));
bind('restore-project','click',()=>batchProjects('trash-checks','restore'));
bind('rename-project','click',async()=>{project=await json(path('/rename'),{name:$('project-name').value});render();await projectList();message('项目名称已保存。');});
bind('jump-review','click',async()=>{if(!jumpTarget)return;const target=jumpTarget;$('review-group').value=target.group_id;renderReview();const card=[...$('review-questions').children].find(c=>c.dataset.reviewQ===target.question_id);card?.classList.add('jump-target');card?.scrollIntoView({behavior:'smooth',block:'center'});card?.querySelector('[data-score]')?.focus({preventScroll:true});});
async function bulkConfirm(gids){checkScope();const overrides=[...reviewDrafts.entries()].map(([key,v])=>{const [group_id,question_id]=JSON.parse(key);return {...v,group_id,question_id,candidate_id:v.id||null};}).filter(v=>gids.includes(v.group_id)&&project.scope.question_ids.includes(v.question_id));const r=await json(path('/confirm-all'),{group_ids:gids,overrides});project=r.project;const skipped=new Set(r.skipped.map(v=>JSON.stringify([v.group_id,v.question_id])));for(const key of reviewDrafts.keys()){if(gids.includes(JSON.parse(key)[0])&&!skipped.has(key))reviewDrafts.delete(key);}render();message(`已确认 ${r.count} 题；${r.skipped.length} 题仍需逐题处理。${r.skipped[0]?.reason||''}`);setJump(r.skipped[0]||null);}
bind('confirm-group','click',async()=>bulkConfirm([$('review-group').value]));
bind('confirm-all','click',async()=>bulkConfirm(project.scope.group_ids));

function formatFeedback(score,reason,qid){
 if(score===null||score===undefined||score===''||Number(score)===0)return '';
 const raw=reason||'';const escaped=String(qid).replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
 const located=new RegExp('^'+escaped+'(?=$|[\\s:：.(\\[/-])','i').test(raw);
 return `-${score}, ${raw?(located?raw:`${qid}: ${raw}`):`${qid}: `}`;
}
function syncFeedback(card,target){
 const score=card.querySelector('[data-score]'),comment=card.querySelector('[data-reason]');
 const match=comment.value.trim().match(/^-(\d+(?:\.\d+)?|\.\d+)\s*,\s*([\s\S]*)$/);
 if(target===comment&&match)score.value=match[1];
 if(target===score){const reason=match?match[2]:comment.value.trim();comment.value=formatFeedback(score.value,reason,card.dataset.reviewQ);}
}

bind('confirm-zero-groups','click',async()=>{
 checkScope();
 const gids=project.scope.group_ids;
 const draft_group_ids=[...new Set([...reviewDrafts.keys()].map(key=>JSON.parse(key)).filter(([gid,qid])=>gids.includes(gid)&&project.scope.question_ids.includes(qid)).map(([gid])=>gid))];
 const r=await json(path('/confirm-zero-groups'),{group_ids:gids,draft_group_ids});
 project=r.project;render();
 message(`已通过 ${r.passed_groups.length} 个零扣分小组（新确认 ${r.count} 题）；${r.already_groups} 组此前已确认；${r.skipped.length} 组跳过。${r.skipped[0]?.reason||''}`);
 setJump(r.skipped[0]||null);
});

function renderApi(){
 if(autoDraft){const completed=project.api_jobs?.find(j=>j.id===autoDraft.id&&j.draft);if(completed){if(JSON.stringify(rubricValues())===autoDraft.previous){renderRubric(completed.draft);message('API 评分草案已填入，请检查并保存确认。');}else message('API 草案已生成，当前编辑内容已保留，可点击载入草案。');autoDraft=null;}}
 const apiMode=$('grading-mode').value==='api';
 document.querySelectorAll('[data-api-mode]').forEach(el=>el.hidden=!apiMode);
 document.querySelectorAll('[data-file-mode]').forEach(el=>el.hidden=apiMode);
 const active=(project?.api_jobs||[]).some(j=>['queued','running','stopping'].includes(j.status));
 $('api-start').disabled=active||project?.busy;$('api-rubric').disabled=active||project?.busy;
 const names={queued:'排队中',running:'批阅中',stopping:'停止中',stopped:'已停止',interrupted:'上次中断',failed:'有失败项',done:'已完成'};
 $('api-jobs').innerHTML=[...(project?.api_jobs||[])].reverse().map(j=>{
  const done=j.items.filter(i=>i.status==='done').length,failed=j.items.filter(i=>i.status==='failed').length;
  const running=['queued','running','stopping'].includes(j.status);
  return `<div class="task"><div><strong>${j.kind==='rubric'?'评分标准草案':'作业初评'}</strong> · ${esc(names[j.status]||j.status)}<p>完成 ${done} / ${j.items.length} · 失败 ${failed} · 待处理 ${j.items.length-done-failed}</p><p class="muted">${esc(j.settings.model)} · ${esc(j.settings.address)}</p>${j.items.filter(i=>i.error).map(i=>`<p class="notice">${esc(project.groups.find(g=>g.id===i.group_id)?.name||'评分标准')}：${esc(i.error)}</p>`).join('')}</div><div>${running?`<button data-api-stop="${esc(j.id)}" class="secondary">停止任务</button>`:done<j.items.length?`<button data-api-retry="${esc(j.id)}" class="secondary">重试未完成项</button>`:''}${j.draft?`<button data-api-draft="${esc(j.id)}">载入草案供确认</button>`:''}</div></div>`;
 }).join('');
}
bind('grading-mode','change',async()=>{apiPending=null;$('api-review-request').hidden=true;renderApi();});
bind('model-form','submit',async()=>{
 if(!project)throw new Error('请先打开项目。');
 const key=$('model-key').value;
 try{modelReady=await json('model-session',{project_id:project.id,address:$('model-address').value,model:$('model-name').value,key});}
 finally{$('model-key').value='';}
 $('model-status').textContent='正在测试合成图片…';
 try{const r=await json('model-session/test',{});$('model-status').textContent=r.message;message(r.message);}
 catch(e){$('model-status').textContent=e.message;modelReady=null;throw e;}
});
bind('model-disconnect','click',async()=>{await json('model-session/disconnect',{});modelReady=null;$('model-key').value='';$('model-status').textContent='会话密钥已清除；已启动任务请另行停止。';});
function prepareApi(kind,retryId=null){
 checkScope();if(!modelReady)throw new Error('请先连接并测试视觉模型。');
 if(!retryId&&kind==='grading'&&JSON.stringify(rubricValues())!==JSON.stringify(scopedQuestions()))throw new Error('请先保存并确认评分标准。');
 if(reviewDrafts.size)throw new Error('请先保存当前复核修改，再开始 API 任务。');
 const old=retryId?(project.api_jobs||[]).find(j=>j.id===retryId):null;
 const items=old?old.items.filter(i=>i.status!=='done'):null;
 const gids=old?items.map(i=>i.group_id).filter(Boolean):kind==='grading'?selectedGroups():[];
 const qids=old?[...new Set(items.flatMap(i=>i.question_ids))]:selectedQuestions();
 if(!qids.length||kind==='grading'&&!gids.length)throw new Error('请选择需要批阅的小组和题目。');
 const files=project.files.filter(f=>f.active!==false&&(f.role!=='submission'||gids.includes(f.group_id)));
 apiPending={kind,retryId,projectId:project.id,group_ids:gids,question_ids:qids,instructions:$('instructions').value,regrade:$('api-regrade').checked};
 $('api-request-summary').textContent=`服务：${modelReady.address}\n模型：${modelReady.model}\n题目：${qids.join('、')}\n小组：${gids.map(id=>project.groups.find(g=>g.id===id)?.name||id).join('、')||'不发送学生作业'}\n${kind==='grading'&&!old&&!apiPending.regrade?'已人工确认的题目将跳过；全组均已确认时不发送该组材料。\n':''}材料：\n${files.map(f=>`${f.name} (${Math.ceil(f.size/1024)} KB)`).join('\n')}\n还将发送题目说明、评分标准或已有 rubric，以及本次额外要求。`;
 $('api-review-request').hidden=false;$('api-review-request').scrollIntoView({behavior:'smooth',block:'center'});
}
bind('api-rubric','click',async()=>{await saveQuestionScope();prepareApi('rubric');});
bind('api-start','click',async()=>prepareApi('grading'));
bind('api-cancel','click',async()=>{apiPending=null;$('api-review-request').hidden=true;});
bind('api-send','click',async()=>{
 if(!apiPending||apiPending.projectId!==project.id)throw new Error('请重新核对发送范围。');
 checkScope();const request=apiPending;if(request.kind==='grading'&&!request.retryId&&(JSON.stringify(request.group_ids.slice().sort())!==JSON.stringify(selectedGroups().sort())||JSON.stringify(request.question_ids.slice().sort())!==JSON.stringify(selectedQuestions().sort())))throw new Error('选择范围已变化，请重新核对发送。');
 if(request.retryId)await json(path(`/api-jobs/${request.retryId}/retry`),{});
 else {const r=await json(path('/api-jobs'),request);if(request.kind==='rubric')autoDraft={id:r.job_id,previous:JSON.stringify(rubricValues())};}
 apiPending=null;$('api-review-request').hidden=true;project=await json(path());render();message('API 任务已启动，完成结果自动进入复核页。');
});
bind('api-jobs','click',async e=>{
 if(e.target.dataset.apiStop){project=await json(path(`/api-jobs/${e.target.dataset.apiStop}/stop`),{});render();message('已请求停止；当前请求结束后不会继续下一组。');}
 if(e.target.dataset.apiRetry){const j=project.api_jobs.find(j=>j.id===e.target.dataset.apiRetry);prepareApi(j.kind,j.id);}
 if(e.target.dataset.apiDraft){const j=project.api_jobs.find(j=>j.id===e.target.dataset.apiDraft);if(JSON.stringify(j.snapshot.scope)!==JSON.stringify(project.scope))throw new Error('范围已变化，请重新生成草案。');renderRubric(j.draft);$('rubric-rows').scrollIntoView({behavior:'smooth',block:'center'});message('评分草案已填入，请检查后保存并确认。');}
});
bind('refresh-projects','click',async()=>{await projectList();$('trash-checks').querySelectorAll('input').forEach(e=>e.checked=true);});
bind('purge-project','click',()=>batchProjects('trash-checks','purge'));
window.addEventListener('focus',()=>projectList().catch(e=>reportError(e)));


async function openProjectRow(id){if(reviewDrafts.size&&!window.confirm('当前复核修改尚未保存，切换项目将丢弃这些修改。仍要切换？'))return;await loadProject(id);message('项目已打开。');}
bind('project-checks','dblclick',async e=>{const row=e.target.closest('[data-project-id]');if(row)await openProjectRow(row.dataset.projectId);});
bind('project-checks','keydown',async e=>{const row=e.target.closest('[data-project-id]');if(!row)return;if(e.key==='Enter'){e.preventDefault();await openProjectRow(row.dataset.projectId);}if(e.key===' '&&e.target===row){e.preventDefault();const box=row.querySelector('input');box.checked=!box.checked;}});
