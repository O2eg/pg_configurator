/**
 * The help table and the form must describe the same set of fields.
 *
 * A form field with no entry silently loses its tooltip; an entry with no field
 * is text nobody will ever read. Both are easy to create by editing one file and
 * not the other, so the two lists are compared against the generated schema.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { FIELD_HELP } from '../src/field-help.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const schema = JSON.parse(
  readFileSync(join(HERE, '..', 'data', 'input-schema.json'), 'utf8'),
).payload;

const formFields = schema.options.filter((option) => option.form_field === true);

test('the form and the help table cover the same fields', () => {
  const described = new Set(Object.keys(FIELD_HELP));
  const shown = new Set(formFields.map((option) => option.dest));
  assert.deepEqual(
    [...shown].filter((dest) => !described.has(dest)),
    [],
    'these form fields have no explanation',
  );
  assert.deepEqual(
    [...described].filter((dest) => !shown.has(dest)),
    [],
    'these explanations belong to no form field',
  );
});

test('every explanation says more than the CLI help line does', () => {
  const helpByDest = new Map(formFields.map((option) => [option.dest, option.help ?? '']));
  for (const [dest, text] of Object.entries(FIELD_HELP)) {
    assert.equal(typeof text, 'string');
    // The point of the table is the part `--help` has no room for. Anything
    // this short is the CLI line copied across, which helps nobody.
    assert.ok(
      text.length > helpByDest.get(dest).length + 40,
      `${dest}: the explanation barely exceeds its --help line`,
    );
    assert.ok(!/\s{2,}/.test(text.replace(/\n/g, ' ')), `${dest}: doubled whitespace`);
    assert.ok(text.trim() === text, `${dest}: stray whitespace at the edges`);
  }
});
