"""Standard-library-only tests for the standalone scoring engine."""

import ast
from dataclasses import dataclass
import inspect
import json
from collections import namedtuple
import unittest

from ai.scoring import rubric


@dataclass(frozen=True)
class Weights:
    weight_match: float = 0.5
    weight_interview: float = 0.3
    weight_behavioral: float = 0.2


class RubricTests(unittest.TestCase):
    def test_all_signals_present(self):
        result = rubric.score(82.0, 0.8, {"attention_pct": 90, "integrity_flag_count": 1}, Weights())
        self.assertEqual(result["final_score"], 81.0)
        self.assertEqual(result["breakdown"], {"match": 82.0, "interview": 80.0, "behavioral": 80.0})
        self.assertIn("match 82.00/100 × 0.5000", result["explanation"])

    def test_match_absent(self):
        result = rubric.score(None, 0.8, {"attention_pct": 90, "integrity_flag_count": 1}, Weights())
        self.assertEqual(result["final_score"], 40.0)
        self.assertIsNone(result["breakdown"]["match"])
        self.assertIn("match signal unavailable — resume parsing failed entirely", result["explanation"])

    def test_interview_absent(self):
        result = rubric.score(82.0, None, {"attention_pct": 90, "integrity_flag_count": 1}, Weights())
        self.assertEqual(result["final_score"], 57.0)
        self.assertIsNone(result["breakdown"]["interview"])
        self.assertIn("interview signal unavailable — interview not yet conducted", result["explanation"])

    def test_behavioral_absent(self):
        result = rubric.score(82.0, 0.8, None, Weights())
        self.assertEqual(result["final_score"], 65.0)
        self.assertIsNone(result["breakdown"]["behavioral"])
        self.assertIn("behavioral signal unavailable — vision pipeline did not run", result["explanation"])

    def test_two_signals_absent(self):
        result = rubric.score(82.0, None, None, Weights())
        self.assertEqual(result["final_score"], 41.0)
        self.assertEqual(result["breakdown"], {"match": 82.0, "interview": None, "behavioral": None})

    def test_all_signals_absent(self):
        result = rubric.score(None, None, None, Weights())
        self.assertEqual(result["final_score"], 0.0)
        self.assertEqual(result["breakdown"], {"match": None, "interview": None, "behavioral": None})
        for name in ("match", "interview", "behavioral"):
            self.assertIn(f"{name} signal unavailable", result["explanation"])

    def test_plain_namedtuple_and_deterministic_bytes(self):
        WeightTuple = namedtuple("WeightTuple", "weight_match weight_interview weight_behavioral")
        args = (82.0, 0.8, {"attention_pct": 90, "integrity_flag_count": 1})
        expected = rubric.score(*args, Weights())
        actual = rubric.score(*args, WeightTuple(0.5, 0.3, 0.2))
        self.assertEqual(actual, expected)
        self.assertEqual(json.dumps(actual, ensure_ascii=False).encode(),
                         json.dumps(rubric.score(*args, Weights()), ensure_ascii=False).encode())

    def test_behavioral_penalty_clamps_at_zero(self):
        result = rubric.score(None, None, {"attention_pct": 20, "integrity_flag_count": 5}, Weights())
        self.assertEqual(result["breakdown"]["behavioral"], 0.0)
        self.assertEqual(result["final_score"], 0.0)

    def test_invalid_scales_and_weights_are_rejected(self):
        with self.assertRaises(ValueError):
            rubric.score(101, None, None, Weights())
        with self.assertRaises(ValueError):
            rubric.score(None, 1.1, None, Weights())
        with self.assertRaises(ValueError):
            rubric.score(None, None, None, Weights(0.5, 0.5, 0.5))

    def test_scoring_module_imports_only_standard_library(self):
        source = inspect.getsource(rubric)
        imports = {
            alias.name.split(".")[0]
            for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports.update(
            node.module.split(".")[0]
            for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ImportFrom) and node.module
        )
        self.assertEqual(imports, {"math", "numbers", "typing", "__future__"})


if __name__ == "__main__":
    unittest.main()
