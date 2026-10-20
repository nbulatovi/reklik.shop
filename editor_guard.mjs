// Trusted guard: stored outside the AI workspace and mounted read-only.
import fs from 'node:fs';
import path from 'node:path';
const ROOT = '/site';
let event;
try { event = JSON.parse(fs.readFileSync(0,'utf8')); } catch { process.exit(2); }
const name = String(event.tool_name || '').split('.').pop();
let input = event.tool_input || {};
if (typeof input === 'string') { try { input = JSON.parse(input); } catch { input = {command:input}; } }
fs.appendFileSync('/ai-home/guard-log.jsonl',JSON.stringify({name,at:Date.now()})+'\n');
function deny(reason) { process.stdout.write(JSON.stringify({hookSpecificOutput:{hookEventName:'PreToolUse',permissionDecision:'deny',permissionDecisionReason:reason}})); process.stderr.write(reason); process.exit(2); }
function allowedPath(value,write=false) {
  const absolute = path.resolve(ROOT,String(value).replace(/^['"]|['"]$/g,''));
  if (absolute !== ROOT && !absolute.startsWith(ROOT+'/')) return false;
  const relative = path.relative(ROOT,absolute);
  if (relative.split('/').some(p=>p.startsWith('.'))) return false;
  let part = ROOT;
  for (const segment of relative.split('/').filter(Boolean)) {part=path.join(part,segment);if(fs.existsSync(part)&&fs.lstatSync(part).isSymbolicLink()) return false;}
  if (write && !/^(?:templates\/.+\.(?:html|txt)|assets\/.+\.(?:css|js|svg|json|txt)|(?:app|store|payments|addresses)\.py)$/.test(relative)) return false;
  return true;
}
const shellNames = new Set(['Bash','shell','unified_exec','exec_command']);
if (shellNames.has(name)) {
  let command = input.command || input.cmd || '';
  if(Array.isArray(command)) command=command.join(' ');
  if(!command || /[;&|><`$(){}\n\r]/.test(command)) deny('Only simple, read-only commands inside /site are available.');
  const words = command.match(/"[^"]*"|'[^']*'|\S+/g) || [];
  const executable = words.shift();
  const cmd = path.basename(executable || '');
  if(!['cat','head','tail','sed','rg','ls','wc','nl','pwd','stat','file'].includes(cmd) || ![cmd,'/usr/bin/'+cmd,'/bin/'+cmd].includes(executable)) deny('That command is not available in this editor.');
  if(words.some(w=>/^--(?:pre|config|hostname|sort-files|no-config)=?/.test(w))) deny('External command flags are not available.');
  if(cmd==='sed' && words.some(w=>!['-n','--'].includes(w)&&w.startsWith('-'))) deny('Only sed -n can be used for reading.');
  if(cmd==='sed') {
    const operands=words.filter(w=>!w.startsWith('-')).map(w=>w.replace(/^['"]|['"]$/g,''));
    if(!/^\d+(?:,\d+)?p$/.test(operands[0]||'') || operands.slice(1).some(w=>!allowedPath(w))) deny('Only numeric line ranges from website files can be read.');
  }
  const flags = new Set(['-n','-i','-l','-F','-S','--files','--hidden','--color=never','-a','-h','-c','--']);
  for(const word of words) {
    if(word.startsWith('-')) {if(!flags.has(word)&&!/^-[0-9]+$/.test(word)) deny('Unsupported reader flag.');continue;}
    const bare=word.replace(/^['"]|['"]$/g,'');
    if(cmd==='sed'&&/^\d+(?:,\d+)?p$/.test(bare)) continue;
    if(bare.startsWith('/')||bare.includes('../')||bare.startsWith('..')) {if(!allowedPath(bare))deny('The editor can only read the website source.');}
  }
  if(input.workdir && !allowedPath(input.workdir)) deny('Working directory must remain in /site.');
  process.exit(0);
}
if(name==='apply_patch') {
  const patch = input.command || input.patch || input.input || '';
  const paths = [...String(patch).matchAll(/^\*\*\* (?:Add File|Update File|Delete File|Move to):\s*(.+)$/gm)].map(m=>m[1]);
  if(!paths.length || paths.some(p=>!allowedPath(p,true))) deny('Only store source, page templates and assets can be edited.');
  process.exit(0);
}
if(['read_file','view_image'].includes(name)) {
  if(!allowedPath(input.path||input.file_path||'')) deny('The editor can only read website files.');
  process.exit(0);
}
if(name==='update_plan') process.exit(0);
deny('This tool is not available in the store editor.');
