#!/usr/bin/env python3
"""Headless regression for the built pg-configurator page.

The differential suites prove the calculation is right. This proves the page
that carries it actually runs: opened from ``file://``, with no network, it
must render a configuration, react to input, refuse bad input without going
blank, and never turn user text into markup.

Usage::

    PW_BROWSER=chromium python3 web/tools/browser-smoke.py

``PW_BROWSER`` selects the engine (chromium, firefox, webkit); ``PW_CHANNEL``
is passed through when a system browser should be used instead of a downloaded
build.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

WEB = pathlib.Path(__file__).resolve().parent.parent
PAGE = WEB / "dist" / "pg-configurator.html"


def normalized_inputs(page):
    """What the calculation used, read from the retained technical artifact."""

    active = page.locator("#tabs .pc-tab[aria-selected='true']").get_attribute("id")
    page.evaluate("document.getElementById('tab-artifact').click()")
    inputs = json.loads(page.locator("#panel-artifact .pc-code").inner_text())["inputs"]
    page.click(f"#{active}")
    return inputs


def generated_conf(page):
    """The postgresql.conf mapping of the current result, from the artifact tab."""

    active = page.locator("#tabs .pc-tab[aria-selected='true']").get_attribute("id")
    page.evaluate("document.getElementById('tab-artifact').click()")
    conf = json.loads(page.locator("#panel-artifact .pc-code").inner_text())["postgresql_conf"]
    page.click(f"#{active}")
    return conf


def conf_seconds(value: str) -> int:
    """A conf time such as 15min or 30s in seconds."""

    for suffix, factor in (("min", 60), ("ms", 0), ("h", 3600), ("s", 1), ("d", 86400)):
        if value.endswith(suffix):
            return int(value.removesuffix(suffix)) * factor
    return int(value)


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> int:
    if not PAGE.exists():
        fail(f"{PAGE} does not exist; run python3 web/build.py first")

    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        from playwright.sync_api import sync_playwright
    except ImportError:
        fail("playwright is not installed")

    engine = os.environ.get("PW_BROWSER", "chromium")
    channel = os.environ.get("PW_CHANNEL") or None
    checks = 0

    with sync_playwright() as playwright:
        launcher = getattr(playwright, engine)
        browser = launcher.launch(channel=channel) if channel else launcher.launch()
        context = browser.new_context()

        # A page that reaches the network is a bug: everything is inlined.
        requests: list[str] = []
        context.on(
            "request",
            lambda request: (
                requests.append(request.url) if not request.url.startswith("file://") else None
            ),
        )
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "console",
            lambda message: errors.append(message.text) if message.type == "error" else None,
        )

        page.goto(PAGE.as_uri())
        page.wait_for_selector("#panels .pc-form")

        def check(condition: bool, message: str) -> None:
            nonlocal checks
            checks += 1
            if not condition:
                fail(message)

        def open_group(selector: str) -> None:
            """Bring the input group that owns this control to the front.

            The form is a second row of tabs now, so most fields are in the DOM
            but not on screen. Every interaction below goes through here.
            """

            page.evaluate(
                """(selector) => {
                     const node = document.querySelector(selector);
                     const box = node && node.closest('.pc-groupbox');
                     if (box && box.hidden) {
                       document.getElementById(box.id.replace('subpanel-', 'subtab-')).click();
                     }
                   }""",
                selector,
            )

        def fill(selector: str, value: str) -> None:
            open_group(selector)
            page.fill(selector, value)

        def select(selector: str, value: str) -> None:
            open_group(selector)
            page.select_option(selector, value)

        def click_field(selector: str) -> None:
            open_group(selector)
            page.click(selector)

        def check_field(selector: str) -> None:
            open_group(selector)
            page.check(selector)

        def hover_field(selector: str) -> None:
            open_group(selector)
            page.hover(selector)

        def settles(selector: str, state: str) -> bool:
            """Wait for a state, and report a miss as a check rather than a traceback."""

            try:
                page.wait_for_selector(selector, state=state, timeout=2000)
            except PlaywrightTimeout:
                return False
            return True

        # --- the page renders a configuration on load ----------------------
        check(
            page.evaluate(
                """() => {
                     const icon = document.querySelector('link[rel="icon"]');
                     return Boolean(icon)
                       && icon.getAttribute('href').startsWith('data:image/svg+xml');
                   }"""
            ),
            "the PostgreSQL page icon is not embedded in the offline file",
        )
        check(
            page.locator("#tabs .pc-tab:visible > span:first-child").all_inner_texts()
            == ["Main", "Calculation", "Advisories", "Overrides", "Diff"],
            "the page-wide tabs are in an unexpected order",
        )
        check(not page.locator("#tab-artifact").is_visible(), "Artifact is visible to the user")
        check(
            page.locator(".pc-output-tabs .pc-output-tab > span:first-child").all_inner_texts()
            == ["postgresql.conf", "Settings", "Patroni"],
            "the Main output tabs are in an unexpected order",
        )
        check(
            page.locator("#main-output-tab-conf").get_attribute("aria-selected") == "true",
            "postgresql.conf is not the default Main output",
        )
        # A tab showing no number must read as centred. The count element is
        # always in the markup, and while it is empty it still holds the margin
        # that separates it from the label, which shifts the label left.
        uncounted = page.evaluate(
            """() => {
                 const rows = [];
                 const groups = ['#tabs .pc-tab', '.pc-output-tabs .pc-output-tab',
                                 '.pc-subtabs .pc-subtab'];
                 for (const selector of groups) {
                   for (const button of document.querySelectorAll(selector)) {
                     if (!button.offsetParent) continue;
                     const count = button.querySelector('.pc-count');
                     if (count && count.textContent !== '') continue;
                     const label = button.querySelector('span:first-child') || button;
                     const range = document.createRange();
                     range.selectNodeContents(label);
                     const box = button.getBoundingClientRect();
                     const text = range.getBoundingClientRect();
                     rows.push({ label: label.textContent,
                                 left: text.left - box.left,
                                 right: box.right - text.right });
                   }
                 }
                 return rows;
               }"""
        )
        check(len(uncounted) >= 8, f"too few tabs measured for centring: {len(uncounted)}")
        lopsided = [row for row in uncounted if abs(row["left"] - row["right"]) > 0.5]
        check(not lopsided, f"tab labels are not centred: {lopsided}")
        conf = page.locator("#main-output-panel-conf .pc-code").inner_text()
        check("shared_buffers = " in conf, "postgresql.conf has no shared_buffers")
        check(
            page.evaluate(
                "() => { const box = (selector) => document"
                "  .querySelector(`#main-output-panel-conf ${selector}`)"
                "  .getBoundingClientRect();"
                "  return Math.round(box('.pc-code').top - box('.pc-toolbar').bottom); }"
            )
            >= 8,
            "the copy and download buttons sit flush against the text below them",
        )
        check(conf.count("\n") > 100, "postgresql.conf looks too short")

        check(
            page.evaluate(
                "() => Math.round("
                "document.querySelector('#tabs').getBoundingClientRect().top"
                " - document.querySelector('.pc-top').getBoundingClientRect().bottom)"
            )
            >= 8,
            "the tab row sits flush against the header",
        )

        # The settings live beside the deployable outputs under the form.
        page.click("#main-output-tab-settings")
        rows = page.locator("#main-output-panel-settings tbody tr").count()
        check(rows > 100, f"settings table has only {rows} rows")
        check(
            page.locator("#main-output-panel-settings .pc-table thead th").all_inner_texts()
            == ["Setting", "Value", "Apply", "Source", "Context"],
            "the settings table has unexpected columns",
        )
        check(
            page.locator("#main-output-panel-settings .pc-table tbody tr")
            .first.locator("td")
            .count()
            == 5,
            "a settings row does not match the five visible columns",
        )
        check(
            page.locator("#main-output-panel-settings .pc-apply-restart").count() > 0,
            "no restart-class parameter is marked",
        )
        check(
            page.locator("#panel-main .pc-form").count() == 1,
            "the form left the Main tab",
        )
        check(
            page.locator("#actions .pc-btn").count() == 2,
            "the page-wide buttons are not in the tab row",
        )
        page.evaluate("document.getElementById('tab-artifact').click()")
        artifact = json.loads(page.locator("#panel-artifact .pc-code").inner_text())
        parameter_detail = next(iter(artifact["parameters"].values()))
        check("rule" in parameter_detail, "the hidden rule data was removed from the artifact")
        check(
            "rule_kind" in parameter_detail,
            "the hidden rule kind was removed from the artifact",
        )
        page.click("#tab-main")

        # --- one input group at a time ---------------------------------------
        order = page.locator(".pc-subtab").all_inner_texts()
        check(
            order == ["Hardware", "Workload", "Memory budget", "Connections", "Replication", "WAL"],
            f"the input groups are in an unexpected order: {order}",
        )
        check(
            page.locator(".pc-groupbox:visible").count() == 1,
            "more than one input group is on screen at once",
        )
        check(
            page.locator("#subpanel-hardware .pc-field").count() == 5,
            "the Hardware group lost fields",
        )
        page.click("#subtab-wal")
        check(
            page.locator("#subpanel-wal:visible").count() == 1
            and page.locator("#subpanel-hardware:visible").count() == 0,
            "choosing an input group did not swap the panel",
        )
        # Reset rebuilds the form; it must not throw the reader back to the first
        # group while they are working in another.
        page.click("text=Reset to defaults")
        check(
            page.locator("#subpanel-wal:visible").count() == 1,
            "resetting jumped back to the first input group",
        )
        page.click("#subtab-hardware")

        def field_geometry():
            """Shared grid columns, label tracks and control widths."""

            return page.evaluate(
                """() => {
                     const box = document.querySelector('.pc-groupbox:not([hidden])');
                     const fields = [
                       ...box.querySelectorAll('.pc-field:not(.pc-field-profiles)')];
                     const controls = [];
                     const gaps = [];
                     const offsets = [];
                     const wrappedLabels = [];
                     for (const field of box.querySelectorAll('.pc-field')) {
                       const label = field.querySelector(':scope > label');
                       // The profile picker is a row of its own under the name,
                       // by design; it has no name-to-value gap to measure.
                       const value = field.querySelector(
                         ':scope > .pc-control, :scope > input, :scope > select');
                       if (!value) continue;
                       const text = document.createRange();
                       text.selectNodeContents(label);
                       if (text.getClientRects().length > 1) wrappedLabels.push(label.textContent);
                       const fieldRect = field.getBoundingClientRect();
                       const labelRect = label.getBoundingClientRect();
                       const valueRect = value.getBoundingClientRect();
                       controls.push(Math.round(valueRect.width));
                       gaps.push(Math.round(valueRect.left - labelRect.right));
                       offsets.push(Math.round(valueRect.left - fieldRect.left));
                     }
                     const fieldWidths = fields
                       .map(node => Math.round(node.getBoundingClientRect().width));
                     const columns = fields
                       .map(node => Math.round(node.getBoundingClientRect().left));
                     return {
                       display: getComputedStyle(box.querySelector('.pc-fields')).display,
                       columns: [...new Set(columns)],
                       controlWidths: [...new Set(controls)],
                       fieldWidths: [...new Set(fieldWidths)],
                       gaps: [...new Set(gaps)],
                       offsets: [...new Set(offsets)],
                       wrappedLabels,
                     };
                   }"""
            )

        # CSS columns balance each side independently, so one wrapped name used
        # to push only half the form down. A real grid gives both halves shared
        # rows. Within either half, every name and every control uses the same
        # guides, regardless of whether the value is a slider, input or select.
        for viewport in (1500, 1150, 1024, 920, 860):
            page.set_viewport_size({"width": viewport, "height": 900})
            for slug in (
                "hardware",
                "workload",
                "memory-budget",
                "connections",
                "replication",
                "wal",
            ):
                page.click(f"#subtab-{slug}")
                geometry = field_geometry()
                check(
                    geometry["display"] == "grid",
                    f"{slug} at {viewport}px is not a grid",
                )
                check(
                    len(geometry["columns"]) == 2,
                    f"{slug} at {viewport}px has {len(geometry['columns'])} columns",
                )
                check(
                    len(geometry["fieldWidths"]) == 1,
                    f"{slug} at {viewport}px has field widths {geometry['fieldWidths']}",
                )
                check(
                    len(geometry["controlWidths"]) == 1,
                    f"{slug} at {viewport}px has control widths {geometry['controlWidths']}",
                )
                check(
                    geometry["gaps"] == [12],
                    f"{slug} at {viewport}px puts {geometry['gaps']}px between name and value",
                )
                check(
                    geometry["offsets"] == [222],
                    f"{slug} at {viewport}px starts controls at {geometry['offsets']}px",
                )
                check(
                    geometry["wrappedLabels"] == [],
                    f"{slug} at {viewport}px wraps labels {geometry['wrappedLabels']}",
                )

        # When two useful columns no longer fit, the label moves above the
        # value and both use the full row. There is no intermediate centred
        # side-label layout that would leave most of the space on the left.
        for viewport in (800, 640):
            page.set_viewport_size({"width": viewport, "height": 900})
            page.click("#subtab-hardware")
            stacked = page.evaluate(
                """() => {
                     const fields = document.querySelector(
                       '#subpanel-hardware .pc-fields');
                     const field = fields.querySelector('.pc-field[data-dest="db_cpu"]');
                     const box = (selector) =>
                       field.querySelector(selector).getBoundingClientRect();
                     const label = box(':scope > label');
                     const value = box(':scope > .pc-control');
                     const outer = field.getBoundingClientRect();
                     return {
                       columns: getComputedStyle(fields).gridTemplateColumns.split(' ').length,
                       below: Math.round(value.top) > Math.round(label.bottom),
                       sameLeft: Math.round(value.left) === Math.round(outer.left),
                       sameWidth: Math.round(value.width) === Math.round(outer.width),
                     };
                   }"""
            )
            check(stacked["columns"] == 1, f"the form at {viewport}px is not one column")
            check(stacked["below"], f"the control at {viewport}px is not below its label")
            check(
                stacked["sameLeft"],
                f"the control at {viewport}px does not start at the field edge",
            )
            check(
                stacked["sameWidth"],
                f"the control at {viewport}px does not fill its field",
            )

        page.set_viewport_size({"width": 1280, "height": 900})
        page.click("#subtab-hardware")
        check(
            page.evaluate(
                "() => document.querySelector('#actions').getBoundingClientRect().right"
                " >= document.querySelector('#tabs').getBoundingClientRect().right"
            ),
            "the buttons are not aligned to the right of the tab row",
        )
        check(
            page.locator("#panels .pc-hint").count() == 0
            or "sent anywhere" not in page.locator("#panels").inner_text(),
            "the removed reassurance is still on the page",
        )
        check(page.locator("#tab-parameters").count() == 0, "a separate Parameters tab is back")

        # The table is rebuilt on every keystroke in the form, so the filter has
        # to be state, not markup.
        page.fill("#main-output-panel-settings .pc-filter input", "wal_")
        fill("#readout-db_ram", "24Gi")
        check(
            page.input_value("#main-output-panel-settings .pc-filter input") == "wal_",
            "the settings filter was cleared by a recalculation",
        )
        visible = page.locator("#main-output-panel-settings tbody tr:visible").count()
        check(0 < visible < rows, f"the filter matched {visible} of {rows} rows")
        page.fill("#main-output-panel-settings .pc-filter input", "")
        fill("#readout-db_ram", "16Gi")

        # --- the page draws its own tooltips ---------------------------------
        check(page.locator(".pc-tip").count() == 1, "no tooltip element on the page")
        check(not page.locator(".pc-tip").is_visible(), "the tooltip starts visible")
        hover_field("label[for='field-shared_buffers_part']")
        check(settles(".pc-tip", "visible"), "hovering an input label showed no tooltip")
        tip = page.locator(".pc-tip").inner_text()
        check("shared_buffers" in tip, f"unexpected tooltip text: {tip[:60]}")
        check(len(tip) > 120, "the tooltip is no longer than a CLI help line")
        check(
            page.locator("label[for='field-shared_buffers_part']").get_attribute("title") is None,
            "the native title bubble is still attached",
        )
        page.hover("h1.pc-title")
        check(settles(".pc-tip", "hidden"), "the tooltip stayed up after the pointer left")

        # --- an empty optional field says what is used instead ----------------
        check(
            page.get_attribute("#readout-disk_score", "placeholder") == "75",
            "disk-score does not show the score its disk type implies",
        )
        select("#field-db_disk_type", "SAS")
        check(
            page.get_attribute("#readout-disk_score", "placeholder") == "30",
            "the derived disk score did not follow the disk type",
        )
        check(
            page.input_value("#field-disk_score") == "30",
            "the slider did not move to the score in force",
        )
        select("#field-db_disk_type", "SSD")
        check(
            page.get_attribute("#readout-db_size", "placeholder") == "\u2014",
            "db-size does not mark itself as carrying no value",
        )
        # An empty optional field must not point its slider at a value it does
        # not hold: a range whose value is never set keeps a midpoint that a
        # later, smaller maximum clamps — db-size used to sit on 100Ti.
        check(
            page.input_value("#field-db_size") == page.get_attribute("#field-db_size", "min"),
            "the empty db-size slider is parked away from its low end",
        )
        check(
            page.input_value("#field-disk_score") == "75",
            "disk-score is not parked on the score in force",
        )
        check(
            page.get_attribute("#readout-peak_wal_rate", "placeholder") == "4Mi",
            "an empty peak WAL rate does not show the assumed rate",
        )
        check(
            page.input_value("#field-peak_wal_rate") == "4",
            "the empty peak WAL slider is not parked on the assumed rate",
        )
        fill("#readout-peak_wal_rate", "8Mi")
        check(not page.locator("#error").is_visible(), "an explicit peak WAL rate was rejected")
        check(
            normalized_inputs(page)["peak_wal_rate_source"] == "explicit",
            "an explicit peak WAL rate was recorded as an assumption",
        )
        fill("#readout-peak_wal_rate", "")
        check(not page.locator("#error").is_visible(), "clearing peak WAL rate broke calculation")
        check(
            page.get_attribute("#readout-peak_wal_rate", "placeholder") == "4Mi",
            "clearing peak WAL rate did not restore the assumed value",
        )
        check(
            normalized_inputs(page)["peak_wal_rate_source"] == "default",
            "a cleared peak WAL rate was not recorded as an assumption",
        )

        # Profile names are the value of conf-profiles, so the list uses the
        # same value guide as neighbouring controls. At compact desktop widths
        # it takes a full row so the long names are not clipped.
        page.click("#subtab-workload")
        wide_profiles = page.evaluate(
            """() => {
                 const field = document.querySelector(
                   '.pc-field[data-dest="conf_profiles"]');
                 const label = field.querySelector(':scope > label')
                   .getBoundingClientRect();
                 const choices = field.querySelector('.pc-choices')
                   .getBoundingClientRect();
                 const version = document.querySelector('#field-pg_version')
                   .getBoundingClientRect();
                 return {
                   sameGuide: Math.round(choices.left) === Math.round(version.left),
                   gap: Math.round(choices.left - label.right),
                 };
               }"""
        )
        check(wide_profiles["sameGuide"], "conf-profiles misses the wide value guide")
        check(wide_profiles["gap"] == 12, "conf-profiles has a different label/value gap")

        page.set_viewport_size({"width": 858, "height": 900})
        compact_profiles = page.evaluate(
            """() => {
                 const field = document.querySelector(
                   '.pc-field[data-dest="conf_profiles"]');
                 const choices = field.querySelector('.pc-choices').getBoundingClientRect();
                 const duty = document.querySelector('#field-db_duty').getBoundingClientRect();
                 const box = field.closest('.pc-groupbox').getBoundingClientRect();
                 return {
                   sameGuide: Math.round(choices.left) === Math.round(duty.left),
                   fullRow: Math.round(field.getBoundingClientRect().width)
                     > Math.round(duty.width),
                   contained: Math.round(choices.right) <= Math.round(box.right),
                 };
               }"""
        )
        check(compact_profiles["sameGuide"], "conf-profiles misses the compact value guide")
        check(compact_profiles["fullRow"], "conf-profiles has no room for long profile names")
        check(compact_profiles["contained"], "conf-profiles overflows its panel")

        page.set_viewport_size({"width": 640, "height": 900})
        mobile_profiles = page.evaluate(
            """() => {
                 const field = document.querySelector(
                   '.pc-field[data-dest="conf_profiles"]');
                 const label = field.querySelector(':scope > label').getBoundingClientRect();
                 const choices = field.querySelector('.pc-choices').getBoundingClientRect();
                 return {
                   below: Math.round(choices.top) > Math.round(label.bottom),
                   sameLeft: Math.round(choices.left)
                     === Math.round(field.getBoundingClientRect().left),
                 };
               }"""
        )
        check(mobile_profiles["below"], "mobile profiles are not below their label")
        check(mobile_profiles["sameLeft"], "mobile profiles miss the field edge")
        page.set_viewport_size({"width": 1280, "height": 900})
        page.click("#subtab-hardware")

        # A readout is 8ch wide; anything longer is delivered as a cut-off word.
        cut = page.evaluate(
            """() => {
                 const probe = document.createElement('span');
                 probe.style.cssText = 'position:fixed;visibility:hidden;white-space:pre;';
                 document.body.append(probe);
                 const bad = [];
                 for (const node of document.querySelectorAll('.pc-readout[placeholder]')) {
                   const cs = getComputedStyle(node);
                   probe.style.font = cs.font;
                   probe.textContent = node.placeholder;
                   // Hidden group panels have a zero bounding box, but their
                   // fixed CSS width is still the width they get when opened.
                   const inner = parseFloat(cs.width)
                     - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight)
                     - parseFloat(cs.borderLeftWidth) - parseFloat(cs.borderRightWidth);
                   if (probe.getBoundingClientRect().width > inner) bad.push(node.placeholder);
                 }
                 probe.remove();
                 return bad;
               }"""
        )
        check(cut == [], f"these placeholders do not fit their box: {cut}")

        # --- sizes read in the unit that fits them ---------------------------
        def drag(dest, position):
            page.evaluate(
                "([id, value]) => { const r = document.getElementById(id);"
                " r.value = String(value);"
                " r.dispatchEvent(new Event('input', { bubbles: true })); }",
                [f"field-{dest}", position],
            )

        stops = []
        for position in range(int(page.get_attribute("#field-db_size", "max")) + 1):
            drag("db_size", position)
            stops.append(page.input_value("#readout-db_size"))
        check(stops[0] == "10Gi", f"db-size starts at {stops[0]}")
        check(stops[-1] == "100Ti", f"db-size stops at {stops[-1]}, not 100Ti")
        check(
            not any(value.endswith("Gi") and int(value[:-2]) >= 1024 for value in stops),
            f"a whole tebibyte was written in gibibytes: {stops}",
        )
        check(len(set(stops)) == len(stops), f"the size slider repeats itself: {stops}")
        check(not page.locator("#error").is_visible(), "the largest database size was refused")

        drag("db_ram", 1024)
        check(
            page.input_value("#readout-db_ram") == "1Ti",
            "1024 gibibytes of RAM is not written as 1Ti",
        )
        # 1536Mi is a size and a half; promoting it would trade an exact number
        # for a shorter one, so the unit must stay put.
        drag("reserved_system_ram", 1536)
        check(
            page.input_value("#readout-reserved_system_ram") == "1536Mi",
            "a size that no larger unit divides was promoted anyway: "
            + page.input_value("#readout-reserved_system_ram"),
        )
        drag("reserved_system_ram", 2048)
        check(
            page.input_value("#readout-reserved_system_ram") == "2Gi",
            "2048 mebibytes is not written as 2Gi",
        )
        fill("#readout-reserved_system_ram", "256Mi")
        fill("#readout-db_ram", "16Gi")
        fill("#readout-db_size", "")

        page.click("#tab-calculation")
        check(
            page.locator("#panel-calculation tbody tr").count() >= 40,
            "calculation table is short",
        )
        calculation_geometry = page.evaluate(
            """() => {
                 const intro = document.querySelector('#panel-calculation .pc-table-intro');
                 const table = document.querySelector('#panel-calculation .pc-calculation-table');
                 const headers = [...table.querySelectorAll('thead th')]
                   .map(node => node.getBoundingClientRect());
                 const cells = [...table.querySelectorAll('tbody tr:first-child td')]
                   .map(node => node.getBoundingClientRect());
                 return {
                   introGap: Math.round(table.closest('.pc-tablewrap').getBoundingClientRect().top
                     - intro.getBoundingClientRect().bottom),
                   valueAligned: Math.abs(headers[1].right - cells[1].right) < 1,
                   sizeAligned: Math.abs(headers[2].right - cells[2].right) < 1,
                   numericTracksEqual: Math.abs(headers[1].width - headers[2].width) < 1,
                   descriptionRatio: headers[0].width / headers[1].width,
                   valueHeadingAlign: getComputedStyle(table.querySelector('thead th:nth-child(2)'))
                     .textAlign,
                   sizeHeadingAlign: getComputedStyle(table.querySelector('thead th:nth-child(3)'))
                     .textAlign,
                 };
               }"""
        )
        check(calculation_geometry["introGap"] >= 8, "calculation intro has no lower gap")
        check(calculation_geometry["valueAligned"], "Value is not above its numbers")
        check(calculation_geometry["sizeAligned"], "Size is not above its values")
        check(
            calculation_geometry["numericTracksEqual"],
            "Value and Size use different track widths",
        )
        check(
            2.9 <= calculation_geometry["descriptionRatio"] <= 3.1,
            "calculation columns do not use the 60/20/20 layout",
        )
        check(
            calculation_geometry["valueHeadingAlign"] == "right"
            and calculation_geometry["sizeHeadingAlign"] == "right",
            "numeric calculation headings are not right-aligned",
        )

        # The panel groups by severity and each entry names the setting it is
        # about, so a reader can tell a conflict from a note without reading
        # every sentence. A default configuration has no warnings at all; the
        # badge stays empty rather than counting notes.
        page.click("#tab-advisories")
        check(page.locator("#panel-advisories li").count() > 0, "no advisories rendered")
        headings = page.locator("#panel-advisories .pc-advisory-heading").all_inner_texts()
        check(
            headings and all("(" in heading for heading in headings),
            f"advisories are not grouped and counted by severity: {headings}",
        )
        check(
            [heading for heading in headings if heading.startswith("WARNINGS")] == [],
            f"a default configuration reports warnings: {headings}",
        )
        check(
            page.locator("#tab-advisories .pc-count").inner_text() == "",
            "the Advisories badge counts notes rather than warnings",
        )
        check(
            page.locator("#panel-advisories .pc-advisory-code").count()
            == page.locator("#panel-advisories li").count(),
            "an advisory was rendered without its stable code",
        )
        severity_borders = page.evaluate(
            """() => Object.fromEntries(
                 ['warning', 'assumption', 'info'].map((severity) => {
                   const entry = document.querySelector(`.pc-advisory-${severity}`);
                   if (!entry) return [severity, null];
                   const style = getComputedStyle(entry);
                   return [severity,
                           `${style.borderLeftWidth} ${style.borderLeftColor}`];
                 }))"""
        )
        drawn = [value for value in severity_borders.values() if value is not None]
        check(
            len(drawn) >= 2 and len(set(drawn)) == len(drawn),
            f"severities are not told apart by their left edge: {severity_borders}",
        )
        check(
            all(value.startswith("3px") for value in drawn),
            f"the severity edge lost to the list border: {severity_borders}",
        )

        # --- input changes recalculate -------------------------------------
        page.click("#tab-main")
        select("#field-pg_version", "9.6")
        page.click("#main-output-tab-conf")
        conf_96 = page.locator("#main-output-panel-conf .pc-code").inner_text()
        check("# PostgreSQL 9.6" in conf_96, "the version change did not reach the output")
        check(conf_96 != conf, "the configuration did not change with the major version")

        page.click("#tab-main")
        select("#field-pg_version", "18")
        fill("#readout-db_ram", "128Gi")
        page.click("#main-output-tab-conf")
        conf_big = page.locator("#main-output-panel-conf .pc-code").inner_text()
        check(conf_big != conf, "a RAM change did not change the configuration")

        # --- invalid input reports without blanking the page ---------------
        page.click("#tab-main")

        def form_top():
            return page.evaluate(
                "() => Math.round("
                "document.querySelector('#panel-main .pc-section').getBoundingClientRect().top)"
            )

        settled = form_top()
        fill("#readout-db_ram", "32Zi")
        check(page.locator("#error").is_visible(), "an invalid size produced no error")
        # The slot is reserved: a message may not shove the page around.
        check(
            form_top() == settled,
            f"the settings moved by {form_top() - settled}px when the message appeared",
        )
        error_text = page.locator("#error").inner_text()
        check("Unknown size unit" in error_text, f"unexpected error text: {error_text}")
        check(
            page.evaluate(
                "() => Math.round("
                "document.querySelector('#panel-main .pc-section').getBoundingClientRect().top"
                " - document.querySelector('#error').getBoundingClientRect().bottom)"
            )
            >= 8,
            "the message sits flush against the settings below it",
        )
        # A result the current input did not produce must not stay on offer.
        check(
            page.locator("#main-output-panel-conf .pc-code").count() == 0,
            "postgresql.conf survived invalid input",
        )
        page.click("#main-output-tab-settings")
        check(
            page.locator("#main-output-panel-settings tbody tr").count() == 0,
            "the settings table survived invalid input",
        )
        check(
            page.locator("#panel-main .pc-form").count() == 1,
            "hiding the stale settings took the form with it",
        )
        # The explanatory tabs keep the last good answer: blanking them would
        # remove the context needed to understand what went wrong.
        page.click("#tab-calculation")
        check(
            page.locator("#panel-calculation tbody tr").count() > 0,
            "the calculation was blanked as well",
        )

        page.click("#tab-main")
        fill("#readout-db_ram", "32Gi")
        check(not page.locator("#error").is_visible(), "the error stayed after a fix")
        check(
            form_top() == settled,
            f"the settings moved by {form_top() - settled}px when the message went away",
        )
        check(
            page.locator("#main-output-panel-settings tbody tr").count() > 100,
            "the settings table did not come back after a fix",
        )
        page.click("#main-output-tab-conf")
        check(
            "shared_buffers = " in page.locator("#main-output-panel-conf .pc-code").inner_text(),
            "postgresql.conf did not come back after a fix",
        )
        page.click("#tab-main")

        # --- hostile text stays text ---------------------------------------
        page.click("#tab-main")
        fill("#field-synchronous_standby_names", "<img src=x onerror=alert(1)>")
        check(
            page.locator("#panel-main img").count() == 0,
            "user text was turned into markup",
        )
        fill("#field-synchronous_standby_names", "")
        fill("#field-synchronous_standby_names", "standby'one")
        check(not page.locator("#error").is_visible(), "a quoted standby name was rejected")
        check(
            generated_conf(page)["synchronous_standby_names"] == "'standby''one'",
            "a quote in synchronous_standby_names was not escaped for postgresql.conf",
        )
        fill("#field-synchronous_standby_names", "")

        # --- theme: dark by default, like the pg_diag report -----------------
        check(
            page.get_attribute("html", "data-pv-theme") == "dark",
            "the page does not open in the report's default scheme",
        )
        check(
            page.eval_on_selector("html", "el => getComputedStyle(el).colorScheme") == "dark",
            "color-scheme is not set, so native controls will not follow the theme",
        )
        page.click("#tab-main")
        page.check("#theme")
        check(
            page.get_attribute("html", "data-pv-theme") == "light",
            "the theme toggle did not switch to light",
        )
        page.uncheck("#theme")
        check(
            page.get_attribute("html", "data-pv-theme") == "dark",
            "the theme toggle did not switch back",
        )

        # --- sliders: bounded fields are dragged, not typed -------------------
        page.click("#tab-main")
        check(page.locator(".pc-range").count() >= 20, "the bounded fields are not sliders")

        def drag(dest, value):
            page.eval_on_selector(
                f"#field-{dest}",
                f"el => {{ el.value = '{value}'; "
                "el.dispatchEvent(new Event('input', {bubbles: true})); }",
            )

        # A pair that must sum to 1.0 is kept summing to 1.0 by the control.
        drag("autovacuum_workers_mem_part", "0.7")
        check(
            page.input_value("#readout-maintenance_conns_mem_part") == "0.3",
            "the complementary share was not kept in step",
        )
        check(not page.locator("#error").is_visible(), "a coupled pair produced an error")
        drag("autovacuum_workers_mem_part", "0.5")

        # A min/max pair cannot be crossed.
        drag("min_conns", "800")
        check(
            int(page.input_value("#readout-max_conns")) >= 800,
            "the maximum was not pushed along with the minimum",
        )
        check(not page.locator("#error").is_visible(), "a min/max pair produced an error")
        drag("min_conns", "20")
        fill("#readout-max_conns", "500")

        # The slider's ends are a comfort, not a limit: bigger is still typeable.
        fill("#readout-db_ram", "512Gi")
        fill("#readout-db_cpu", "256")
        check(
            normalized_inputs(page)["cpu_cores"] == 256,
            "a value beyond the slider's end could not be entered",
        )
        fill("#readout-db_cpu", "500m")
        check(
            normalized_inputs(page)["cpu_cores"] == 0.5,
            "millicores are no longer accepted",
        )
        fill("#readout-db_cpu", "4")
        fill("#readout-db_ram", "16Gi")

        check(
            "this page cannot" not in page.locator("#panels").inner_text(),
            "the form still explains itself instead of just working",
        )

        # --- the form offers nothing that cannot change the answer -----------
        check(
            page.locator(".pc-subtab", has_text="Extensions").count() == 0,
            "the form still offers an input that changes no generated setting",
        )

        # --- a profile produces overrides, and the tab renders them ----------
        page.click("#tab-main")
        check(
            page.locator("#field-conf_profiles").count() == 0,
            "profiles are still a name the user has to know and type",
        )
        check_field("#field-profile-profile_backend_common")
        check_field("#field-profile-profile_backend_perf")
        page.click("#tab-overrides")
        override_rows = page.locator("#panel-overrides tbody tr").count()
        check(override_rows > 0, "selecting profiles produced no overrides")
        check(
            page.locator("#panel-overrides .pc-tablewrap").count() == 1,
            "the overrides table is not wrapped the way the other tables are",
        )
        override_headers = page.locator("#panel-overrides thead th").all_inner_texts()
        check(
            override_headers == ["Setting", "From", "Value From", "To", "Value To"],
            f"the overrides table does not explain values before and after: {override_headers}",
        )
        naptime_cells = (
            page.locator("#panel-overrides tbody tr")
            .filter(has_text="autovacuum_naptime")
            .locator("td")
            .all_inner_texts()
        )
        check(
            naptime_cells == ["autovacuum_naptime", "base", "30s", "profile_backend_perf", "15s"],
            f"the overrides table does not show the naptime recalculation: {naptime_cells}",
        )
        page.click("#tab-main")

        # --- a pasted configuration is compared after unit conversion --------
        conf = generated_conf(page)
        shared_kb = int(conf["shared_buffers"].removesuffix("MB")) * 1024
        page.click("#tab-diff")
        check(page.locator("#panel-diff #diff-input").count() == 1, "the Diff tab has no paste box")
        check(
            "Nothing pasted" in page.locator("#diff-results").inner_text(),
            "an empty Diff tab does not say what to do",
        )
        page.fill(
            "#diff-input",
            "\n".join(
                [
                    "# a comment line",
                    f"shared_buffers = {shared_kb}kB  # same value in another unit",
                    "work_mem = '1GB'",
                    "include 'extra.conf'",
                    "some_unknown_setting = 1",
                    f"random_page_cost = {conf['random_page_cost']}",
                ]
            ),
        )
        diff_rows = page.locator("#diff-results tbody tr")
        check(
            diff_rows.count() == 1 and "work_mem" in diff_rows.first.inner_text(),
            "the Diff table does not list exactly the setting that differs",
        )
        check(
            page.locator("#diff-results tbody tr").first.locator(".pc-apply").count() == 1,
            "a differing setting does not say what applying it costs",
        )
        summary = page.locator("#diff-summary").inner_text()
        for fragment in (
            "Read as postgresql.conf",
            "1 differ, 2 match",
            "1 pasted settings this tool does not calculate",
            "1 lines skipped",
        ):
            check(fragment in summary, f"the Diff summary lacks '{fragment}': {summary}")
        check(
            page.locator("#tab-diff .pc-count").inner_text() == "1",
            "the Diff badge does not count the differences",
        )

        # pg_settings export with a header: bare numbers carry the unit column.
        page.fill(
            "#diff-input",
            "\n".join(
                [
                    "name,setting,unit",
                    f"shared_buffers,{shared_kb // 8},8kB",
                    f"checkpoint_timeout,{conf_seconds(conf['checkpoint_timeout'])},s",
                    "work_mem,1048576,kB",
                ]
            ),
        )
        summary = page.locator("#diff-summary").inner_text()
        check(
            "Read as CSV with a header row" in summary and "1 differ, 2 match" in summary,
            f"a pg_settings export with a header is not read through its unit column: {summary}",
        )

        # Without a header a bare number is in the setting's own unit.
        page.fill("#diff-input", f"shared_buffers,{shared_kb // 8}\nwork_mem,1048576\n")
        summary = page.locator("#diff-summary").inner_text()
        check(
            "Read as CSV without a header row" in summary and "1 differ, 1 match" in summary,
            f"a headerless export is not read in the snapshot's units: {summary}",
        )
        page.fill("#diff-input", "")
        check(
            "Nothing pasted" in page.locator("#diff-results").inner_text(),
            "clearing the paste box does not clear the comparison",
        )
        check(
            page.locator("#tab-diff .pc-count").inner_text() == "",
            "the Diff badge survives clearing the paste box",
        )
        page.click("#tab-main")

        # An exclusive profile is enforced by the control, not by an error.
        # `click`, not `check`: `check` insists on the final state and would
        # time out on a regression instead of reporting it.
        click_field("#field-profile-profile_1c")
        check(
            page.is_checked("#field-profile-profile_1c"),
            "an exclusive profile could not be selected",
        )
        check(
            not page.is_checked("#field-profile-profile_backend_perf"),
            "an exclusive profile did not clear the others",
        )
        check(
            page.is_disabled("#field-profile-ext_perf"),
            "an exclusive profile left the others selectable",
        )
        check(
            not page.locator("#error").is_visible(),
            f"the exclusive profile produced an error: {page.locator('#error').inner_text()}",
        )
        click_field("#field-profile-profile_1c")
        check(
            not page.is_disabled("#field-profile-ext_perf"),
            "the others stayed locked after the exclusive profile was cleared",
        )

        # --- the form never claims a value the calculation is not using -------
        page.click("#tab-main")
        check(
            page.input_value("#field-replication_mode") == "physical",
            "the form does not show the replication mode that is actually in force",
        )
        check(
            normalized_inputs(page)["replication_mode"] == "physical",
            "the resolved replication mode is not what the calculation used",
        )
        select("#field-replication_mode", "logical")
        check(
            normalized_inputs(page)["replication_mode"] == "logical",
            "an explicit replication mode did not reach the calculation",
        )
        select("#field-replication_mode", "physical")

        # --- output tabs -----------------------------------------------------
        check(page.locator("#tabs .pc-tab:visible").count() == 5, "unexpected number of page tabs")
        check(page.locator("#tabs .pc-tab").count() == 6, "the retained Artifact tab was removed")
        check(
            page.locator(".pc-output-tabs .pc-output-tab").count() == 3,
            "unexpected number of Main output tabs",
        )
        check(page.locator("#tab-conf").count() == 0, "postgresql.conf is still a page tab")
        check(page.locator("#tab-patroni").count() == 0, "Patroni is still a page tab")
        check(
            page.locator("#tabs .pc-tab-finding").count() == 1,
            "the findings tab lost its accent outline",
        )

        check(
            "input-v1" not in page.locator("#panels").inner_text(),
            "a schema version name is still shown to the user as a label",
        )

        page.evaluate("document.getElementById('tab-artifact').click()")
        artifact = json.loads(page.locator("#panel-artifact .pc-code").inner_text())
        check(
            artifact["generator"]["name"] == "pg-configurator-web",
            "the artifact does not name the web runtime",
        )
        check("artifact_hash" not in artifact, "the web artifact claims a canonical hash")

        page.click("#tab-main")
        page.click("#main-output-tab-patroni")
        patroni = json.loads(page.locator("#main-output-panel-patroni .pc-code").inner_text())
        check("postgresql" in patroni, "the Patroni document has no postgresql key")

        check(not errors, f"the page reported errors: {errors[:3]}")
        check(not requests, f"the page made network requests: {requests[:3]}")

        context.close()
        browser.close()

    print(f"browser-smoke ({engine}): {checks} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
