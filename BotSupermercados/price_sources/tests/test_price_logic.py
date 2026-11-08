import unittest

from shopping import compare_shopping_list
from price_sources.web_catalog import _extract_products


class ShoppingComparisonTests(unittest.TestCase):
    def test_does_not_compare_different_products_without_canonical_key(self):
        queried = [{"query": "yogur", "prices": [
            {"supermarket": "A", "name": "Yogur natural marca A", "normalized_name": "yogur natural marca a", "package_size": "4x125g", "unit_price": 2.0, "unit": "kg", "pack_price": 1.0},
            {"supermarket": "B", "name": "Yogur griego marca B", "normalized_name": "yogur griego marca b", "package_size": "4x125g", "unit_price": 3.0, "unit": "kg", "pack_price": 1.5},
        ]}]
        result = compare_shopping_list(["yogur"], ["A", "B"], queried)
        self.assertIsNone(result["items"][0]["cheapest"])

    def test_compares_same_canonical_product_and_unit(self):
        queried = [{"query": "leche", "prices": [
            {"supermarket": "A", "name": "Leche 1L A", "canonical_key": "leche-entera-1l", "unit_price": 0.9, "unit": "l", "pack_price": 0.9},
            {"supermarket": "B", "name": "Leche entera 1 litro B", "canonical_key": "leche-entera-1l", "unit_price": 1.1, "unit": "l", "pack_price": 1.1},
        ]}]
        result = compare_shopping_list(["leche"], ["A", "B"], queried)
        self.assertEqual(result["items"][0]["cheapest"], "A")
        self.assertEqual(result["items"][0]["savings"], 0.2)

    def test_single_complete_basket_is_not_declared_cheapest(self):
        queried = [{"query": "huevos", "prices": [
            {"supermarket": "DIA", "name": "Huevos M", "pack_price": 2.5, "unit_price": None, "unit": None}
        ]}]
        result = compare_shopping_list(["huevos"], ["DIA", "Alcampo"], queried)
        self.assertIsNone(result["best_total"])


class JsonLdConnectorTests(unittest.TestCase):
    def test_extracts_only_real_product_offer_prices(self):
        html = '''<html><script type="application/ld+json">
        {"@context":"https://schema.org","@type":"Product","name":"Leche entera 1 L",
         "sku":"sku-1","brand":{"@type":"Brand","name":"Marca"},
         "offers":{"@type":"Offer","price":"1.25","priceCurrency":"EUR","availability":"https://schema.org/InStock"}}
        </script></html>'''
        rows = list(_extract_products(html, "https://example.test/search", "Carrefour", "leche", "28045"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["pack_price"], 1.25)
        self.assertEqual(rows[0]["postal_code"], "28045")
        self.assertTrue(rows[0]["raw_data"]["jsonld_offer"])

    def test_rejects_non_eur_and_missing_price(self):
        html = '''<script type="application/ld+json">
        {"@type":"Product","name":"Producto","offers":{"price":"2","priceCurrency":"USD"}}
        </script>'''
        self.assertEqual(list(_extract_products(html, "https://example.test", "Carrefour", "producto", "28045")), [])


if __name__ == "__main__":
    unittest.main()
