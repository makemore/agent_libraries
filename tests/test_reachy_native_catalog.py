"""Offline catalog contract tests; no Django, SDK, network or robot required.

Run: .venv/bin/python -B -m unittest discover -s tests -p test_reachy_native_catalog.py -v
"""

from collections import Counter
from pathlib import Path
import re
import runpy
import unittest


ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATHS = (
    ROOT / "products/studio/reachy/native_catalog.py",
    ROOT / "clients/agent-reachy/src/agent_reachy/native_catalog.py",
)
FIELDS = {"id", "label", "group", "dataset", "move", "duration_ms", "sha256", "snapshot"}
SNAPSHOTS = {
    "emotions": "873ae49f0b89114b7e535eff0c1f7560d21d9357",
    "dances": "3564295e72d41c1271f46bf5540a3fcad9d5f669",
}
EXPECTED_MOVES = {
    "emotions": (
        "amazed1 anxiety1 attentive1 attentive2 boredom1 boredom2 calming1 cheerful1 "
        "come1 confused1 contempt1 curious1 dance1 dance2 dance3 disgusted1 displeased1 "
        "displeased2 downcast1 dying1 electric1 enthusiastic1 enthusiastic2 exhausted1 "
        "fear1 frustrated1 furious1 go_away1 grateful1 helpful1 helpful2 impatient1 "
        "impatient2 incomprehensible2 indifferent1 inquiring1 inquiring2 inquiring3 "
        "irritated1 irritated2 laughing1 laughing2 lonely1 lost1 loving1 mini-deep-sleep "
        "no1 no_excited1 no_sad1 oops1 oops2 proud1 proud2 proud3 rage1 relief1 relief2 "
        "reprimand1 reprimand2 reprimand3 resigned1 sad1 sad2 scared1 serenity1 shy1 "
        "sleep1 success1 success2 surprised1 surprised2 thoughtful1 thoughtful2 tired1 "
        "toc-toc-toc uncertain1 uncomfortable1 understanding1 understanding2 waiting "
        "wake-mini-up welcoming1 welcoming2 yes1 yes_sad1"
    ).split(),
    "dances": (
        "chicken_peck chin_lead dizzy_spin grid_snap groovy_sway_and_roll head_tilt_roll "
        "interwoven_spirals jackson_square neck_recoil pendulum_swing polyrhythm_combo "
        "sharp_side_tilt side_glance_flick side_peekaboo side_to_side_sway simple_nod "
        "stumble_and_recover uh_huh_tilt yeah_nod"
    ).split(),
}
EXPECTED_DURATIONS = (
    3420, 8120, 4280, 6460, 15700, 14180, 6060, 2800, 3160, 7880,
    3560, 11780, 3240, 17260, 18360, 6940, 3360, 2940, 5880, 5680,
    3500, 2720, 3420, 18260, 3480, 5960, 5720, 4840, 2500, 4360,
    3840, 4020, 3900, 3440, 2560, 2140, 2580, 2920, 2480, 5260,
    4640, 2920, 10220, 8120, 5600, 15985, 2680, 4480, 7040, 2460,
    2700, 3760, 3180, 3360, 5080, 5000, 6900, 4700, 11140, 4280,
    4740, 9020, 7340, 7200, 4580, 7800, 19760, 2260, 2420, 2480,
    3020, 5900, 5460, 7440, 13369, 6140, 6020, 3940, 2620, 9956,
    15664, 3460, 4320, 3400, 5080,
    1840, 1840, 1840, 1840, 1840, 1840, 3960, 5000, 1860, 1840,
    2880, 2880, 1860, 5000, 1860, 1820, 1840, 1820, 1860,
)


class NativeCatalogTests(unittest.TestCase):
    def setUp(self):
        # Load the standalone modules without importing either application's startup.
        self.modules = [runpy.run_path(str(path)) for path in CATALOG_PATHS]

    def test_host_client_parity_and_revision(self):
        self.assertEqual(CATALOG_PATHS[0].read_bytes(), CATALOG_PATHS[1].read_bytes())
        self.assertEqual(self.modules[0]["ANIMATIONS"], self.modules[1]["ANIMATIONS"])
        for module in self.modules:
            self.assertEqual(module["CATALOG_REVISION"], "native-1")

    def test_counts_id_order_and_move_assignments(self):
        expected_ids = [f"e{i:03}" for i in range(85)] + [f"d{i:03}" for i in range(19)]
        for path, module in zip(CATALOG_PATHS, self.modules):
            with self.subTest(path=path):
                animations = module["ANIMATIONS"]
                self.assertIs(type(animations), dict)
                self.assertEqual(len(animations), 104)
                self.assertEqual(list(animations), expected_ids)
                self.assertEqual(
                    Counter(row["group"] for row in animations.values()),
                    {"emotions": 85, "dances": 19},
                )
                self.assertEqual(len({(row["dataset"], row["move"]) for row in animations.values()}), 104)
                for group, moves in EXPECTED_MOVES.items():
                    self.assertEqual(
                        [row["move"] for row in animations.values() if row["group"] == group],
                        moves,
                    )

    def test_schema_bounds_and_strict_characters(self):
        patterns = {
            "id": r"[ed][0-9]{3}",
            "label": r"[A-Z][a-z]*(?: [A-Z][a-z]*)*(?: [0-9]+)?",
            "group": r"emotions|dances",
            "dataset": r"pollen-robotics/reachy-mini-(?:emotions|dances)-library",
            "move": r"[a-z0-9]+(?:[_-][a-z0-9]+)*",
            "sha256": r"[0-9a-f]{64}",
            "snapshot": r"[0-9a-f]{40}",
        }
        for path, module in zip(CATALOG_PATHS, self.modules):
            for animation_id, row in module["ANIMATIONS"].items():
                with self.subTest(path=path, animation_id=animation_id):
                    self.assertIs(type(row), dict)
                    self.assertEqual(set(row), FIELDS)
                    self.assertEqual(row["id"], animation_id)
                    for field, pattern in patterns.items():
                        self.assertIs(type(row[field]), str)
                        self.assertIsNotNone(re.fullmatch(pattern, row[field]), field)
                    self.assertIs(type(row["duration_ms"]), int)
                    self.assertGreater(row["duration_ms"], 0)
                    self.assertLessEqual(row["duration_ms"], 20000)
                    group = "emotions" if animation_id.startswith("e") else "dances"
                    self.assertEqual(row["group"], group)
                    self.assertEqual(row["dataset"], f"pollen-robotics/reachy-mini-{group}-library")
                    self.assertEqual(row["snapshot"], SNAPSHOTS[group])

    def test_inspected_durations_are_not_rounded_or_replaced(self):
        for path, module in zip(CATALOG_PATHS, self.modules):
            with self.subTest(path=path):
                self.assertEqual(
                    tuple(row["duration_ms"] for row in module["ANIMATIONS"].values()),
                    EXPECTED_DURATIONS,
                )

    def test_display_labels_space_numbered_variants_only(self):
        for path, module in zip(CATALOG_PATHS, self.modules):
            for animation_id, row in module["ANIMATIONS"].items():
                with self.subTest(path=path, animation_id=animation_id):
                    original = row["move"].replace("_", " ").replace("-", " ").title()
                    expected = re.sub(r"(?<=[A-Za-z])([0-9]+)$", r" \1", original)
                    self.assertEqual(row["label"], expected)
            self.assertEqual(module["ANIMATIONS"]["e000"]["label"], "Amazed 1")
            self.assertEqual(module["ANIMATIONS"]["e027"]["label"], "Go Away 1")
            self.assertEqual(module["ANIMATIONS"]["e045"]["label"], "Mini Deep Sleep")
            self.assertEqual(module["ANIMATIONS"]["d004"]["label"], "Groovy Sway And Roll")


if __name__ == "__main__":
    unittest.main()