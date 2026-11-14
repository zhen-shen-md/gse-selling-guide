"""Unit tests for the parsing helpers most likely to break when agencies change their PDFs.

Run: python -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

from build_changes import classify_text_change, match_sections, sentence_diff, word_bag_similarity  # noqa: E402
from build_pricing import attribute_key, parse_score, score_key  # noqa: E402
from kb_common import parse_long_date, parse_us_date, split_chunks  # noqa: E402
from resolve_official_links import slugify  # noqa: E402


def sec(sid, title, body, rev="2026-01-01"):
    return {"section_id": sid, "section_title": title, "text": f"{sid}, {title}\n{body}", "revision_date": rev}


class DateParsing(unittest.TestCase):
    def test_two_and_four_digit_years(self):
        self.assertEqual(parse_us_date("11/04/26"), ("2026-11-04", None))
        self.assertEqual(parse_us_date("10/07/2026"), ("2026-10-07", None))

    def test_malformed_three_digit_year_is_recovered_with_warning(self):
        iso, warning = parse_us_date("11/04/026")  # appears in the Nov 2026 Freddie bookmarks
        self.assertEqual(iso, "2026-11-04")
        self.assertIn("malformed", warning)

    def test_long_dates(self):
        self.assertEqual(parse_long_date("Selling Guide Published November 4, 2026"), "2026-11-04")


class Chunking(unittest.TestCase):
    def test_chunks_respect_limit_and_overlap(self):
        text = ("Sentence number one is here. " * 400).strip()
        chunks = split_chunks(text, 1000, 100)
        self.assertTrue(all(len(c) <= 1000 for c in chunks))
        self.assertGreater(len(chunks), 10)


class PricingParsing(unittest.TestCase):
    def test_score_band_labels_from_both_agencies_align(self):
        fannie, freddie = parse_score("760 – 779"), parse_score("≥ 760 & < 780")
        self.assertEqual(score_key(*fannie[:2]), score_key(*freddie[:2]))
        self.assertEqual(score_key(*parse_score("≤ 639")[:2]), score_key(*parse_score("< 640")[:2]))
        self.assertEqual(score_key(*parse_score("≥ = 780")[:2]), "gte_780")

    def test_strict_greater_than_is_flagged(self):
        lo, hi, note = parse_score("> 740")
        self.assertEqual((lo, hi), (740, None))
        self.assertIsNotNone(note)

    def test_attribute_labels_map_across_agencies(self):
        self.assertEqual(attribute_key("Condo1")[0], attribute_key("Condominium Unit 6")[0])
        self.assertEqual(attribute_key("Subordinate financing3")[0], attribute_key("Secondary Financing 8, 9")[0])
        self.assertEqual(attribute_key("High-balance ARM")[0], attribute_key("Super Conforming ARM")[0])
        self.assertEqual(attribute_key("Two- to four-unit property")[0], attribute_key("Number of Units > 1")[0])


class ChangeDetection(unittest.TestCase):
    def test_renumbered_section_is_matched_by_title(self):
        old = [sec("B4-2.2-01", "Limited Review Process", "limited review text " * 20),
               sec("B4-2.2-02", "Full Review Process", "full review text about projects " * 20)]
        new = [sec("B4-2.2-01", "Full Review Process", "full review text about projects " * 20)]
        pairs = {(o and o["section_id"], n and n["section_id"]): how for o, n, how in match_sections(old, new)}
        self.assertEqual(pairs[("B4-2.2-02", "B4-2.2-01")], "same_title_new_id")
        self.assertIn(("B4-2.2-01", None), pairs)

    def test_table_reflow_is_layout_only(self):
        a = sec("C3-7-03", "Making Good Delivery", "Rate Amount Pools < 8% 1 2 3 >= 8% 3 4 5")
        b = sec("C3-7-03", "Making Good Delivery", ">= 8% 3 4 5 Rate Amount Pools < 8% 1 2 3")
        self.assertGreater(word_bag_similarity(a["text"], b["text"]), 0.98)
        self.assertEqual(classify_text_change(a, b, {}), "layout_only")

    def test_undated_wording_change_is_editorial(self):
        a = sec("B5-2-02", "MH Eligibility", "see B4-2.2-06 for PERS requirements and other rules")
        b = sec("B5-2-02", "MH Eligibility", "see B4-2.2-04 for PERS requirements and other rules and conditions")
        self.assertEqual(classify_text_change(a, b, {}), "editorial")
        b["revision_date"] = "2026-11-04"
        self.assertEqual(classify_text_change(a, b, {}), "revised")

    def test_sentence_diff_reports_added_and_removed(self):
        d = sentence_diff("The lender must call the employer. Records are kept.",
                          "The lender may use any reasonable method. Records are kept.")
        self.assertEqual(d["sentences_added"], 1)
        self.assertEqual(d["sentences_removed"], 1)


class OfficialLinks(unittest.TestCase):
    def test_slugs_match_fannie_url_style(self):
        self.assertEqual(slugify("General Requirements for Credit Scores"), "general-requirements-credit-scores")
        self.assertEqual(slugify("Non-U.S. Citizen Borrower Eligibility Requirements"), "non-us-citizen-borrower-eligibility-requirements")
        self.assertEqual(slugify("Representations and Warranties Overview"), "representations-and-warranties-overview")


if __name__ == "__main__":
    unittest.main()
