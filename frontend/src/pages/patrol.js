import {plot} from '../components/common/chart.js';
import {feedback} from '../components/common/fields.js';
import {metrics, panel, pill} from '../components/common/panels.js';
import {table} from '../components/common/table.js';
import {handler} from '../hooks/action.js';
import {session} from '../stores/session.js';
import {workspace} from '../stores/workspace.js';
import {button, el, p} from '../utils/dom.js';
import {date} from '../utils/format.js';

const SECTIONS = [
  {key:'crosswalk', title:'路口对照表', headers:['signal_name', 'road', 'intersection'],
    hint:'把信号台信号点对到台账的道路与路口。'},
  {key:'signal', title:'信号台进场窗口', headers:['signal_name', 'date', 'shift', 'start', 'end'],
    hint:'每个信号点、日期、时段的许可进场起止（HH:MM）。'},
  {key:'groups', title:'巡查组班次', headers:['name', 'start', 'shift_start', 'shift_end', 'transfer_minutes'],
    hint:'各班组的班次起止、起点信号点与从上一站转场所需分钟。'},
  {key:'fixed', title:'已定巡查', headers:['group', 'signal_name', 'date', 'shift'],
    hint:'已经商定、必须保留的守窗安排；冲突时会停排。'},
];

function parseCsv(text) {
  const rows = [];
  let row = [];
  let cell = '';
  let quoted = false;
  const text2 = text.replace(/^﻿/, '');
  for (let i = 0; i < text2.length; i += 1) {
    const ch = text2[i];
    if (quoted) {
      if (ch === '"') {
        if (text2[i + 1] === '"') { cell += '"'; i += 1; }
        else quoted = false;
      } else cell += ch;
    } else if (ch === '"') quoted = true;
    else if (ch === ',') { row.push(cell); cell = ''; }
    else if (ch === '\n' || ch === '\r') {
      if (ch === '\r' && text2[i + 1] === '\n') i += 1;
      row.push(cell); cell = '';
      if (row.some(value => value.trim() !== '')) rows.push(row);
      row = [];
    } else cell += ch;
  }
  row.push(cell);
  if (row.some(value => value.trim() !== '')) rows.push(row);
  return rows;
}

function records(text, headers) {
  const rows = parseCsv(text);
  if (!rows.length) return [];
  const header = rows[0].map(value => value.trim());
  const missing = headers.filter(key => !header.includes(key));
  const extra = header.filter(key => key && !headers.includes(key));
  if (missing.length || extra.length || new Set(header).size !== header.length) {
    const detail = [missing.length && `缺少列 ${missing.join('、')}`, extra.length && `未知列 ${extra.join('、')}`]
      .filter(Boolean).join('；') || '表头有重复列';
    throw new Error(`${detail}，请按表头顺序填写`);
  }
  return rows.slice(1).map((values, index) => {
    if (values.length !== header.length) throw new Error(`第 ${index + 2} 行列数与表头不一致`);
    return Object.fromEntries(header.map((key, i) => [key, values[i].trim()]));
  });
}

function toCsv(rows, headers) {
  const escape = value => /[",\n\r]/.test(String(value ?? '')) ? `"${String(value ?? '').replaceAll('"', '""')}"` : String(value ?? '');
  return [headers.join(','), ...(rows || []).map(row => headers.map(key => escape(row[key])).join(','))].join('\n');
}

function routeView(route) {
  const label = route.schedulable === false ? `${route.group}（未参与排班）` : route.group;
  return panel(`${label} · ${route.shift_start}–${route.shift_end}`,
    p(`起点 ${route.start} · 转场 ${route.transfer_minutes} 分钟 · 覆盖 ${route.covered} 窗 · 预计 ${route.predicted.toFixed(2)} 次`, 'subtle'),
    table([
      {label:'日期', render:row=>row.date},
      {label:'时段', render:row=>row.shift},
      {label:'窗口', render:row=>`${row.start}–${row.end}`},
      {label:'路口', render:row=>`${row.road}·${row.intersection}`},
      {label:'信号点', key:'signal_name'},
      {label:'预计阻断次数', render:row=>row.predicted.toFixed(2)},
      {label:'来源', render:row=>pill(row.source, row.source === '已定巡查' ? 'accepted' : 'queued')},
    ], route.days.flatMap(day => day.windows), {caption:`${route.group}巡查路线`, pageSize:8}));
}

function conflictView(plan) {
  return panel('已定巡查冲突',
    p('已定巡查与班次、起点或转场时间冲突，排班已停止，未输出任何路线。请先处理下列冲突。', 'feedback'),
    table([
      {key:'group', label:'班组'},
      {key:'code', label:'冲突类型'},
      {key:'message', label:'冲突说明'},
    ], plan.conflicts, {caption:'已定巡查冲突', pageSize:20}));
}

function warningView(plan) {
  if (!plan.warnings?.length) return null;
  return panel('排班数据提示',
    p('下列记录未参与排班，其余窗口仍按留出期预测排定。', 'subtle'),
    table([
      {key:'code', label:'提示类型'},
      {key:'message', label:'说明'},
    ], plan.warnings, {caption:'排班数据提示', pageSize:12}));
}

function planView(plan) {
  const covered = plan.routes.filter(route=>route.covered);
  const routes = covered.length
    ? el('div', {class:'patrol-routes'}, covered.map(routeView))
    : p('没有班组能守住完整窗口。', 'empty');
  const coverage = plan.tables.find(item => item.title === '窗口覆盖与收益');
  const summary = plan.tables.find(item => item.title === '班组覆盖汇总');
  return el('div', {'data-testid':'patrol-plan'},
    plan.state === 'conflict'
      ? conflictView(plan)
      : el('div', {}, p(plan.headline, 'intro-copy'), p(plan.note, 'subtle'), metrics(plan.metrics),
        (plan.charts || []).map(plot), panel('各组巡查路线', routes),
        summary && panel(summary.title, table(summary.columns, summary.rows)),
        coverage && panel(coverage.title, table(coverage.columns, coverage.rows, {pageSize:16})),
        plan.sections.map(item => panel(item.title, p(item.text)))),
    warningView(plan));
}

export async function patrolPanel(job, saved, refresh) {
  const message = feedback();
  const editable = session.canWrite;
  const submit = button('按留出期排班', {type:'submit', class:'primary', 'data-testid':'patrol-submit'});
  const load = button('载入示例底册', {class:'quiet', type:'button'});
  const textareas = {};
  for (const section of SECTIONS) {
    textareas[section.key] = el('textarea', {id:`patrol-${section.key}`, rows:4, spellcheck:'false'},
      toCsv(saved?.inputs?.[section.key], section.headers));
  }
  const copy = () => {
    const data = {};
    for (const section of SECTIONS) data[section.key] = records(textareas[section.key].value, section.headers);
    return data;
  };
  load.addEventListener('click', handler(load, message, async () => {
    const sample = await workspace.patrolSample(job.id);
    for (const section of SECTIONS) textareas[section.key].value = toCsv(sample[section.key], section.headers);
  }));
  const form = editable && el('form', {class:'fields patrol-form', onSubmit:handler(submit, message, async () => {
    await workspace.schedulePatrol(job.id, copy());
    await refresh();
  })}, el('div', {class:'patrol-grid'}, SECTIONS.map(section => el('label', {class:'field', for:textareas[section.key].id},
    el('span', {}, section.title), el('small', {}, section.hint), textareas[section.key]))),
    el('div', {class:'actions'}, submit, load), message);
  return panel('巡查排班',
    p('读入信号台进场窗口与巡查组班次，先用路口对照表对齐台账路口，再按留出期逐窗预测排班。', 'subtle'),
    saved ? planView(saved.plan) : p('尚未排班。提交跨系统底册后在这里核对各组路线和覆盖收益。', 'empty'),
    saved ? p(`最近排班：${saved.author} · ${date(saved.created_at)}`, 'subtle') : null,
    editable ? form : p('只读角色可核对已保存的排班。', 'subtle'));
}
