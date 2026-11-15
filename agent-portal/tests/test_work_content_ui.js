const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(require('node:path').join(__dirname, '../static/app.js'),'utf8');
const context={workAge:()=> '6 分钟前',esc:x=>String(x).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;')};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function workContent('),source.indexOf('function workTask(')),context);
const goal='任务目标'.repeat(120)+'\nSHA256: '+'a'.repeat(64);
const html=context.workContent({goal,phase:{title:'阶段'.repeat(80)},last_step:{summary:'步骤'.repeat(500),next:'下一步',blocker:'步骤阻塞'},blocker:{detail:'详细诊断'}});
for(const value of [goal,'阶段'.repeat(80),'步骤'.repeat(500),'下一步','步骤阻塞','详细诊断','不是完整执行日志']) assert.ok(html.includes(value),value.slice(0,30));
assert.ok(context.workContent({goal:'<script>alert(1)</script>'}).includes('&lt;script&gt;'));
assert.ok(!context.workContent({goal:'<script>alert(1)</script>'}).includes('<script>'));
assert.ok(context.workContent({}).includes('尚无已同步'));
assert.ok(source.includes('d.className}`'));
console.log('work content UI: 11 assertions passed');

const full=context.workContent({goal:"legacy",content:{schema:1,fields:{goal:"完整正文"},observed_at:1,stale:true,truncated:["goal"]}});
for(const value of ["完整正文","较旧","已截断","16384"]) assert.ok(full.includes(value));
assert.ok(!full.includes("legacy"));

const history=context.workContent({content:{schema:1,fields:{goal:"旧正文"},older_than_status:true}});
assert.ok(history.includes("历史 · 任务目标")); assert.ok(history.includes("当前状态以卡片概览为准"));
console.log("work content extended UI: 7 additional assertions passed");

assert.ok(!context.workContent({reason:"new reason",content:{schema:1,fields:{goal:"old goal"},older_than_status:true}}).includes("new reason"));
console.log("work content provenance: 1 assertion passed");
