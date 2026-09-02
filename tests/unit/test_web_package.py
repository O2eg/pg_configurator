"""The JavaScript build has to be installable and importable as a module.

`web/src/` mirrors the Python reference module by module; it is not an API.
`web/index.mjs` is, and this suite is what keeps the difference honest: the
tarball is built, installed into an empty project, and imported by name. A
module left out of `files`, an `exports` map that does not resolve, or an entry
point that needs a file the tarball does not carry all fail here rather than in
somebody's build.

The packed contents are bounded on purpose. A package that ships the Python
source, the test suite and the built demo page is 1.6 MB of things an embedder
never loads, and every one of them is a file a consumer can come to depend on.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import pytest

from pg_configurator.configurator import PGConfigurator

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))


def npm(*arguments, cwd=ROOT):
    completed = subprocess.run(
        ["npm", *arguments],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "npm_config_update_notifier": "false"},
    )
    if completed.returncode != 0:
        raise AssertionError(f"npm {' '.join(arguments)} failed:\n{completed.stderr}")
    return completed.stdout


@pytest.mark.web
class TestPackageManifest(unittest.TestCase):
    def test_the_entry_points_exist(self):
        self.assertEqual("module", MANIFEST["type"])
        entries = [MANIFEST["main"], *MANIFEST["exports"].values(), *MANIFEST["bin"].values()]
        for entry in entries:
            with self.subTest(entry=entry):
                self.assertTrue((ROOT / entry).is_file(), f"{entry} is declared but missing")
        self.assertEqual("./web/index.mjs", MANIFEST["exports"]["."])

    def test_the_version_matches_the_python_package(self):
        namespace = {}
        exec((ROOT / "src" / "pg_configurator" / "version.py").read_text(), namespace)
        self.assertEqual(namespace["__version__"], MANIFEST["version"])


@pytest.mark.web
class TestPackedContents(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which("npm") is None:
            if os.environ.get("PGC_REQUIRE_NODE"):
                raise AssertionError("npm is required but was not found on PATH")
            raise unittest.SkipTest("npm is not installed; skipping the package suite")
        cls.listing = json.loads(npm("pack", "--dry-run", "--json"))[0]
        cls.files = sorted(entry["path"] for entry in cls.listing["files"])

    def test_the_tarball_carries_the_module_and_nothing_else(self):
        # Everything that is not the module: the reference implementation, the
        # suites, the exporter, and the demo page that Pages publishes.
        unwanted = [
            name
            for name in self.files
            if name.endswith(".py")
            or name.endswith(".html")
            or name.startswith("tests/")
            or name.startswith("web/test/")
            or name.startswith("web/tools/")
            or name.startswith("web/dist/")
            or name.startswith(".github/")
        ]
        self.assertEqual([], unwanted, "the tarball ships more than the module")
        self.assertLess(len(self.files), 25, self.files)
        self.assertLess(self.listing["unpackedSize"], 600_000)

    def test_every_shipped_module_can_resolve_its_imports(self):
        shipped = {name for name in self.files if name.endswith(".mjs") or name.endswith(".js")}
        for name in sorted(shipped):
            with self.subTest(module=name):
                source = (ROOT / name).read_text(encoding="utf-8")
                for line in source.splitlines():
                    if not line.startswith("import ") or " from '" not in line:
                        continue
                    target = line.split(" from '")[1].split("'")[0]
                    if not target.startswith("."):
                        continue  # node: builtins
                    resolved = (ROOT / name).parent / target
                    self.assertIn(
                        str(resolved.resolve().relative_to(ROOT)),
                        shipped,
                        f"{name} imports {target}, which the tarball does not carry",
                    )


@pytest.mark.web
class TestInstalledPackage(unittest.TestCase):
    """Build the tarball, install it into an empty project, import it by name."""

    @classmethod
    def setUpClass(cls):
        if shutil.which("npm") is None:
            if os.environ.get("PGC_REQUIRE_NODE"):
                raise AssertionError("npm is required but was not found on PATH")
            raise unittest.SkipTest("npm is not installed; skipping the package suite")
        cls._directory = tempfile.TemporaryDirectory()
        workspace = Path(cls._directory.name)
        tarball = workspace / "package.tgz"
        npm("pack", f"--pack-destination={workspace}")
        packed = next(workspace.glob("pg-configurator-web-*.tgz"))
        packed.rename(tarball)

        project = workspace / "consumer"
        project.mkdir()
        (project / "package.json").write_text(
            json.dumps({"name": "consumer", "version": "1.0.0", "type": "module", "private": True}),
            encoding="utf-8",
        )
        npm("install", "--no-audit", "--no-fund", str(tarball), cwd=project)
        cls.project = project

    @classmethod
    def tearDownClass(cls):
        cls._directory.cleanup()

    def run_node(self, script):
        completed = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=self.project,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        return completed.stdout

    def test_the_package_generates_a_configuration_by_name(self):
        output = self.run_node(
            """
            import { createConfigurator, SUPPORTED_MAJORS, renderConf }
              from 'pg-configurator-web';
            const pgc = await createConfigurator();
            const result = pgc.generate({ cpu_cores: '8', ram_value: '16Gi', pg_version: '18' });
            console.log(JSON.stringify({
              shared_buffers: result.config.shared_buffers,
              majors: SUPPORTED_MAJORS,
              advisories: result.advisories.length,
              severities: [...new Set(result.advisories.map((item) => item.severity))].sort(),
              conf: renderConf(result, { version: '0', host: 'h' }).split('\\n')[1],
              defaults: pgc.defaults.pg_version,
            }));
            """
        )
        answer = json.loads(output)
        self.assertTrue(answer["shared_buffers"].endswith("MB"))
        # The list is exported so a form can be built before any data is
        # loaded, which is exactly how it comes to disagree with the data.
        self.assertEqual(sorted(PGConfigurator.known_versions), sorted(answer["majors"]))
        self.assertGreater(answer["advisories"], 0)
        self.assertEqual(["assumption", "info"], answer["severities"])
        self.assertEqual("# PostgreSQL 18; host h", answer["conf"])
        self.assertEqual("18", answer["defaults"])

    def test_the_data_payloads_resolve_through_the_exports_map(self):
        # A bundler never calls loadBundledData: it resolves the JSON through
        # the exports map and hands the payloads to configuratorFromData, which
        # must then touch no file of its own. Resolution is done with
        # createRequire rather than an import assertion because the syntax for
        # those changed between the Node versions this has to run on.
        output = self.run_node(
            """
            import { createRequire } from 'node:module';
            import { readFileSync } from 'node:fs';
            import { configuratorFromData } from 'pg-configurator-web';
            const require = createRequire(`${process.cwd()}/`);
            const read = (name) => JSON.parse(
              readFileSync(require.resolve(`pg-configurator-web/data/${name}.json`), 'utf8'),
            ).payload;
            const pgc = configuratorFromData({
              rules: read('rules'), pgSettings: read('pg_settings'),
            });
            const result = pgc.generate({ cpu_cores: '8', ram_value: '16Gi', pg_version: '18' });
            console.log(result.config.shared_buffers);
            """
        )
        self.assertTrue(output.strip().endswith("MB"))

    def test_a_rejected_input_raises_the_reference_error(self):
        output = self.run_node(
            """
            import { createConfigurator, PyValueError } from 'pg-configurator-web';
            const pgc = await createConfigurator();
            try {
              pgc.generate({ cpu_cores: '0', ram_value: '16Gi', pg_version: '18' });
              console.log('accepted');
            } catch (error) {
              console.log(`${error instanceof PyValueError}|${error.message}`);
            }
            """
        )
        raised, _, message = output.strip().partition("|")
        self.assertEqual("true", raised)
        self.assertIn("cpu_cores", message)

    def test_the_bundled_command_line_is_installed(self):
        binary = self.project / "node_modules" / ".bin" / "pg-configurator-js"
        self.assertTrue(binary.exists(), "the bin entry did not install")
        completed = subprocess.run(
            [str(binary), "--db-cpu=8", "--db-ram=16Gi", "--pg-version=18"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("shared_buffers = ", completed.stdout)


if __name__ == "__main__":
    unittest.main()
