"""Preset names and the tolerance of hand-edited files."""

import pytest

from opal_c1.core.presets import decode, validate_name


@pytest.mark.parametrize("bad", ["", "  ", "../evil", "a/b", "a\\b", ".hidden", ".", ".."])
def test_pathlike_names_are_refused(bad):
    with pytest.raises(ValueError):
        validate_name(bad)


@pytest.mark.parametrize("good", ["desk", "studio-warm", "Meeting 2", "g1_low"])
def test_ordinary_names_pass(good):
    assert validate_name(good) == good


def test_decode_roundtrips_a_well_formed_preset():
    raw = {
        "version": 1,
        "mode": "studio",
        "look": "G1",
        "strength": 0.65,
        "look_strength": {"noir": 0.4},
        "mirror_h": True,
        "mirror_v": False,
        "overlay": {"path": "/tmp/x.png", "x": 10, "y": 20,
                    "width": 300, "height": 300, "opacity": 0.8},
        "controls": {"focus": 150, "wb": 3200},
    }
    fields, notes = decode(raw)
    assert notes == []
    assert fields["mode"] == "studio"
    assert fields["look"] == "G1"
    assert fields["overlay"]["opacity"] == 0.8
    assert fields["controls"]["focus"] == 150


def test_decode_clamps_out_of_range_values():
    fields, _ = decode({"strength": 7.5, "overlay": {"opacity": -3}})
    assert fields["strength"] == 1.0
    assert fields["overlay"]["opacity"] == 0.0


def test_decode_drops_garbage_without_raising():
    fields, notes = decode({
        "mode": "turbo",
        "strength": "loud",
        "look_strength": {"noir": "no", "G1": 0.9},
        "overlay": {"x": "left"},
    })
    assert "mode" not in fields
    assert "strength" not in fields
    assert fields["look_strength"] == {"G1": 0.9}
    assert any("turbo" in n for n in notes)


def test_decode_survives_a_non_object():
    fields, notes = decode(["not", "a", "preset"])
    assert fields == {}
    assert notes


def test_blur_and_background_decode():
    fields, _ = decode({"blur": 1.7, "background": "/tmp/bg.png"})
    assert fields["blur"] == 1.0
    assert fields["background"] == "/tmp/bg.png"
    fields, _ = decode({"blur": "junk", "background": 42})
    assert "blur" not in fields and "background" not in fields


def test_blur_style_decode():
    fields, _ = decode({"blur_style": 1})
    assert fields["blur_style"] == 1
    fields, _ = decode({"blur_style": 7})
    assert "blur_style" not in fields
