/* Public admin setup contract: secrets remain write-only and are cleared. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const source=fs.readFileSync('static/app.js','utf8');
const begin=source.indexOf('async function workbuddyApplicationSettings()');
const end=source.indexOf('function resetLink(user)',begin);
assert(begin>0 && end>begin);
const body=source.slice(begin,end);
assert(body.includes('me?.role !== "admin"'));
assert(body.includes('type="password"'));
assert(body.includes('https://124.222.173.36:8888/api/workbuddy/callback'));
assert(body.includes('user.localassistant.readable') && body.includes('user.localassistant.invokable'));
assert(!body.includes('user.task.invokable'));
assert(body.includes('"/api/admin/workbuddy-app","POST",data'));
assert(body.includes('finally { data.client_secret="";form.elements.client_secret.value="";'));
assert(!body.includes('status.client_secret') && !body.includes('status.client_id'));
console.log('WorkBuddy administrator setup UI contract passed');
