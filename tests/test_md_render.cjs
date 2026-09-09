const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const context = {window:{}};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../web/shared/md.js'),'utf8'),context);
const MD = context.window.MD;
let count = 0;
function test(name, run) { run(); count++; console.log('PASS',name); }
const diagram = '+-----------------------+\n| HOW THE 90° TURN WORKS |\n+-----------------------+\n|   Turn Key / Handle   |\n|          ▼            |\n| Cam Rotates           |\n+-----------------------+';
test('legacy ASCII diagram keeps spacing without paragraphs',()=>{
 const html=MD.render(diagram);assert.ok(html.startsWith('<pre'));assert.ok(html.includes('          ▼'));assert.ok(!html.includes('<p>'));
});
test('fenced diagram with empty lines and HTML is escaped',()=>{
 const html=MD.render('```text\n'+diagram+'\n\n<script>alert(1)</script>\n```');
 assert.equal((html.match(/<pre/g)||[]).length,1);assert.ok(!html.includes('<script>'));assert.ok(!html.includes('```'));assert.ok(html.includes('\n\n&lt;script&gt;'));
});
test('tilde fences and shorter inner fence',()=>{
 const html=MD.render('~~~~text\n~~~\nx\n~~~~\n\n# End');assert.ok(html.includes('~~~\nx'));assert.ok(html.includes('<h1>End</h1>'));
});
test('unfinished streaming code remains code',()=>{
 assert.ok(MD.render('```\n  a\n\n  b').includes('  a\n\n  b'));
});
test('stream chunks never settle inside a fence',()=>{
 const text='Intro\n\n```text\nA\n\nB\n```\n\nEnd';
 for(let i=0;i<=text.length;i++){
   const blocks=MD.splitBlocks(text.slice(0,i));
   for(const block of blocks.slice(0,-1)){
     if(block.includes('```text')) assert.ok(block.endsWith('```'));
   }
 }
 assert.equal(MD.splitBlocks(text).length,3);
});
test('headings tables lists links and image placeholders survive',()=>{
 const html=MD.render('# Title\n\n1. First\n2. Second\n\n- Bullet\n\nA | B\n--- | ---\nx | y\n\n[Site](https://example.com)\n\n[IMAGE: product photo]');
 for(const tag of ['<h1>','<ol>','<ul>','<table>','<a ','class="md-img"']) assert.ok(html.includes(tag),tag);
});
test('fence language is never injected as HTML',()=>{
 const html=MD.render('```<img src=x onerror=alert(1)>\nx\n```');assert.ok(!html.includes('<img'));
});
console.log(count+' tests passed');
