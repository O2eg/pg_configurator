/**
 * The stylesheet contract, enforced rather than promised.
 *
 * `pgc.css` is structural: it holds no colour, font or text size of its own,
 * exactly as `pgplan.css` does for pg-explain-viewer. And its spacing comes in
 * two families — layout on a 4px scale, control geometry copied verbatim from
 * the viewer. Both rules are easy to state in a comment and easy to break in a
 * hurry, so they are checked here.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const CSS_DIR = join(HERE, '..', 'css');
const structural = readFileSync(join(CSS_DIR, 'pgc.css'), 'utf8');
const theme = readFileSync(join(CSS_DIR, 'pgc-theme.css'), 'utf8');

/** Selectors whose padding is the viewer's control geometry, not layout. */
const INHERITED_GEOMETRY = [
  '.pc-title small',
  '.pc-link',
  '.pc-theme-toggle',
  '.pc-tab',
  '.pc-subtab',
  '.pc-output-tab',
  '.pc-btn',
  '.pc-hint code',
  '.pc-error',
  '.pc-tabrow',
  '.pc-table th',
  '.pc-table td',
  '.pc-apply',
  '.pc-empty',
  // one geometry shared by every text control on the page
  '.pc-field > input',
  '.pc-readout',
  '.pc-filter input',
  '.pc-list li',
  '.pc-tip',
];

const SPACING = /^\s*(margin|padding|gap|column-gap|row-gap)(-[a-z]+)?\s*:\s*([^;]+);/gm;

/** Split a stylesheet into { selector, body } blocks, ignoring at-rules. */
function rules(css) {
  const withoutComments = css.replace(/\/\*[\s\S]*?\*\//g, '');
  const blocks = [];
  const pattern = /([^{}]+)\{([^{}]*)\}/g;
  let match;
  while ((match = pattern.exec(withoutComments)) !== null) {
    const selector = match[1].trim().replace(/\s+/g, ' ');
    if (selector.startsWith('@')) continue;
    blocks.push({ selector, body: match[2] });
  }
  return blocks;
}

test('the structural sheet declares no colour of its own', () => {
  const offenders = [];
  for (const line of structural.replace(/\/\*[\s\S]*?\*\//g, '').split('\n')) {
    if (/#[0-9a-fA-F]{3,8}\b/.test(line)) offenders.push(line.trim());
    if (/\b(rgb|rgba|hsl|hsla)\(/.test(line)) offenders.push(line.trim());
  }
  assert.deepEqual(offenders, [], 'colour belongs in pgc-theme.css');
});

test('the structural sheet declares no font family or text size of its own', () => {
  const offenders = [];
  for (const { selector, body } of rules(structural)) {
    for (const line of body.split(';')) {
      if (/font-family\s*:/.test(line) && !line.includes('var(--pv-font-')) {
        offenders.push(`${selector}: ${line.trim()}`);
      }
      if (/font-size\s*:/.test(line) && !line.includes('var(--pv-fs')) {
        offenders.push(`${selector}: ${line.trim()}`);
      }
    }
  }
  // The page title and the switch label are the page's own chrome, sized the
  // way the viewer sizes them; everything else must use the type scale.
  const allowed = new Set(['.pc-title: font-size: 20px', '.pc-theme-toggle: font-size: 12px']);
  assert.deepEqual(
    offenders.filter((item) => !allowed.has(item)),
    [],
    'fonts and text sizes come from the theme',
  );
});

test('layout spacing is on the shared scale', () => {
  const offenders = [];
  for (const { selector, body } of rules(structural)) {
    if (selector === ':root') continue;
    if (INHERITED_GEOMETRY.some((item) => selector.split(',').some((s) => s.trim() === item))) {
      continue;
    }
    SPACING.lastIndex = 0;
    let match;
    while ((match = SPACING.exec(body)) !== null) {
      const value = match[3].trim();
      if (/\bvar\(--pc-sp-/.test(value)) continue;
      // `auto` is a layout instruction, not a measurement off the scale.
      if (/^(0|auto|0 auto|inherit|normal|0 0)$/.test(value)) continue;
      offenders.push(`${selector} { ${match[1]}${match[2] ?? ''}: ${value} }`);
    }
  }
  assert.deepEqual(
    offenders,
    [],
    'use var(--pc-sp-*) for layout, or list the selector as inherited geometry',
  );
});

test('the spacing scale is a 4px progression', () => {
  const scale = [...structural.matchAll(/--pc-sp-(\d+):\s*(\d+)px/g)].map(([, step, value]) => [
    Number(step),
    Number(value),
  ]);
  assert.ok(scale.length >= 5, 'the scale is defined');
  for (const [step, value] of scale) {
    assert.equal(value, step * 4, `--pc-sp-${step} should be ${step * 4}px`);
  }
});

test('form controls use shared grid guides instead of independent column flow', () => {
  const blocks = rules(structural);
  const fields = blocks.find(({ selector }) => selector === '.pc-fields');
  const field = blocks.find(({ selector }) => selector === '.pc-field');
  const label = blocks.find(({ selector }) => selector === '.pc-field > label');

  assert.ok(fields, 'the form field collection has a structural rule');
  assert.match(fields.body, /display\s*:\s*grid/);
  assert.match(fields.body, /grid-template-columns\s*:\s*repeat\(2,/);
  assert.doesNotMatch(fields.body, /(?:^|;)\s*columns?\s*:/, 'CSS columns drift row baselines');

  assert.ok(field, 'an individual form field has a structural rule');
  assert.match(field.body, /grid-template-columns\s*:\s*210px\s+minmax\(0,\s*1fr\)/);
  assert.match(field.body, /align-self\s*:\s*start/);

  assert.ok(label, 'form labels have a structural rule');
  assert.match(label.body, /white-space\s*:\s*nowrap/);

  assert.match(
    structural,
    /@media\s*\(max-width:\s*820px\)[\s\S]*?\.pc-field\s*\{[^}]*grid-template-columns\s*:\s*1fr/,
    'narrow layouts should stack labels above full-width controls',
  );
  assert.doesNotMatch(
    structural,
    /@media\s*\(max-width:\s*9\d\dpx\)[\s\S]*?\.pc-fields\s*\{[^}]*max-width\s*:\s*720px/,
    'a centred side-label column creates a large empty left gutter',
  );
});

test('the theme carries both schemes and sets color-scheme for each', () => {
  for (const scheme of ['dark', 'light']) {
    assert.ok(
      new RegExp(`\\[data-pv-theme='${scheme}'\\][^{]*\\{[^}]*color-scheme:\\s*${scheme}`).test(
        theme,
      ),
      `${scheme} must set color-scheme so native controls follow it`,
    );
  }
});
