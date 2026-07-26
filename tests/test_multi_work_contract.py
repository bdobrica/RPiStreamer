from __future__ import annotations

import configparser
import json
import re
import unittest
from pathlib import Path
from typing import Any, cast

from rpi_streamer.sidecar import read_sidecar, validate_local_files

FIXTURES = Path(__file__).parent / "fixtures" / "multi_work"
SECTION_RE = re.compile(r'^(?:rpi-streamer|(?:work|media) "[^"\r\n]{1,64}")$')


def load_fixture(name: str) -> dict[str, Any]:
    payload: object = json.loads(
        (FIXTURES / f"{name}.json").read_text(encoding="utf-8")
    )
    if not isinstance(payload, dict):
        raise AssertionError(f"{name} fixture must contain an object")
    return cast(dict[str, Any], payload)


def expand_files(fixture: dict[str, Any]) -> list[str]:
    files = list(fixture.get("files", []))
    for series in fixture.get("file_series", []):
        files.extend(
            series["template"].format(episode=episode)
            for episode in range(series["start"], series["end"] + 1)
        )
    return files


class MultiWorkContractFixtureTests(unittest.TestCase):
    def test_contract_fixtures_are_synthetic_bounded_and_self_consistent(
        self,
    ) -> None:
        for name in ("mf_ghost_continuous", "tsukimichi_reset", "tie_ins"):
            with self.subTest(name=name):
                fixture = load_fixture(name)
                files = expand_files(fixture)
                work_ids = {work["mal_id"] for work in fixture["works"]}

                self.assertEqual(fixture["contract_version"], 1)
                self.assertEqual(len(files), fixture["expected"]["file_count"])
                self.assertEqual(len(files), len(set(files)))
                self.assertLessEqual(len(files), 50)
                self.assertIn(fixture["primary_mal_id"], work_ids)
                self.assertTrue(all(name.endswith(".mp4") for name in files))
                self.assertTrue(
                    all("/" not in name and "\\" not in name for name in files)
                )
                self.assertNotIn("/mnt/", json.dumps(fixture))
                self.assertNotIn("AnimePahe", json.dumps(fixture))

                for work in fixture["works"]:
                    self.assertGreater(work["mal_id"], 0)
                    self.assertGreater(work["episode_count"], 0)
                for mapping in fixture.get("expected_mappings", []):
                    self.assertIn(mapping["file"], files)
                    if mapping["mal_id"] is not None:
                        self.assertIn(mapping["mal_id"], work_ids)

    def test_continuous_numbering_contract_resets_three_work_ranges(self) -> None:
        fixture = load_fixture("mf_ghost_continuous")
        works = fixture["works"]

        mapped: list[tuple[int, int]] = []
        for work in works:
            first, last = work["local_episode_range"]
            mapped.extend(
                (work["mal_id"], local + work["episode_offset"])
                for local in range(first, last + 1)
            )

        self.assertEqual(len(mapped), 37)
        self.assertEqual(mapped[0], (990001, 1))
        self.assertEqual(mapped[12], (990002, 1))
        self.assertEqual(mapped[24], (990003, 1))
        self.assertEqual(mapped[-1], (990003, 13))

    def test_reset_numbering_contract_has_twelve_and_twenty_five_files(
        self,
    ) -> None:
        fixture = load_fixture("tsukimichi_reset")
        files = expand_files(fixture)

        first = [name for name in files if "2nd_Season" not in name]
        second = [name for name in files if "2nd_Season" in name]

        self.assertEqual((len(first), len(second)), (12, 25))
        self.assertTrue(first[0].endswith("-_01_1080p.mp4"))
        self.assertTrue(second[0].endswith("-_01_1080p.mp4"))

    def test_tie_in_contract_keeps_ambiguous_and_numeric_files_unmapped(
        self,
    ) -> None:
        fixture = load_fixture("tie_ins")
        unmapped = [
            mapping
            for mapping in fixture["expected_mappings"]
            if mapping["mal_id"] is None
        ]

        self.assertEqual(
            {mapping["file"] for mapping in unmapped},
            {"Fixture_Show_Bonus.mp4", "Fixture_Show_1080p_2024_07.mp4"},
        )

    def test_sidecar_examples_are_compact_and_follow_the_frozen_sections(
        self,
    ) -> None:
        limits = {
            "mf_ghost_continuous": 4,
            "tsukimichi_reset": 3,
            "tie_ins": 4,
        }
        for name, maximum_sections in limits.items():
            with self.subTest(name=name):
                parser = configparser.ConfigParser(interpolation=None)
                parser.read(FIXTURES / f"{name}.ini", encoding="utf-8")

                self.assertLessEqual(len(parser.sections()), maximum_sections)
                self.assertTrue(all(SECTION_RE.fullmatch(s) for s in parser.sections()))
                self.assertIn("rpi-streamer", parser)
                self.assertGreater(int(parser["rpi-streamer"]["mal_id"]), 0)
                sidecar = read_sidecar(FIXTURES / f"{name}.ini")
                validate_local_files(sidecar, set(expand_files(load_fixture(name))))
