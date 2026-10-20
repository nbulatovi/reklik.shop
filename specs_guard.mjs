// Specification research can use web search, but cannot read local secrets or edit files.
import fs from 'node:fs';
let event;try{event=JSON.parse(fs.readFileSync(0,'utf8'));}catch{process.exit(2);}
const name=String(event.tool_name||'');
if(/(?:^|[._])web(?:[._]|$)|web_search/.test(name)||name==='update_plan')process.exit(0);
process.stdout.write(JSON.stringify({hookSpecificOutput:{hookEventName:'PreToolUse',permissionDecision:'deny',permissionDecisionReason:'Only web research is available. No local file or shell access.'}}));
process.exit(2);
