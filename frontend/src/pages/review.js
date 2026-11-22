import {feedback, field} from '../components/common/fields.js';
import {empty, panel, pill} from '../components/common/panels.js';
import {table} from '../components/common/table.js';
import {REVIEW} from '../constants/enums.js';
import {handler} from '../hooks/action.js';
import {session} from '../stores/session.js';
import {workspace} from '../stores/workspace.js';
import {button, el, link, p} from '../utils/dom.js';

function reviewForm(item, spec, refresh) {
  const message = feedback();
  const submit = button(spec.action || '提交复核决定', {class:'primary', type:'submit', 'data-testid':'review-submit'});
  const action = field({key:'decision', label:spec.decision, type:spec.modes?.length ? 'select' : 'string',
    options:spec.modes, required:false, max_length:120});
  return el('form', {class:'fields', onSubmit:handler(submit, message, async event => {
    const data = new FormData(event.target);
    await workspace.review(item.id, {review:data.get('review'), decision:data.get('decision'), note:data.get('note')});
    await refresh();
  })}, el('label', {class:'field', for:'review-state'}, el('span',{},'复核结论'),
    el('select', {name:'review', id:'review-state'}, el('option',{value:'accepted'},'通过'), el('option',{value:'returned'},'退回'))),
    action, el('label',{class:'field',for:'review-note'},el('span',{},'复核说明'),
      el('textarea',{id:'review-note',name:'note',required:true,maxlength:500,rows:3})), submit, message);
}

export async function reviewPage(route, refresh) {
  const {spec, runs} = workspace;
  const completed = runs.filter(run => run.status === 'complete');
  const id = route.id || completed[0]?.id;
  if (!id) return panel(spec.pages[4], empty('先完成一次运行，再复核结果。','#run',spec.pages[2]));
  const job = await workspace.getRun(id);
  if (!job.result) return panel(job.name, empty('当前运行没有可复核结果。',`#results/${job.id}`,'查看运行状态'));
  const patrol = await workspace.getPatrol(id);
  const patrolBlock = !patrol ? null : panel('巡查排班', p(patrol.plan.headline, 'intro-copy'),
    patrol.plan.state === 'conflict'
      ? table([{key:'group',label:'班组'},{key:'message',label:'冲突说明'}], patrol.plan.conflicts, {caption:'已定巡查冲突'})
      : table([
          {label:'班组', render:row=>row.group},
          {label:'班次', render:row=>`${row.shift_start}–${row.shift_end}`},
          {label:'覆盖窗口', render:row=>row.covered},
          {label:'覆盖预计次数', render:row=>row.predicted.toFixed(2)},
        ], patrol.plan.routes.filter(row=>row.covered), {caption:'各组覆盖收益'}),
    link('打开巡查排班详情', `#results/${id}`, {class:'quiet'}));
  return el('div', {class:'two-col'},
    panel(job.name, p(job.result.headline,'intro-copy'), p(job.result.note),
      link('查看计算证据',`#results/${id}`,{class:'quiet'}),
      pill(REVIEW[job.review],job.review),
      job.review_note && el('div',{class:'decision'},p(job.decision),p(job.review_note)),
      job.review === 'pending' && (session.canReview ? reviewForm(job,spec,refresh) : p('等待业务负责人复核。','subtle'))),
    el('div',{class:'sidebar-panel'}, patrolBlock, panel('可复核记录', el('ul',{class:'method-list'}, completed.map(run =>
      el('li',{}, link(run.name,`#review/${run.id}`), ' ', pill(REVIEW[run.review],run.review)))))));
}
