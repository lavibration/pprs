import unittest
import os
import csv
import tempfile
from pappers_scraper import (
    parse_pappers_number,
    extract_siren_from_text,
    evaluate_financial_criteria,
    parse_financial_rows,
    append_to_csv,
    clean_text,
    FIELDNAMES
)


class TestPappersScraper(unittest.TestCase):

    def test_parse_pappers_number(self):
        # Normal positive cases
        self.assertEqual(parse_pappers_number("9,62 M"), 9620000.0)
        self.assertEqual(parse_pappers_number("79,1 k"), 79100.0)
        self.assertEqual(parse_pappers_number("868"), 868.0)
        self.assertEqual(parse_pappers_number("11,6"), 11.6)
        self.assertEqual(parse_pappers_number("1 250 k €"), 1250000.0)
        self.assertEqual(parse_pappers_number("2,5 M €"), 2500000.0)

        # Empty or null indicators
        self.assertEqual(parse_pappers_number("-"), 0.0)
        self.assertEqual(parse_pappers_number(""), 0.0)
        self.assertEqual(parse_pappers_number(None), 0.0)
        self.assertEqual(parse_pappers_number("N/A"), 0.0)
        self.assertEqual(parse_pappers_number("n/a"), 0.0)
        self.assertEqual(parse_pappers_number("NC"), 0.0)

        # Negative numbers
        self.assertEqual(parse_pappers_number("-50 k"), -50000.0)
        self.assertEqual(parse_pappers_number("(1,2 M)"), -1200000.0)
        self.assertEqual(parse_pappers_number("-120"), -120.0)

    def test_extract_siren(self):
        self.assertEqual(extract_siren_from_text("SIREN : 529 725 863"), "529725863")
        self.assertEqual(extract_siren_from_text("https://www.pappers.fr/entreprise/bouygues-572015246"), "572015246")
        self.assertEqual(extract_siren_from_text("aucun siren"), None)

    def test_parse_financial_rows(self):
        sample_rows = [
            ("Chiffre d’affaires", ["1,5 M", "1,45 M", "1,52 M", "1,3 M"]),
            ("EBITDA - EBE", ["300 k", "280 k", "290 k", "260 k"]),
            ("Résultat d’exploitation", ["250 k", "230 k", "240 k", "210 k"]),
            ("Trésorerie", ["400 k", "350 k", "300 k", "250 k"]),
            ("Dettes financières", ["50 k", "60 k", "70 k", "80 k"])
        ]
        res = parse_financial_rows(sample_rows)
        self.assertEqual(res["ca"], [1500000.0, 1450000.0, 1520000.0])
        self.assertEqual(res["ebitda"], [300000.0, 280000.0, 290000.0])
        self.assertEqual(res["rex"], [250000.0, 230000.0, 240000.0])
        self.assertEqual(res["tresorerie"], [400000.0, 350000.0, 300000.0])
        self.assertEqual(res["dettes_fin"], [50000.0, 60000.0, 70000.0])

    def test_evaluate_financial_criteria_eligible(self):
        # 1. CA: 1.5M, 1.45M, 1.52M (mean = 1.49M, std dev < 15%)
        # 2. EBITDA: 300k / 1.5M = 20% (>= 12%)
        # 3. REX: 250k / 300k = 83.33% (>= 75%)
        # 4. Treso nette: 400k - 50k = 350k -> ratio 350k / 300k = 1.17x (>= 1.0x)
        data = {
            "ca": [1500000.0, 1450000.0, 1520000.0],
            "ebitda": [300000.0, 280000.0, 290000.0],
            "rex": [250000.0, 230000.0, 240000.0],
            "tresorerie": [400000.0, 300000.0, 250000.0],
            "dettes_fin": [50000.0, 60000.0, 70000.0]
        }
        is_eligible, metrics = evaluate_financial_criteria(data)
        self.assertTrue(is_eligible)
        self.assertEqual(metrics["Eligible_Screening"], "OUI")
        self.assertEqual(metrics["CA_N"], 1500000.0)
        self.assertEqual(metrics["EBITDA_N"], 300000.0)
        self.assertEqual(metrics["Marge_EBITDA_pct"], 20.0)
        self.assertAlmostEqual(metrics["Cash_Conversion_pct"], 83.33, places=2)
        self.assertEqual(metrics["Treso_Nette_EUR"], 350000.0)
        self.assertAlmostEqual(metrics["Ratio_Treso_EBITDA"], 1.17, places=2)
        self.assertEqual(metrics["Raison_Exclusion"], "Aucune")

    def test_evaluate_financial_criteria_ineligible_ca_out_of_range(self):
        data = {
            "ca": [500000.0, 500000.0, 500000.0], # < 800k€
            "ebitda": [100000.0, 100000.0, 100000.0],
            "rex": [80000.0, 80000.0, 80000.0],
            "tresorerie": [200000.0, 200000.0, 200000.0],
            "dettes_fin": [10000.0, 10000.0, 10000.0]
        }
        is_eligible, metrics = evaluate_financial_criteria(data)
        self.assertFalse(is_eligible)
        self.assertEqual(metrics["Eligible_Screening"], "NON")
        self.assertIn("CA hors plage", metrics["Raison_Exclusion"])

    def test_evaluate_financial_criteria_ineligible_unstable_ca(self):
        data = {
            "ca": [2000000.0, 1000000.0, 800000.0], # std dev > 15%
            "ebitda": [400000.0, 200000.0, 150000.0],
            "rex": [350000.0, 180000.0, 130000.0],
            "tresorerie": [600000.0, 500000.0, 400000.0],
            "dettes_fin": [50000.0, 50000.0, 50000.0]
        }
        is_eligible, metrics = evaluate_financial_criteria(data)
        self.assertFalse(is_eligible)
        self.assertEqual(metrics["Eligible_Screening"], "NON")
        self.assertIn("Instabilité CA", metrics["Raison_Exclusion"])

    def test_evaluate_financial_criteria_ineligible_low_ebitda(self):
        data = {
            "ca": [1000000.0, 1000000.0, 1000000.0],
            "ebitda": [100000.0, 100000.0, 100000.0], # 10% < 12%
            "rex": [80000.0, 80000.0, 80000.0],
            "tresorerie": [200000.0, 200000.0, 200000.0],
            "dettes_fin": [10000.0, 10000.0, 10000.0]
        }
        is_eligible, metrics = evaluate_financial_criteria(data)
        self.assertFalse(is_eligible)
        self.assertIn("Marge EBITDA faible", metrics["Raison_Exclusion"])

    def test_evaluate_financial_criteria_ineligible_low_cash_conv(self):
        data = {
            "ca": [1000000.0, 1000000.0, 1000000.0],
            "ebitda": [200000.0, 200000.0, 200000.0],
            "rex": [100000.0, 100000.0, 100000.0], # 50% < 75%
            "tresorerie": [300000.0, 300000.0, 300000.0],
            "dettes_fin": [10000.0, 10000.0, 10000.0]
        }
        is_eligible, metrics = evaluate_financial_criteria(data)
        self.assertFalse(is_eligible)
        self.assertIn("Cash Conv. faible", metrics["Raison_Exclusion"])

    def test_evaluate_financial_criteria_ineligible_low_treso_ratio(self):
        data = {
            "ca": [1000000.0, 1000000.0, 1000000.0],
            "ebitda": [200000.0, 200000.0, 200000.0],
            "rex": [180000.0, 180000.0, 180000.0],
            "tresorerie": [100000.0, 100000.0, 100000.0],
            "dettes_fin": [50000.0, 50000.0, 50000.0] # Net treso = 50k -> ratio 50k / 200k = 0.25 < 1.0x
        }
        is_eligible, metrics = evaluate_financial_criteria(data)
        self.assertFalse(is_eligible)
        self.assertIn("Ratio Treso/EBITDA faible", metrics["Raison_Exclusion"])

    def test_evaluate_financial_criteria_insufficient_history(self):
        data = {
            "ca": [1000000.0, 950000.0], # Only 2 years
            "ebitda": [200000.0, 190000.0]
        }
        is_eligible, metrics = evaluate_financial_criteria(data)
        self.assertFalse(is_eligible)
        self.assertIn("Historique CA < 3 ans", metrics["Raison_Exclusion"])

    def test_csv_export(self):
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            temp_path = f.name

        try:
            sample_data = [
                {
                    "Nom de l'entreprise": "ALPHA SAS",
                    "SIREN": "123456789",
                    "Eligible_Screening": "OUI",
                    "CA_N": 1500000.0,
                    "Moyenne_CA_3ans": 1490000.0,
                    "StdDev_CA_pct": 2.5,
                    "EBITDA_N": 300000.0,
                    "Marge_EBITDA_pct": 20.0,
                    "Cash_Conversion_pct": 80.0,
                    "Treso_Nette_EUR": 400000.0,
                    "Ratio_Treso_EBITDA": 1.33,
                    "Raison_Exclusion": "Aucune"
                }
            ]
            append_to_csv(sample_data, temp_path)

            with open(temp_path, mode="r", encoding="utf-8-sig") as csvfile:
                reader = csv.DictReader(csvfile, delimiter=";")
                rows = list(reader)

            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["Nom de l'entreprise"], "ALPHA SAS")
            self.assertEqual(row["SIREN"], "123456789")
            self.assertEqual(row["Eligible_Screening"], "OUI")
            self.assertEqual(row["CA_N"], "1500000.0")
            self.assertEqual(row["Marge_EBITDA_pct"], "20.0")

            # Check headers
            self.assertEqual(list(row.keys()), FIELDNAMES)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)


if __name__ == "__main__":
    unittest.main()
