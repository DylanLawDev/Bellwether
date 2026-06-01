# GKG 2.1 tab-delimited column -> producer payload field mapping:
#   0  GKGRECORDID            -> gkg_record_id (also idempotency_key)
#   1  V2.1DATE               -> date / fetched_at
#   8  V2EnhancedThemes       -> v2_themes        (Name,offset;... -> names)
#   10 V2EnhancedLocations    -> v2_locations     (type#name#country#adm1#lat#lng#featureId,offset;...
#                                                   -> ;-separated #-delimited location records,
#                                                      trailing ,offset stripped)
#   12 V2EnhancedPersons      -> v2_persons       (Name,offset;... -> names)
#   14 V2EnhancedOrganizations-> v2_organizations (Name,offset;... -> names)
#   15 V1.5Tone               -> v15_tone         (raw comma list; [0]=overall tone)
import pathlib

from producers.gdelt.producer import parse_gkg_line, rows_to_submissions

LINES = (pathlib.Path(__file__).parent / "fixtures/gkg_sample.csv").read_text().splitlines()


def test_parse_extracts_payload_fields():
    p = parse_gkg_line(LINES[0])
    assert "v2_themes" in p and "v15_tone" in p and "gkg_record_id" in p and "date" in p


def test_rows_to_submissions_uses_record_id_as_idempotency_key():
    subs = rows_to_submissions(LINES)
    assert subs[0].idempotency_key == parse_gkg_line(LINES[0])["gkg_record_id"]
    assert subs[0].content_type == "gdelt-gkg-v2" and subs[0].source == "gdelt.gkg"
    assert subs[0].fetched_at.tzinfo is not None


def test_clean_enhanced_strips_offsets():
    # The enhanced columns are "Name,offset;Name,offset"; offsets must be dropped,
    # leaving the bare ;-separated values that T10's GdeltGkgExtractor consumes.
    p = parse_gkg_line(LINES[0])
    assert p["v2_persons"] == "Barack Obama;Angela Merkel"
    assert p["v2_organizations"] == "United Nations;World Bank"
    assert "EPU_POLICY_GOVERNMENT" in p["v2_themes"].split(";")
    assert "120" not in p["v2_themes"]  # offset stripped
    # tone is the raw comma list with overall tone first
    assert p["v15_tone"].split(",")[0] == "-2.13"
    # Locations: V2EnhancedLocations is type#name#country#adm1#lat#lng#featureId,offset;...
    # After stripping the trailing ,offset, each item is a full #-delimited location record.
    # Fixture row 0 col 10: "1#United States#US#US#38.0#-97.0#US,210;4#London#UK#UKH9#51.5#-0.12#London,600"
    expected_locations = "1#United States#US#US#38.0#-97.0#US;4#London#UK#UKH9#51.5#-0.12#London"
    assert "210" not in p["v2_locations"]  # numeric offset stripped
    assert "600" not in p["v2_locations"]  # numeric offset stripped
    assert p["v2_locations"] == expected_locations
