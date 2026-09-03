/**
 * The page: a form generated from the input schema, and the calculation's
 * output as tabs.
 *
 * This module only renders and reacts. Every number it shows comes from
 * `makeConf`, which is the same code the Node CLI runs and the same code the
 * differential suite compares with the Python reference — the page adds no
 * arithmetic of its own.
 *
 * All user text reaches the DOM through `textContent`. Nothing here builds
 * markup from a string.
 */

import { FIELD_HELP } from './field-help.js';
import {
  ASSUMED_PEAK_WAL_RATE,
  loadSettingMetadata,
  makeConf,
  unquotePostgresqlConfValue,
} from './make-conf.js';
import { diffConfigurations, parseUserConfig } from './config-diff.js';
import { createEnums } from './configurator.js';
import { numberOf, PyNumber, pyStr, sizeTo, SYS_IEC } from './units.js';

/**
 * The input groups, in the order someone actually settles them.
 *
 * Describe the machine, then what runs on it; divide its memory, then say how
 * many sessions share it. Replication comes next because it is a decision about
 * the cluster rather than the host, and WAL comes last because its sizing reads
 * the answer: retention only means something once there is a replica to retain
 * for. Each group is a tab of its own — the whole form at once was mostly
 * scrolling.
 */
const FIELD_GROUPS = [
  { title: 'Hardware', fields: ['db_cpu', 'db_ram', 'db_disk_type', 'disk_score', 'db_size'] },
  { title: 'Workload', fields: ['db_duty', 'pg_version', 'platform', 'conf_profiles'] },
  {
    title: 'Memory budget',
    fields: [
      'reserved_ram_percent',
      'reserved_system_ram',
      'shared_buffers_part',
      'client_mem_part',
      'maintenance_mem_part',
      'autovacuum_workers_mem_part',
      'maintenance_conns_mem_part',
      'work_mem_concurrency_factor',
    ],
  },
  {
    title: 'Connections',
    fields: [
      'min_conns',
      'max_conns',
      'min_autovac_workers',
      'max_autovac_workers',
      'min_maint_conns',
      'max_maint_conns',
    ],
  },
  {
    title: 'Replication',
    fields: [
      'replication_mode',
      'pitr_enabled',
      'synchronous_standby_names',
      'replica_count',
      'logical_subscription_count',
    ],
  },
  {
    title: 'WAL',
    fields: ['peak_wal_rate', 'replica_outage_tolerance', 'wal_disk_budget', 'wal_segment_size'],
  },
];

/**
 * Fields that are a number between two ends, given a slider.
 *
 * `max` never exceeds what make_conf validates — the memory parts are capped at
 * 0.4 and the shares at 1.0 because that is where the calculation refuses —
 * but the other ends are a judgement about what is comfortable, not a limit.
 * That is why every slider keeps an editable readout beside it: a machine
 * bigger than the slider is still expressible by typing.
 *
 * `unit` renders and parses a size the way the option expects it;
 * `complement` keeps a pair that must sum to 1.0 summing to 1.0;
 * `atMost`/`atLeast` keep a min/max pair from crossing.
 */
const SLIDERS = {
  db_cpu: { min: 1, max: 128, step: 1 },
  db_ram: { min: 1, max: 1024, step: 1, unit: 'Gi' },
  // `optional.unset` names what the calculation does with no value, in the
  // readout's own placeholder. "unset" described the widget; these describe the
  // answer, and disk-score can show the number it is already using.
  disk_score: {
    min: 0,
    max: 100,
    step: 1,
    optional: true,
    unset: (result) => (result ? String(result.calculation.disk_score) : ''),
  },
  // Four orders of magnitude do not fit a linear track: one pixel would be
  // hundreds of gibibytes and the small end would be unreachable. `stops` gives
  // the slider a 1-2-5 series instead, so every position is a size someone
  // would actually type, and every one of them formats cleanly.
  db_size: {
    min: 10,
    max: 102400,
    unit: 'Gi',
    stops: [10, 20, 50, 100, 200, 500, 1024, 2048, 5120, 10240, 20480, 51200, 102400],
    optional: true,
    unset: () => '—',
  },

  reserved_ram_percent: { min: 0, max: 50, step: 1 },
  reserved_system_ram: { min: 64, max: 8192, step: 64, unit: 'Mi' },
  shared_buffers_part: { min: 0.05, max: 0.8, step: 0.01 },
  client_mem_part: { min: 0.05, max: 0.4, step: 0.01 },
  maintenance_mem_part: { min: 0.02, max: 0.4, step: 0.01 },
  autovacuum_workers_mem_part: { min: 0.05, max: 0.95, step: 0.05, complement: 'maintenance_conns_mem_part' },
  maintenance_conns_mem_part: { min: 0.05, max: 0.95, step: 0.05, complement: 'autovacuum_workers_mem_part' },
  work_mem_concurrency_factor: { min: 1, max: 16, step: 0.5 },

  replica_count: { min: 0, max: 16, step: 1 },
  logical_subscription_count: { min: 0, max: 32, step: 1 },

  min_conns: { min: 1, max: 1000, step: 1, atMost: 'max_conns' },
  max_conns: { min: 1, max: 1000, step: 1, atLeast: 'min_conns' },
  min_autovac_workers: { min: 1, max: 64, step: 1, atMost: 'max_autovac_workers' },
  max_autovac_workers: { min: 1, max: 64, step: 1, atLeast: 'min_autovac_workers' },
  min_maint_conns: { min: 1, max: 64, step: 1, atMost: 'max_maint_conns' },
  max_maint_conns: { min: 1, max: 64, step: 1, atLeast: 'min_maint_conns' },

  peak_wal_rate: {
    min: 1,
    max: 512,
    step: 1,
    unit: 'Mi',
    optional: true,
    unset: () => ASSUMED_PEAK_WAL_RATE,
  },
  replica_outage_tolerance: { min: 0, max: 7200, step: 60 },
  wal_disk_budget: { min: 1, max: 1024, step: 1, unit: 'Gi' },
};

/**
 * What make_conf resolves an unset option to.
 *
 * Verified equal to leaving it unset: same configuration, same normalized
 * inputs, same advisories. Showing the resolved value is what lets the form
 * answer "what is in force" instead of "nothing was chosen".
 */
const RESOLVED_DEFAULTS = { replication_mode: 'physical' };

/**
 * wal_segment_size is fixed at initdb and never appears in postgresql.conf; the
 * form asks for it only to describe the cluster the settings are sized for. The
 * tool accepts every power of two from 1Mi to 1Gi, but that is the validator's
 * range, not a menu: these are the sizes clusters are actually built with.
 */
const LOCAL_CHOICES = {
  wal_segment_size: ['16Mi', '32Mi', '64Mi', '128Mi'],
};

// Mirrors ADVISORY_SEVERITIES in the calculation, in the same order, so the
// panel reads severest first however the list was built.
const ADVISORY_HEADINGS = {
  warning: 'Warnings',
  assumption: 'Assumptions',
  info: 'Notes',
};

const TABS = [
  { id: 'main', label: 'Main' },
  { id: 'calculation', label: 'Calculation' },
  { id: 'advisories', label: 'Advisories', finding: true },
  { id: 'overrides', label: 'Overrides' },
  { id: 'diff', label: 'Diff' },
  // Kept for parity/debugging and easy restoration, but raw runtime JSON is
  // not a primary browser workflow.
  { id: 'artifact', label: 'Artifact', hidden: true },
];

const MAIN_OUTPUT_TABS = [
  { id: 'conf', label: 'postgresql.conf' },
  { id: 'settings', label: 'Settings', count: true },
  { id: 'patroni', label: 'Patroni' },
];

/**
 * Views that must not outlive the input they were computed from.
 *
 * A stale result elsewhere is context; here it is something the reader would
 * copy into a cluster believing it matches the form above it. The rest of the
 * tabs keep the last good answer so a half-typed size does not blank the page.
 */
const HIDES_STALE_OUTPUT = new Set(['conf', 'settings', 'patroni', 'diff']);

const STALE_NOTICE = 'Not shown while the input is invalid — see the message on the Main tab.';

const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
};

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

/** Defaults the CLI reads from the host; a browser has to be told. */
const BROWSER_DEFAULTS = { db_cpu: '4', db_ram: '16Gi' };

export class ConfiguratorPage {
  constructor(root, data) {
    this.root = root;
    this.rules = data.rules;
    this.pgSettings = data.pgSettings;
    this.schema = data.schema;
    this.version = data.version;
    this.enums = createEnums(this.rules.enums);
    this.optionsByDest = new Map(this.schema.options.map((item) => [item.dest, item]));
    this.values = {};
    this.result = null;
    this.error = null;
    this.settingsFilter = '';
    // What the Diff tab compares against; kept across recalculations.
    this.diffText = '';
    this.active = 'main';
    this.activeMainOutput = 'conf';

    for (const option of this.schema.options) {
      if (option.role !== 'calculation') continue;
      this.values[option.dest] =
        option.default_source === 'host'
          ? (BROWSER_DEFAULTS[option.dest] ?? '')
          : (option.default ?? RESOLVED_DEFAULTS[option.dest] ?? null);
    }
  }

  mount() {
    this.tabBar = this.root.querySelector('#tabs');
    this.panels = this.root.querySelector('#panels');
    this.errorBox = this.root.querySelector('#error');
    this.buildTabs();
    this.buildActions();
    this.buildForm();
    this.calculate();
    this.bindTooltip();
  }

  /**
   * One tooltip element for the whole page, driven by `data-pc-tip`.
   *
   * Delegated from the panel container so it survives every re-render, and
   * positioned in viewport coordinates so it never escapes the window. Ported
   * from pg_explain_viewer, down to the delay before it appears.
   */
  bindTooltip() {
    const tip = el('div', 'pc-tip');
    tip.hidden = true;
    tip.setAttribute('role', 'tooltip');
    this.root.body.append(tip);
    this.tipNode = tip;

    let anchor = null;
    let timer = null;

    const hide = () => {
      if (timer) clearTimeout(timer);
      timer = null;
      anchor = null;
      tip.hidden = true;
    };

    const place = (x, y) => {
      tip.style.left = '0px';
      tip.style.top = '0px';
      const width = tip.offsetWidth;
      const height = tip.offsetHeight;
      let left = x + 14;
      let top = y + 16;
      if (left + width + 8 > window.innerWidth) left = Math.max(8, x - width - 10);
      if (top + height + 8 > window.innerHeight) top = Math.max(8, y - height - 12);
      tip.style.left = `${left}px`;
      tip.style.top = `${top}px`;
    };

    this.panels.addEventListener('mouseover', (event) => {
      const target = event.target.closest?.('[data-pc-tip]');
      if (!target) {
        hide();
        return;
      }
      if (target === anchor) return;
      anchor = target;
      if (timer) clearTimeout(timer);
      const { clientX, clientY } = event;
      timer = setTimeout(() => {
        if (anchor !== target) return;
        tip.textContent = target.getAttribute('data-pc-tip');
        tip.hidden = false;
        place(clientX, clientY);
      }, 160);
    });
    this.panels.addEventListener('mousemove', (event) => {
      if (!tip.hidden && anchor) place(event.clientX, event.clientY);
    });
    this.panels.addEventListener('mouseleave', hide);
    this.panels.addEventListener('mousedown', hide);
    this.root.addEventListener('scroll', hide, { passive: true, capture: true });
  }

  // ---------------------------------------------------------------- tabs

  buildTabs() {
    clear(this.tabBar);
    this.tabButtons = new Map();
    for (const tab of TABS) {
      const button = el('button', 'pc-tab');
      if (tab.finding) button.classList.add('pc-tab-finding');
      button.type = 'button';
      button.id = `tab-${tab.id}`;
      button.setAttribute('role', 'tab');
      button.setAttribute('aria-controls', `panel-${tab.id}`);
      button.hidden = tab.hidden === true;
      button.append(el('span', null, tab.label));
      const count = el('span', 'pc-count');
      button.append(count);
      button.addEventListener('click', () => this.select(tab.id));
      this.tabBar.append(button);
      this.tabButtons.set(tab.id, { button, count });
    }

    clear(this.panels);
    this.panelNodes = new Map();
    for (const tab of TABS) {
      const panel = el('div', 'pc-panel');
      panel.id = `panel-${tab.id}`;
      panel.setAttribute('role', 'tabpanel');
      panel.setAttribute('aria-labelledby', `tab-${tab.id}`);
      panel.hidden = tab.id !== this.active;
      this.panels.append(panel);
      this.panelNodes.set(tab.id, panel);
    }
    this.syncTabs();
  }

  syncTabs() {
    for (const [id, { button }] of this.tabButtons) {
      const selected = id === this.active;
      button.setAttribute('aria-selected', String(selected));
      button.tabIndex = selected ? 0 : -1;
      this.panelNodes.get(id).hidden = !selected;
    }
  }

  select(id) {
    this.active = id;
    this.syncTabs();
    this.renderPanel(id);
  }

  /**
   * The two form-wide buttons, in the tab row rather than under the fields.
   *
   * They act on the whole page, not on the group they used to sit beneath, and
   * the settings table now occupies that space.
   */
  buildActions() {
    const bar = this.root.querySelector('#actions');
    clear(bar);
    const inputsButton = el('button', 'pc-btn', 'Download inputs (JSON)');
    inputsButton.type = 'button';
    inputsButton.title = 'The same document the command line accepts through --input-json';
    inputsButton.addEventListener('click', () => this.downloadInputDocument());
    const resetButton = el('button', 'pc-btn', 'Reset to defaults');
    resetButton.type = 'button';
    resetButton.addEventListener('click', () => this.reset());
    bar.append(inputsButton, resetButton);
  }

  // ---------------------------------------------------------------- form

  buildForm() {
    const panel = this.panelNodes.get('main');
    clear(panel);
    const form = el('div', 'pc-form');
    this.fieldNodes = new Map();

    const bar = el('div', 'pc-subtabs');
    bar.setAttribute('role', 'tablist');
    bar.setAttribute('aria-label', 'Input groups');
    form.append(bar);

    this.groupTabs = new Map();
    this.groupPanels = new Map();
    for (const group of FIELD_GROUPS) {
      const slug = groupSlug(group.title);
      const button = el('button', 'pc-subtab', group.title);
      button.type = 'button';
      button.id = `subtab-${slug}`;
      button.setAttribute('role', 'tab');
      button.setAttribute('aria-controls', `subpanel-${slug}`);
      button.addEventListener('click', () => this.selectGroup(slug));
      bar.append(button);

      const box = el('div', 'pc-groupbox');
      box.id = `subpanel-${slug}`;
      box.setAttribute('role', 'tabpanel');
      box.setAttribute('aria-labelledby', `subtab-${slug}`);
      // One group at a time has the whole width, so its fields flow in columns
      // rather than stacking down a third of the page.
      const fields = el('div', 'pc-fields');
      for (const dest of group.fields) {
        const option = this.optionsByDest.get(dest);
        if (option === undefined || option.form_field !== true) continue;
        fields.append(this.buildField(option));
      }
      box.append(fields);
      form.append(box);
      this.groupTabs.set(slug, button);
      this.groupPanels.set(slug, box);
    }
    this.activeGroup ??= groupSlug(FIELD_GROUPS[0].title);
    this.syncGroups();
    panel.append(form);

    // The message belongs with the fields that cause it, not above the tab row:
    // it reports on the values just entered, and it sits directly over the
    // settings it is suppressing.
    panel.append(this.errorBox);

    // Deployable output sits right below the form. These views share the same
    // calculation and stay inside Main so changing an input and copying its
    // result no longer requires a trip through the page-wide navigation.
    const outputSection = el('div', 'pc-section');
    const outputTabs = el('div', 'pc-output-tabs');
    outputTabs.setAttribute('role', 'tablist');
    outputTabs.setAttribute('aria-label', 'Configuration outputs');
    outputSection.append(outputTabs);

    this.mainOutputTabs = new Map();
    this.mainOutputPanels = new Map();
    for (const output of MAIN_OUTPUT_TABS) {
      const button = el('button', 'pc-output-tab');
      button.type = 'button';
      button.id = `main-output-tab-${output.id}`;
      button.setAttribute('role', 'tab');
      button.setAttribute('aria-controls', `main-output-panel-${output.id}`);
      button.append(el('span', null, output.label));
      const count = el('span', 'pc-count');
      if (output.count) button.append(count);
      button.addEventListener('click', () => this.selectMainOutput(output.id));
      outputTabs.append(button);
      this.mainOutputTabs.set(output.id, { button, count });

      const outputPanel = el('div', 'pc-output-panel');
      outputPanel.id = `main-output-panel-${output.id}`;
      outputPanel.setAttribute('role', 'tabpanel');
      outputPanel.setAttribute('aria-labelledby', button.id);
      outputSection.append(outputPanel);
      this.mainOutputPanels.set(output.id, outputPanel);
    }
    this.syncMainOutputTabs();
    panel.append(outputSection);
  }

  selectGroup(slug) {
    this.activeGroup = slug;
    this.syncGroups();
  }

  syncGroups() {
    for (const [slug, button] of this.groupTabs) {
      const selected = slug === this.activeGroup;
      button.setAttribute('aria-selected', String(selected));
      button.tabIndex = selected ? 0 : -1;
      this.groupPanels.get(slug).hidden = !selected;
    }
  }

  selectMainOutput(id) {
    this.activeMainOutput = id;
    this.syncMainOutputTabs();
    this.renderMainOutput(id);
  }

  syncMainOutputTabs() {
    for (const [id, { button }] of this.mainOutputTabs) {
      const selected = id === this.activeMainOutput;
      button.setAttribute('aria-selected', String(selected));
      button.tabIndex = selected ? 0 : -1;
      this.mainOutputPanels.get(id).hidden = !selected;
    }
  }

  buildField(option) {
    const wrap = el('div', 'pc-field');
    wrap.dataset.dest = option.dest;
    const label = el('label', null, option.primary_option.replace(/^--/, ''));
    label.htmlFor = `field-${option.dest}`;
    // The native title bubble is slow, unstyled and truncates; the page draws
    // its own, the way pg_explain_viewer does.
    const help = FIELD_HELP[option.dest];
    if (help) label.setAttribute('data-pc-tip', help);
    wrap.append(label);

    const slider = SLIDERS[option.dest];
    if (slider) {
      const control = this.buildSlider(option, slider);
      wrap.append(control);
      this.fieldNodes.set(option.dest, { wrap, input: control });
      return wrap;
    }

    if (option.dest === 'conf_profiles') {
      // Profile names are the value of this field, so they start on the same
      // control guide as selects and sliders. At compact desktop widths CSS
      // gives this long choice list a full outer row instead of clipping it.
      wrap.classList.add('pc-field-profiles');
      wrap.append(this.buildProfilePicker());
      this.fieldNodes.set(option.dest, { wrap });
      return wrap;
    }

    const localChoices = LOCAL_CHOICES[option.dest];
    let input;
    if (localChoices) {
      input = el('select');
      for (const choice of localChoices) {
        const item = el('option', null, choice);
        item.value = choice;
        input.append(item);
      }
      input.value = String(this.values[option.dest] ?? localChoices[0]);
    } else if (option.choices !== null) {
      input = el('select');
      for (const choice of option.choices) {
        const item = el('option', null, choice);
        item.value = choice;
        input.append(item);
      }
      // A select with no static default used to offer "— default —", which
      // named an internal state rather than an answer. make_conf resolves it
      // deterministically and to a byte-identical result, so the form shows
      // the value that is actually in force.
      const current = this.values[option.dest] ?? RESOLVED_DEFAULTS[option.dest];
      input.value = String(current ?? option.choices[0]);
    } else if (option.type === 'parse_bool') {
      input = el('select');
      for (const [text, value] of [
        ['true', 'true'],
        ['false', 'false'],
      ]) {
        const item = el('option', null, text);
        item.value = value;
        input.append(item);
      }
      input.value = this.values[option.dest] ? 'true' : 'false';
    } else {
      input = el('input');
      input.type = 'text';
      input.spellcheck = false;
      const current = this.values[option.dest];
      input.value = current === null || current === undefined ? '' : String(current);
    }

    input.id = `field-${option.dest}`;
    input.addEventListener('input', () => this.onFieldChange(option, input));
    input.addEventListener('change', () => this.onFieldChange(option, input));
    wrap.append(input);
    this.fieldNodes.set(option.dest, { wrap, input });
    return wrap;
  }

  /**
   * Profiles, as a list rather than a name to remember.
   *
   * They are applied in the order shown, which matters: `ext_perf` and
   * `profile_backend_perf` both set `autovacuum_naptime` and
   * `max_parallel_workers_per_gather`, so the later one wins. The command line
   * can express any order; the page commits to this one and says so.
   *
   * `profile_1c` is exclusive, so the control enforces it instead of letting
   * the calculation refuse the combination.
   */
  buildProfilePicker() {
    const box = el('div', 'pc-choices');
    const names = Object.keys(this.rules.profiles);
    this.profileChecks = new Map();

    for (const name of names) {
      const profile = this.rules.profiles[name];
      const label = el('label');
      const check = el('input');
      check.type = 'checkbox';
      check.value = name;
      check.id = `field-profile-${name}`;
      if (profile.description) label.title = profile.description;
      check.addEventListener('change', () => {
        if (check.checked) {
          for (const [other, node] of this.profileChecks) {
            if (other === name) continue;
            // An exclusive profile clears the rest, and any other clears an
            // exclusive one. Skipping itself keeps a tick from undoing itself.
            if (this.rules.profiles[name].exclusive || this.rules.profiles[other].exclusive) {
              node.checked = false;
            }
          }
        }
        this.syncProfiles();
        this.calculate();
      });
      label.append(check, el('span', null, name));
      box.append(label);
      this.profileChecks.set(name, check);
    }

    this.syncProfileChecks();
    return box;
  }

  syncProfiles() {
    const chosen = [...this.profileChecks]
      .filter(([, check]) => check.checked)
      .map(([name]) => name);
    this.values.conf_profiles = chosen.length ? chosen.join(',') : null;
    this.syncProfileChecks();
  }

  /** A profile the target major does not support cannot be chosen. */
  syncProfileChecks() {
    if (!this.profileChecks) return;
    const version = String(this.values.pg_version);
    const exclusiveChosen = [...this.profileChecks].some(
      ([name, check]) => check.checked && this.rules.profiles[name].exclusive,
    );
    for (const [name, check] of this.profileChecks) {
      const profile = this.rules.profiles[name];
      const unsupported = !profile.supported_versions.includes(version);
      check.disabled = unsupported || (exclusiveChosen && !check.checked);
      check.parentElement.classList.toggle('pc-choice-off', check.disabled);
    }
  }

  /** A range plus an editable readout, driving the same value. */
  buildSlider(option, spec) {
    const control = el('div', 'pc-control');
    const range = el('input');
    range.type = 'range';
    range.className = 'pc-range';
    range.min = String(spec.stops ? 0 : spec.min);
    range.max = String(spec.stops ? spec.stops.length - 1 : spec.max);
    range.step = '1';
    if (!spec.stops) range.step = String(spec.step);
    range.id = `field-${option.dest}`;
    range.setAttribute('aria-label', option.primary_option.replace(/^--/, ''));

    const readout = el('input');
    readout.type = 'text';
    readout.className = 'pc-readout';
    readout.spellcheck = false;
    readout.id = `readout-${option.dest}`;
    readout.setAttribute('aria-label', `${option.primary_option.replace(/^--/, '')} value`);
    const showUnset = () => {
      if (!spec.optional) return;
      const label = spec.unset(this.result);
      readout.placeholder = label;
      if (this.values[option.dest] !== null) return;
      // A slider pointing somewhere while the readout reads empty is a
      // contradiction. Park it on the value actually in force when there is
      // one, and at the low end when there is not — a range whose value was
      // never set keeps the midpoint it was born with, clamped to whatever
      // maximum arrives later, which left db-size sitting on 100Ti.
      const derived = sliderPosition(label, spec);
      range.value =
        label !== '' && derived !== null
          ? String(rangePosition(derived, spec))
          : range.min;
    };

    const write = (value) => {
      readout.value = value === null ? '' : String(value);
      const numeric = sliderPosition(value, spec);
      if (numeric !== null) range.value = String(rangePosition(numeric, spec));
      showUnset();
    };
    write(this.values[option.dest]);

    range.addEventListener('input', () => {
      const numeric = spec.stops ? spec.stops[Number(range.value)] : Number(range.value);
      this.values[option.dest] = formatSliderValue(numeric, spec, option);
      this.applyCoupling(option.dest, numeric, spec);
      this.syncSliders();
      this.calculate();
    });
    readout.addEventListener('input', () => {
      const raw = readout.value.trim();
      this.values[option.dest] = raw === '' && spec.optional ? null : coerceReadout(raw, option);
      const numeric = sliderPosition(this.values[option.dest], spec);
      if (numeric !== null) range.value = String(rangePosition(numeric, spec));
      this.calculate();
    });

    control.append(range, readout);
    this.sliderNodes ??= new Map();
    this.sliderNodes.set(option.dest, { range, readout, spec, option, write, showUnset });
    return control;
  }

  /** Keep a coupled pair consistent instead of letting it become an error. */
  applyCoupling(dest, numeric, spec) {
    if (spec.complement) {
      const other = SLIDERS[spec.complement];
      const value = round2(1 - numeric);
      this.values[spec.complement] = formatSliderValue(
        clamp(value, other.min, other.max),
        other,
        this.optionsByDest.get(spec.complement),
      );
    }
    if (spec.atMost && Number(this.values[spec.atMost]) < numeric) {
      this.values[spec.atMost] = numeric;
    }
    if (spec.atLeast && Number(this.values[spec.atLeast]) > numeric) {
      this.values[spec.atLeast] = numeric;
    }
  }

  syncSliders() {
    for (const [dest, node] of this.sliderNodes ?? []) node.write(this.values[dest]);
  }

  /**
   * Refresh only what a new result changes about an empty field.
   *
   * Deliberately not syncSliders: that rewrites `readout.value`, and this runs
   * from the readout's own input handler, where it would fight the caret.
   */
  syncUnsetPlaceholders() {
    for (const [, node] of this.sliderNodes ?? []) node.showUnset();
  }

  onFieldChange(option, input) {
    const raw = input.value;
    let value = raw;
    if (option.type === 'parse_bool') {
      value = raw === 'true';
    } else if (option.type === 'int') {
      value = /^-?\d+$/.test(raw.trim()) ? Number(raw.trim()) : raw.trim();
    } else if (option.type === 'float') {
      const parsed = Number(raw);
      value = raw.trim() !== '' && !Number.isNaN(parsed) ? parsed : raw.trim();
    } else if (raw === '' && option.default === null) {
      value = null;
    }
    if (option.choices !== null && raw === '') value = null;
    this.values[option.dest] = value;
    this.calculate();
  }

  reset() {
    this.sliderNodes = new Map();
    this.profileChecks = null;
    for (const option of this.schema.options) {
      if (option.role !== 'calculation') continue;
      this.values[option.dest] =
        option.default_source === 'host'
          ? (BROWSER_DEFAULTS[option.dest] ?? '')
          : option.default;
    }
    this.buildForm();
    this.calculate();
  }

  // --------------------------------------------------------- calculation

  callOptions() {
    const options = {};
    for (const option of this.schema.options) {
      if (option.role !== 'calculation') continue;
      options[option.make_conf_parameter] = this.values[option.dest];
    }
    return options;
  }

  calculate() {
    this.syncProfileChecks();
    const options = this.callOptions();
    const cpuCores = options.cpu_cores;
    const ramValue = options.ram_value;
    delete options.cpu_cores;
    delete options.ram_value;

    try {
      this.result = makeConf(cpuCores, ramValue, options, {
        rules: this.rules,
        pgSettings: this.pgSettings,
        enums: this.enums,
      });
      this.error = null;
    } catch (failure) {
      // The previous result stays on screen: a half-typed size should not blank
      // the page. The message names what is wrong.
      this.error = failure.message ?? String(failure);
    }
    this.syncUnsetPlaceholders();
    this.renderError();
    this.renderCounts();
    this.renderPanel(this.active);
  }

  renderError() {
    // The box keeps its place whether or not it says anything: hiding it
    // outright made every panel below jump on each keystroke that fixed or
    // broke the input.
    this.errorBox.textContent = this.error ?? '';
    this.errorBox.classList.toggle('pc-error-quiet', this.error === null);
    for (const [, node] of this.fieldNodes) node.wrap.classList.remove('pc-invalid');
  }

  renderCounts() {
    const counts = {
      // The badge counts what needs a decision. Assumptions and notes are
      // worth reading once; putting them in the same number as a real conflict
      // is what turns the badge into wallpaper.
      advisories: this.result
        ? this.result.advisories.filter((item) => item.severity === 'warning').length
        : 0,
      overrides: this.result ? this.result.overrides.length : 0,
      calculation: this.result ? Object.keys(this.result.calculation).length : 0,
      diff: this.hidesStaleOutput('diff') ? 0 : (this.diffSummary()?.rows.length ?? 0),
    };
    for (const [id, value] of Object.entries(counts)) {
      const entry = this.tabButtons.get(id);
      if (entry) entry.count.textContent = value ? String(value) : '';
    }
    const settings = this.mainOutputTabs?.get('settings');
    if (settings) {
      const count = this.result && !this.hidesStaleOutput('settings')
        ? Object.keys(this.result.parameters).length
        : 0;
      settings.count.textContent = count ? String(count) : '';
    }
  }

  // -------------------------------------------------------------- panels

  /** Whether this view must not show a result the current input did not produce. */
  hidesStaleOutput(id) {
    return this.error !== null && HIDES_STALE_OUTPUT.has(id);
  }

  renderPanel(id) {
    if (id === 'main') {
      this.renderMainOutput(this.activeMainOutput);
      return;
    }
    const panel = this.panelNodes.get(id);
    clear(panel);
    if (this.hidesStaleOutput(id)) {
      panel.append(el('div', 'pc-empty', STALE_NOTICE));
      return;
    }
    if (this.result === null) {
      panel.append(el('div', 'pc-empty', 'No configuration yet.'));
      return;
    }
    const renderers = {
      calculation: () => this.renderCalculation(panel),
      advisories: () => this.renderAdvisories(panel),
      overrides: () => this.renderOverrides(panel),
      diff: () => this.renderDiff(panel),
      artifact: () => this.renderArtifact(panel),
    };
    renderers[id]?.();
  }

  renderMainOutput(id) {
    const panel = this.mainOutputPanels.get(id);
    clear(panel);
    if (this.hidesStaleOutput(id)) {
      panel.append(el('div', 'pc-empty', STALE_NOTICE));
      return;
    }
    if (this.result === null) {
      panel.append(el('div', 'pc-empty', 'No configuration yet.'));
      return;
    }
    const renderers = {
      conf: () => this.renderConf(panel),
      settings: () => this.renderSettings(panel),
      patroni: () => this.renderPatroni(panel),
    };
    renderers[id]?.();
  }

  renderConf(panel) {
    const lines = [
      `# Generated by pg-configurator ${this.version} (web build)`,
      `# PostgreSQL ${this.result.inputs.pg_version}`,
    ];
    for (const item of this.result.advisories) {
      const messageLines = String(item.message).split(/\r\n|[\r\n]/);
      lines.push(`# ${item.severity.toUpperCase()}: ${messageLines[0]}`);
      for (const line of messageLines.slice(1)) lines.push(`# ${line}`);
    }
    lines.push('');
    for (const [name, value] of Object.entries(this.result.config)) {
      lines.push(`${name} = ${value}`);
    }
    const text = `${lines.join('\n')}\n`;
    panel.append(this.copyBar(text, 'postgresql.conf'));
    panel.append(el('pre', 'pc-code', text));
  }

  /** The settings table under the form, rebuilt on every recalculation. */
  renderSettings(panel) {
    this.renderParameters(panel);
  }

  renderParameters(panel) {
    const filter = el('div', 'pc-filter');
    const search = el('input');
    search.type = 'search';
    search.placeholder = 'filter settings';
    // The section is rebuilt whenever a field changes, so the filter has to
    // outlive its own input element or it would clear itself as you type.
    search.value = this.settingsFilter ?? '';
    filter.append(search);
    filter.append(
      el('span', 'pc-hint', 'Apply mode says what it costs to put the value into effect.'),
    );
    panel.append(filter);

    const scroll = el('div', 'pc-tablewrap');
    const table = el('table', 'pc-table');
    const head = el('thead');
    const headRow = el('tr');
    for (const column of ['Setting', 'Value', 'Apply', 'Source', 'Context']) {
      headRow.append(el('th', null, column));
    }
    head.append(headRow);
    table.append(head);

    const body = el('tbody');
    for (const [name, detail] of Object.entries(this.result.parameters)) {
      const row = el('tr');
      row.append(el('td', null, name));
      row.append(el('td', null, detail.value));

      const apply = el('td');
      apply.append(el('span', `pc-apply pc-apply-${detail.apply_mode}`, detail.apply_mode));
      row.append(apply);

      const sourceClass = detail.source === 'base' ? 'pc-source-base' : 'pc-source-profile';
      row.append(el('td', sourceClass, detail.source));
      row.append(el('td', 'pc-muted', detail.context));
      body.append(row);
    }
    table.append(body);
    scroll.append(table);
    panel.append(scroll);

    const applyFilter = () => {
      const needle = (this.settingsFilter ?? '').trim().toLowerCase();
      for (const row of body.children) {
        row.hidden = needle !== '' && !row.textContent.toLowerCase().includes(needle);
      }
    };
    search.addEventListener('input', () => {
      this.settingsFilter = search.value;
      applyFilter();
    });
    applyFilter();
  }

  renderCalculation(panel) {
    panel.append(
      el(
        'div',
        'pc-hint pc-table-intro',
        'The budgets the settings are derived from. Byte counts are shown as the calculation holds them.',
      ),
    );
    const scroll = el('div', 'pc-tablewrap');
    const table = el('table', 'pc-table pc-calculation-table');
    const head = el('thead');
    const headRow = el('tr');
    headRow.append(el('th', null, 'Budget'), el('th', null, 'Value'), el('th', null, 'Size'));
    head.append(headRow);
    table.append(head);
    const body = el('tbody');
    for (const [name, value] of Object.entries(this.result.calculation)) {
      const row = el('tr');
      row.append(el('td', null, name));
      row.append(el('td', 'pc-num', renderScalar(value)));
      row.append(
        el(
          'td',
          'pc-num pc-faint',
          name.endsWith('_bytes') && typeof value === 'number' ? sizeTo(value, SYS_IEC) : '',
        ),
      );
      body.append(row);
    }
    table.append(body);
    scroll.append(table);
    panel.append(scroll);
  }

  renderAdvisories(panel) {
    if (!this.result.advisories.length) {
      panel.append(el('div', 'pc-empty', 'Nothing to report about this configuration.'));
      return;
    }
    for (const [severity, heading] of Object.entries(ADVISORY_HEADINGS)) {
      const items = this.result.advisories.filter((item) => item.severity === severity);
      if (!items.length) continue;
      panel.append(el('h3', 'pc-advisory-heading', `${heading} (${items.length})`));
      const list = el('ul', 'pc-list');
      for (const item of items) list.append(this.buildAdvisory(item));
      panel.append(list);
    }
  }

  buildAdvisory(item) {
    const entry = el('li', `pc-advisory pc-advisory-${item.severity}`);
    const head = el('div', 'pc-advisory-head');
    // The setting is what a reader looks for first: the message explains a
    // decision, and this says which line of the file it is about.
    if (item.setting !== null) {
      head.append(el('span', 'pc-advisory-setting', `${item.setting} = ${item.actual}`));
    }
    head.append(el('span', 'pc-advisory-code', item.code));
    entry.append(head, el('div', null, item.message));
    return entry;
  }

  renderOverrides(panel) {
    if (!this.result.overrides.length) {
      panel.append(
        el('div', 'pc-empty', 'No overrides: every setting comes from the base rules.'),
      );
      return;
    }
    const table = el('table', 'pc-table');
    const head = el('thead');
    const headRow = el('tr');
    headRow.append(
      el('th', null, 'Setting'),
      el('th', null, 'From'),
      el('th', null, 'Value From'),
      el('th', null, 'To'),
      el('th', null, 'Value To'),
    );
    head.append(headRow);
    table.append(head);
    const body = el('tbody');
    for (const item of this.result.overrides) {
      const row = el('tr');
      row.append(el('td', null, item.parameter));
      row.append(el('td', 'pc-muted', item.from));
      row.append(el('td', null, item.value_from));
      row.append(el('td', 'pc-source-profile', item.to));
      row.append(el('td', null, item.value_to));
      body.append(row);
    }
    table.append(body);
    const wrap = el('div', 'pc-tablewrap');
    wrap.append(table);
    panel.append(wrap);
  }

  // ------------------------------------------------------------------ diff

  /** What the pasted configuration disagrees with, or null when there is nothing to compare. */
  diffSummary() {
    if (this.result === null || !this.diffText.trim()) return null;
    const parsed = parseUserConfig(this.diffText);
    const metadata = loadSettingMetadata(this.pgSettings, this.result.inputs.pg_version);
    return { parsed, ...diffConfigurations(this.result.parameters, parsed.entries, metadata) };
  }

  renderDiff(panel) {
    panel.append(
      el(
        'div',
        'pc-table-intro pc-hint',
        'Paste the configuration the cluster runs now: postgresql.conf as it is (comments ' +
          'are ignored), or a pg_settings export as CSV, with or without a header row. Only ' +
          'the settings this run calculated are compared, after unit conversion, and only ' +
          'the ones that differ are listed.',
      ),
    );
    const input = el('textarea', 'pc-diff-input');
    input.id = 'diff-input';
    input.rows = 8;
    input.spellcheck = false;
    input.placeholder = 'shared_buffers = 4GB        # or:  name,setting,unit';
    input.setAttribute('aria-label', 'Current configuration to compare');
    input.value = this.diffText;
    const results = el('div');
    results.id = 'diff-results';
    input.addEventListener('input', () => {
      this.diffText = input.value;
      // The box is left alone so typing keeps its focus; only the answer moves.
      this.renderDiffResults(results);
      this.renderCounts();
    });
    panel.append(input, results);
    this.renderDiffResults(results);
  }

  renderDiffResults(container) {
    clear(container);
    const summary = this.diffSummary();
    if (summary === null) {
      container.append(el('div', 'pc-empty', 'Nothing pasted yet.'));
      return;
    }
    const { parsed, rows, matching, missing, unknown } = summary;
    const readAs = {
      conf: 'postgresql.conf',
      csv: parsed.header ? 'CSV with a header row' : 'CSV without a header row',
    }[parsed.format];
    if (!parsed.entries.size) {
      container.append(
        el(
          'div',
          'pc-empty',
          readAs
            ? `Read as ${readAs}, but no setting was found in it.`
            : 'The text is neither a postgresql.conf nor a name/value export.',
        ),
      );
      return;
    }
    const parts = [
      `Read as ${readAs}: ${parsed.entries.size} settings.`,
      `${rows.length} differ, ${matching} match,`,
      `${missing} calculated settings are not in the input,`,
      `${unknown} pasted settings this tool does not calculate.`,
    ];
    if (parsed.skipped) parts.push(`${parsed.skipped} lines skipped.`);
    const line = el('div', 'pc-diff-summary pc-hint', parts.join(' '));
    line.id = 'diff-summary';
    container.append(line);
    if (!rows.length) {
      container.append(
        el('div', 'pc-empty', 'No differences among the settings present on both sides.'),
      );
      return;
    }

    const scroll = el('div', 'pc-tablewrap');
    const table = el('table', 'pc-table');
    const head = el('thead');
    const headRow = el('tr');
    for (const column of ['Setting', 'Calculated', 'Yours', 'Apply', 'Context']) {
      headRow.append(el('th', null, column));
    }
    head.append(headRow);
    table.append(head);
    const body = el('tbody');
    for (const item of rows) {
      const row = el('tr');
      row.append(el('td', null, item.name));
      row.append(el('td', null, item.calculated));
      row.append(el('td', 'pc-diff-yours', item.yours));
      const apply = el('td');
      apply.append(el('span', `pc-apply pc-apply-${item.apply_mode}`, item.apply_mode));
      row.append(apply);
      row.append(el('td', 'pc-muted', item.context));
      body.append(row);
    }
    table.append(body);
    scroll.append(table);
    container.append(scroll);
  }

  renderArtifact(panel) {
    const artifact = {
      // See web/bin/pg-configurator.mjs: no canonical hash, so not v2.
      schema_version: 'pg_configurator/preview-v1',
      kind: 'PostgreSQLConfigurationPreview',
      generator: { name: 'pg-configurator-web', version: this.version },
      inputs: this.result.inputs,
      extensions: this.result.extensions,
      calculation: this.result.calculation,
      parameters: Object.fromEntries(
        Object.entries(this.result.parameters).map(([name, detail]) => [
          name,
          { ...detail, raw_value: plainValue(detail.raw_value) },
        ]),
      ),
      overrides: this.result.overrides,
      advisories: this.result.advisories,
      postgresql_conf: this.result.config,
    };
    const text = `${JSON.stringify(artifact, null, 2)}\n`;
    panel.append(
      el(
        'div',
        'pc-hint',
        'Generated by the web runtime. It carries no artifact_hash: the canonical hash belongs to the Python implementation.',
      ),
    );
    panel.append(this.copyBar(text, 'pg-configurator-artifact.json'));
    panel.append(el('pre', 'pc-code', text));
  }

  renderPatroni(panel) {
    const parameters = {};
    for (const [name, value] of Object.entries(this.result.config)) {
      parameters[name] = unquotePostgresqlConfValue(value);
    }
    parameters.max_replication_slots = String(
      Math.max(4, Math.trunc(Number(parameters.max_replication_slots))),
    );
    const text = `${JSON.stringify({ postgresql: { parameters } }, null, 2)}\n`;
    panel.append(this.copyBar(text, 'patroni.json'));
    panel.append(el('pre', 'pc-code', text));
  }

  // --------------------------------------------------------------- export

  copyBar(text, filename) {
    const bar = el('div', 'pc-toolbar');
    const copy = el('button', 'pc-btn', 'Copy');
    copy.type = 'button';
    copy.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(text);
        copy.textContent = 'Copied';
      } catch {
        // Clipboard access can be refused; selecting the block still works.
        copy.textContent = 'Select the text to copy';
      }
      setTimeout(() => {
        copy.textContent = 'Copy';
      }, 1500);
    });
    const download = el('button', 'pc-btn', 'Download');
    download.type = 'button';
    download.addEventListener('click', () => downloadText(text, filename));
    bar.append(copy, download);
    return bar;
  }

  inputDocument() {
    const inputs = {};
    for (const option of this.schema.options) {
      if (option.role !== 'calculation' || option.form_field !== true) continue;
      const value = this.values[option.dest];
      if (value === null || value === undefined || value === '') continue;
      inputs[option.dest] = value;
    }
    return { schema_version: 'pg_configurator/input-v1', inputs };
  }

  downloadInputDocument() {
    downloadText(
      `${JSON.stringify(this.inputDocument(), null, 2)}\n`,
      'pg-configurator-input.json',
    );
  }
}

const groupSlug = (title) => title.toLowerCase().replace(/\s+/g, '-');

const clamp = (value, low, high) => Math.min(Math.max(value, low), high);

const round2 = (value) => Math.round(value * 100) / 100;

const UNIT_BYTES = { Ki: 1024, Mi: 1024 ** 2, Gi: 1024 ** 3, Ti: 1024 ** 4 };

/**
 * Render a size in the largest IEC unit that divides it exactly.
 *
 * A slider counting in gibibytes reaches four figures long before its maximum,
 * and `4096Gi` is a worse way of writing `4Ti`. Only exact division promotes,
 * so nothing is rounded away to get a shorter string.
 */
function formatSize(numeric, unit) {
  const bytes = Math.round(numeric) * UNIT_BYTES[unit];
  let chosen = unit;
  for (const candidate of ['Ki', 'Mi', 'Gi', 'Ti']) {
    if (UNIT_BYTES[candidate] > UNIT_BYTES[chosen] && bytes % UNIT_BYTES[candidate] === 0) {
      chosen = candidate;
    }
  }
  return `${bytes / UNIT_BYTES[chosen]}${chosen}`;
}

/** Where the range input's thumb goes: an index when the slider has stops. */
function rangePosition(numeric, spec) {
  if (!spec.stops) return clamp(numeric, spec.min, spec.max);
  let best = 0;
  for (let index = 1; index < spec.stops.length; index += 1) {
    if (Math.abs(spec.stops[index] - numeric) < Math.abs(spec.stops[best] - numeric)) {
      best = index;
    }
  }
  return best;
}

/** Where a value sits on its slider, or null when it cannot be placed. */
function sliderPosition(value, spec) {
  if (value === null || value === undefined || value === '') return null;
  if (!spec.unit) {
    const numeric = Number(value);
    return Number.isFinite(numeric) ? numeric : null;
  }
  const match = /^\s*(\d+(?:\.\d+)?)\s*(Ki|Mi|Gi|Ti)?\s*$/.exec(String(value));
  if (match === null) return null;
  const bytes = Number(match[1]) * (UNIT_BYTES[match[2] ?? spec.unit] ?? 1);
  return bytes / UNIT_BYTES[spec.unit];
}

/** Render a slider position in the syntax the option expects. */
function formatSliderValue(numeric, spec, option) {
  if (spec.unit) return formatSize(numeric, spec.unit);
  if (option?.type === 'int') return Math.round(numeric);
  if (option?.type === 'float') return numeric;
  // db_cpu is typed as a string so it can carry millicores.
  return String(Number.isInteger(numeric) ? numeric : round2(numeric));
}

/** A typed readout keeps the option's own type, so the calculation sees it. */
function coerceReadout(raw, option) {
  if (option.type === 'int') return /^-?\d+$/.test(raw) ? Number(raw) : raw;
  if (option.type === 'float') {
    const parsed = Number(raw);
    return raw !== '' && Number.isFinite(parsed) ? parsed : raw;
  }
  return raw;
}

function plainValue(value) {
  return value instanceof PyNumber ? value.value : value;
}

function renderScalar(value) {
  if (value === null) return '';
  if (typeof value === 'number') return pyStr(Number.isInteger(value) ? value : value);
  if (value instanceof PyNumber) return pyStr(value);
  return String(value);
}

function downloadText(text, filename) {
  const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

export { numberOf };
