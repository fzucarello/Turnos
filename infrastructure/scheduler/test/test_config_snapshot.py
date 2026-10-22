import hashlib
import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("snapshot", ROOT / "config_snapshot.py")
snapshot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snapshot)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        values = dict(line.split("=", 1) for line in (ROOT / ".dev.vars.example").read_text(encoding="utf-8").splitlines() if line and not line.startswith("#"))
        self.config = {key: values[key].strip('"') for key in snapshot.BOT_KEYS}

    def payload(self):
        raw = json.dumps(self.config, ensure_ascii=False, separators=(",", ":"))
        return raw, hashlib.sha256(raw.encode()).hexdigest()

    def test_preserves_criteria_and_production(self):
        result = snapshot.validate_snapshot(*self.payload(), "false")
        self.assertEqual(result, self.config)
        self.assertEqual(result["DRY_RUN"], "false")

    def test_safe_input_cannot_be_overridden_by_config(self):
        result = snapshot.validate_snapshot(*self.payload(), "true")
        self.assertEqual(result["DRY_RUN"], "true")

    def test_recurring_safe_config_cannot_be_overridden_by_input(self):
        self.config["DRY_RUN"] = "true"
        self.assertEqual(snapshot.validate_snapshot(*self.payload(), "false")["DRY_RUN"], "true")

    def test_unknown_or_credential_keys_are_rejected(self):
        self.config["OSEP_PASS"] = "placeholder"
        with self.assertRaises(ValueError):
            snapshot.validate_snapshot(*self.payload(), "false")

    def test_missing_config_is_rejected(self):
        with self.assertRaises(ValueError):
            snapshot.validate_snapshot("", "", "false")

    def test_tampering_is_rejected(self):
        raw, sha = self.payload()
        with self.assertRaises(ValueError):
            snapshot.validate_snapshot(raw.replace("ESPECIALIDAD DE EJEMPLO", "OTRA"), sha, "false")

    def test_environment_line_injection_is_rejected(self):
        self.config["OBJ_MEDICO"] = "profesional\nOSEP_PASS=otro"
        with self.assertRaises(ValueError):
            snapshot.validate_snapshot(*self.payload(), "false")

    def test_bad_hour_date_and_boolean_are_rejected(self):
        for key, value in [("OBJ_HORA_MAX", "25:00"), ("OBJ_FECHA_DISP", "31-02-2026"), ("OBJ_HORA_FLEXIBLE", "yes")]:
            with self.subTest(key=key):
                original = self.config[key]
                self.config[key] = value
                with self.assertRaises(ValueError):
                    snapshot.validate_snapshot(*self.payload(), "false")
                self.config[key] = original

    def test_github_actions_script_directory_import(self):
        # En producción: python infrastructure/scheduler/run_with_credentials.py
        # En ese modo sys.path apunta a infrastructure/scheduler (no al repo).
        raw, signature = self.payload()
        code = (
            "import config_snapshot,sys;"
            "config_snapshot.validate_snapshot(sys.argv[1],sys.argv[2],'true')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code, raw, signature],
            cwd=str(ROOT), text=True, capture_output=True, timeout=15
        )
        self.assertEqual(proc.returncode, 0,
                         "La validación debe poder importar availability_rules desde el directorio del runner")

    def test_validation_does_not_write_actions_files(self):
        raw, sha = self.payload()
        with tempfile.TemporaryDirectory() as temporary:
            env_file, summary = pathlib.Path(temporary) / "env", pathlib.Path(temporary) / "summary"
            with patch.dict(snapshot.os.environ, {"GITHUB_ENV":str(env_file), "GITHUB_STEP_SUMMARY":str(summary)}):
                self.assertEqual(snapshot.validate_snapshot(raw,sha,"false"),self.config)
            self.assertFalse(env_file.exists())
            self.assertFalse(summary.exists())


if __name__ == "__main__":
    unittest.main()
