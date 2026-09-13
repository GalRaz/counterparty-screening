import pytest

from screening.subjects import Identifier, Subject, parse_identifier


def test_identifier_requires_source():
    with pytest.raises(ValueError, match="source"):
        Identifier("dob", "1965", "")


def test_subject_type_must_be_person_or_organization():
    with pytest.raises(ValueError, match="type"):
        Subject(type="company", name="Acme")


def test_subject_name_required():
    with pytest.raises(ValueError, match="name"):
        Subject(type="person", name="  ")


def test_slug_and_key_normalise_whitespace_and_case():
    s = Subject(type="person", name="  Aleksandr   ZACHAROV ")
    assert s.slug == "aleksandr-zacharov"
    assert s.normalised_key() == "person:aleksandr zacharov"


def test_key_includes_dob_for_person_and_registration_for_org():
    p = Subject(type="person", name="Mark Phillips",
                identifiers=[Identifier("dob", "1970", "passport copy")])
    assert p.normalised_key() == "person:mark phillips:dob=1970"
    o = Subject(type="organization", name="Carbon Capital Corporation Pty Ltd",
                identifiers=[Identifier("registration_number", "32 667 478 471", "ASIC extract")])
    assert o.normalised_key() == "organization:carbon capital corporation pty ltd:reg=32667478471"


def test_all_names_includes_aliases_deduped():
    s = Subject(type="person", name="Alexander Ivanov", aliases=["Александр Иванов", "Alexander Ivanov"])
    assert s.all_names() == ["Alexander Ivanov", "Александр Иванов"]


def test_non_latin_detection():
    assert Subject(type="person", name="Александр Иванов").has_non_latin_name()
    assert Subject(type="person", name="Zoë Müller-Łukasz").has_non_latin_name() is False


def test_split_person_name():
    assert Subject(type="person", name="Mark Laurence Allington").split_person_name() == ("Mark", "Laurence", "Allington")
    assert Subject(type="person", name="Madonna").split_person_name() == ("", "", "Madonna")


def test_identifier_lookup_and_source():
    s = Subject(type="person", name="X", identifiers=[Identifier("country", "AU", "user statement")])
    assert s.identifier("country") == "AU"
    assert s.identifier_source("country") == "user statement"
    assert s.identifier("dob") is None


def test_round_trip_dict():
    s = Subject(type="organization", name="Acme Pte Ltd", aliases=["ACME"], jurisdiction="SG",
                role="counterparty", identifiers=[Identifier("registration_number", "2023", "ACRA")])
    assert Subject.from_dict(s.to_dict()) == s


def test_parse_identifier_cli_form():
    assert parse_identifier("dob=1970-01-02@passport copy") == Identifier("dob", "1970-01-02", "passport copy")
    with pytest.raises(ValueError):
        parse_identifier("dob=1970")  # no source
