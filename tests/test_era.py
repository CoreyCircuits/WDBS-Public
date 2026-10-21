import unittest

from wdbs.era import DEFAULT_ERA, ERAS, era_for, tag_eras


class EraRuleTests(unittest.TestCase):
    def test_decades(self):
        cases = {1955: "70s", 1972: "70s", 1979: "70s", 1980: "80s", 1989: "80s", 1990: "90s",
                 1999: "90s", 2000: "00s", 2009: "00s", 2016: "00s", 2026: "00s"}
        for year, era in cases.items():
            self.assertEqual(era_for(year), era, year)

    def test_notion_numbers_and_junk(self):
        self.assertEqual(era_for(1985.0), "80s")          # Notion numbers arrive as floats
        self.assertEqual(era_for("1993"), "90s")
        for junk in (None, "", "n/a", [], {}):
            self.assertEqual(era_for(junk), DEFAULT_ERA)

    def test_override_wins(self):
        self.assertEqual(era_for(1989, "90s"), "90s")
        self.assertEqual(era_for(1972, "2000s"), "00s")   # Notion option name for the everyday finish
        self.assertEqual(era_for(None, " 80s "), "80s")
        self.assertEqual(era_for(1972, "sixties"), "70s")  # unknown override -> year rule

    def test_tag_eras_from_manifest(self):
        programs = [
            {"tag": "sanford_and_son", "premiere_year": 1972},
            {"tag": "seinfeld", "premiere_year": 1989, "era_override": "90s"},
            {"tag": "atlanta", "premiere_year": 2016},
            {"tag": "nanny"},                                   # no year yet
            {"program": "No Tag", "premiere_year": 1985},       # skipped
        ]
        self.assertEqual(tag_eras(programs), {"sanford_and_son": "70s", "seinfeld": "90s",
                                              "atlanta": "00s", "nanny": "00s"})

    def test_era_keys(self):
        self.assertEqual(ERAS, ("70s", "80s", "90s", "00s"))
        self.assertIn(DEFAULT_ERA, ERAS)


if __name__ == "__main__":
    unittest.main()
