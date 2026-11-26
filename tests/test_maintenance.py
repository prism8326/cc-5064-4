import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tile_raster.maintenance import (
    BEGIN,
    END,
    check_documentation,
    compute_claims,
    extract_doc_claims,
    render_claims_block,
    update_documentation,
)


def _write_minimal_repo(root: Path, *, claims_text: str | None = None,
                        corrupt_claims: bool = False) -> None:
    (root / "docs").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    body = claims_text
    if body is None:
        claims = compute_claims()
        claims["mini_grid"]["covered_pixel_count"] = 1  # 故意写错
        body = render_claims_block(claims)
    if corrupt_claims:
        body = f"{BEGIN}\n```json\n{{not valid json,,}}\n```\n{END}"
    (root / "docs" / "tile-rules.md").write_text(
        "# 瓦片规则\n分箱 瓦片边界 片元归属 continuation rebin 净目录\n\n"
        + body + "\n", encoding="utf-8")
    (root / "README.md").write_text(
        "python3 -m tile_raster gui docs/gui.md tile-rules\n", encoding="utf-8")
    (root / "docs" / "gui.md").write_text(
        "运行状态 异常 覆盖图 计算负载 tile\n", encoding="utf-8")
    scripts = root / "scripts"
    scripts.mkdir()
    (scripts / "small_grid_reference.py").write_text("# stub\n")
    (scripts / "check_tile_rules.py").write_text("# stub\n")


class RealRepoMaintenanceTests(unittest.TestCase):
    def test_real_documentation_matches_code(self):
        issues = check_documentation(ROOT)
        failed = [i for i in issues if not i.ok and i.severity != "warn"]
        self.assertEqual([(i.check_id, i.msg) for i in failed], [])

    def test_doc_claims_extract_and_equal_canonical(self):
        text = (ROOT / "docs" / "tile-rules.md").read_text(encoding="utf-8")
        claims = extract_doc_claims(text)
        self.assertIsNotNone(claims)
        self.assertEqual(claims, compute_claims())


class DriftDetectionTests(unittest.TestCase):
    """维护校验不允许伪报：文档被改坏时必须 FAIL。"""

    def test_wrong_claims_block_fails(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _write_minimal_repo(root)
            issues = check_documentation(root)
            drift = [i for i in issues if i.check_id == "claims-match-code"]
            self.assertEqual(len(drift), 1)
            self.assertFalse(drift[0].ok)

    def test_corrupt_claims_json_fails(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _write_minimal_repo(root, corrupt_claims=True)
            issues = check_documentation(root)
            parse_issue = next(i for i in issues if i.check_id == "claims-parse")
            self.assertFalse(parse_issue.ok)

    def test_missing_required_term_fails(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _write_minimal_repo(root, claims_text=render_claims_block(compute_claims()))
            doc = root / "docs" / "tile-rules.md"
            doc.write_text(doc.read_text(encoding="utf-8").replace("片元归属", "XXXX"))
            issues = check_documentation(root)
            terms = next(i for i in issues if i.check_id == "tile-rules-terms")
            self.assertFalse(terms.ok)
            self.assertIn("片元归属", terms.actual)

    def test_update_then_check_passes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _write_minimal_repo(root)  # 内含错误 claims
            update_documentation(root)
            issues = check_documentation(root)
            failed = [i.check_id for i in issues if not i.ok]
            self.assertEqual(failed, [])

    def test_update_creates_missing_doc(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "docs").mkdir()
            update_documentation(root)
            doc = root / "docs" / "tile-rules.md"
            self.assertTrue(doc.is_file())
            self.assertIn(BEGIN, doc.read_text(encoding="utf-8"))


class MaintenanceSubprocessTests(unittest.TestCase):
    def test_maintenance_module_exit_zero_on_real_repo(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [sys.executable, "-m", "tile_raster.maintenance"],
            cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=300)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("MAINTAINED", proc.stdout)

    def test_maintenance_script_wrapper(self):
        wrapper = ROOT / "scripts" / "check_maintenance.py"
        if not wrapper.is_file():
            self.skipTest("check_maintenance.py 包装脚本不存在")
        proc = subprocess.run(
            [sys.executable, str(wrapper)], cwd=tempfile.mkdtemp(),
            capture_output=True, text=True, timeout=300)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("MAINTAINED", proc.stdout)


if __name__ == "__main__":
    unittest.main()
