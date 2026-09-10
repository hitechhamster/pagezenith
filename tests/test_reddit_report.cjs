const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
class Element {
  constructor(tag, cls, text) {
    this.tag = tag; this.className = cls || ''; this.text = text == null ? '' : String(text);
    this.children = []; this.style = {}; this.classList = {add: () => {}};
  }
  append(...items) { this.children.push(...items); }
  setAttribute(name, value) { this[name] = value; }
  set innerHTML(value) { assert.equal(value, '', 'Model content must never be interpreted as HTML'); this.children = []; }
}
const out = new Element('div');
const context = {URL, Set, Map, E: (...args) => new Element(...args), $: () => out,
  document: {createTextNode: text => new Element('#text', '', text)},
  renderSteps: () => {}, metric: (value, label) => new Element('div', '', value + label)};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/shared/reddit-report.js'), 'utf8'), context);
const all = node => [node, ...node.children.flatMap(all)];
const report = {
  sections: [{title: 'Long chapter', introduction: 'Retained long analysis', findings: []}],
  threads: [{url: 'https://www.reddit.com/r/test/comments/one', subreddit: 'test', title: 'Source'},
    {url: 'javascript:alert(1)', subreddit: 'bad', title: 'Unsafe'}],
  market_tables: [{key: 'audiences', title: 'Who and when', columns: {who: 'Who', when: 'When'}, rows: [{cells: {
    who: {basis: 'sample', text: '<img src=x onerror=alert(1)>', source_ids: ['T001', 'T002'],
      quote_evidence: [{text: 'Actual source quote', source_url: 'https://reddit.com/r/test/comments/one'},
        {text: 'Unsafe quote', source_url: 'https://reddit.com.evil.example/'}]},
    when: {basis: 'unknown', text: '样本未提及'}}}]},
    {key: 'purchase', title: 'Evidence gap', columns: {stage: 'Stage'}, rows: [], evidence_gap: 'Missing purchase data'}]
};
context.renderDeepResearch(report);
let nodes = all(out);
assert.equal(nodes.filter(n => n.tag === 'table').length, 1);
assert.equal(nodes.filter(n => n.tag === 'tr').length, 2);
assert.equal(nodes.filter(n => n.scope === 'col').length, 2);
assert.equal(nodes.filter(n => n.scope === 'row').length, 1);
assert.ok(nodes.some(n => n.text === 'Retained long analysis'));
assert.ok(nodes.some(n => n.text === 'Missing purchase data'));
assert.ok(nodes.some(n => n.text === '<img src=x onerror=alert(1)>'));
assert.ok(!nodes.some(n => n.tag === 'img'));
assert.ok(!nodes.some(n => n.href?.includes('javascript:') || n.href?.includes('evil.example')));
assert.ok(nodes.some(n => n.tag === 'details'));
assert.ok(nodes.some(n => n.tabIndex === 0 && n.role === 'region'));
context.renderDeepResearch({...report, market_tables: []});
nodes = all(out);
assert.equal(nodes.filter(n => n.tag === 'table').length, 0);
assert.ok(nodes.some(n => n.text === 'Retained long analysis'));
console.log('Reddit report rendering: table structure, retained prose, old reports, safe links and escaping OK');
