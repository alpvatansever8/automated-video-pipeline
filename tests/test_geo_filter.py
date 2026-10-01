from video_pipeline import geo_filter as g


def setup_function():
    g.set_document_country("Iceland")


def teardown_function():
    g.set_document_country("")


def test_location_terms_include_country_and_words():
    terms = g.extract_location_terms("Vatnajokull Glacier")
    assert terms[0] == "Vatnajokull Glacier"
    assert "Glacier" in terms and "Iceland" in terms


def test_inject_location_adds_missing_terms_only():
    assert g.inject_location("waterfall", ["Gullfoss", "Iceland"]) == "Gullfoss waterfall Iceland"
    assert g.inject_location("Gullfoss waterfall Iceland", ["Gullfoss", "Iceland"]) == \
        "Gullfoss waterfall Iceland"


def test_title_matching_is_loose():
    assert g.title_contains_location("An Icelandic waterfall", ["Iceland"])
    assert not g.title_contains_location("City traffic", ["Iceland"])


def test_hard_reject_wrong_country():
    assert g.hard_reject("Tokyo street at night, Japan", "", ["Reykjavik"]) is not None
    assert g.hard_reject("Iceland and Japan comparison", "", ["Reykjavik"]) is None
