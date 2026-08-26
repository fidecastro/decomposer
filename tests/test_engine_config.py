"""EngineConfig: one struct, two projections, no drift.

The argv a fresh engine is spawned with and the socket lines a running one
receives are both derived here; these tests pin the mapping so a protocol or
flag rename cannot silently split them again.
"""

from dataclasses import replace

from opal_c1.core.model import (
    EngineConfig,
    engine_cli_args,
    engine_delta_lines,
)


def cfg(**kw) -> EngineConfig:
    return replace(EngineConfig(), **kw)


def test_no_change_means_no_lines():
    assert engine_delta_lines(cfg(), cfg()) == []


def test_each_live_field_produces_its_line():
    base = cfg()
    assert engine_delta_lines(base, replace(base, look="noir")) == ["look noir"]
    assert engine_delta_lines(base, replace(base, strength=0.7)) == ["strength 0.7"]
    assert engine_delta_lines(base, replace(base, flip=3)) == ["flip 3"]
    assert engine_delta_lines(
        base, replace(base, overlay_x=10, overlay_y=20, overlay_w=30, overlay_h=40)
    ) == ["overlay-rect 10 20 30 40"]
    assert engine_delta_lines(
        base, replace(base, overlay_opacity=0.5)
    ) == ["overlay-opacity 0.5"]
    assert engine_delta_lines(
        base, replace(base, overlay="/tmp/x.png")
    ) == ["overlay /tmp/x.png"]


def test_clearing_the_overlay_sends_off():
    with_overlay = cfg(overlay="/tmp/x.png")
    assert engine_delta_lines(with_overlay, cfg()) == ["overlay off"]


def test_restart_fields_are_exactly_the_unsendable_ones():
    base = cfg()
    for field in EngineConfig.RESTART_FIELDS:
        changed = replace(base, **{field: "9999" if field != "width" else 9999})
        assert changed.needs_restart_from(base), field
    # And live changes never demand a restart.
    live = replace(
        base, look="G1", strength=0.9, flip=1,
        overlay="/tmp/x.png", overlay_x=5, overlay_opacity=0.3,
    )
    assert not live.needs_restart_from(base)


def test_cli_args_carry_every_field_the_engine_needs():
    config = cfg(
        input="/dev/video1", look="G1", strength=0.65, flip=2,
        overlay="/tmp/mark.png", overlay_x=100, overlay_y=200,
        overlay_w=300, overlay_h=300, overlay_opacity=0.8,
        lut_dir="/opt/luts",
    )
    args = engine_cli_args(config)
    text = " ".join(args)
    assert "--input /dev/video1" in text
    assert "--look G1" in text
    assert "--strength 0.65" in text
    assert "--flip 2" in text
    assert "--overlay-rect 100,200,300,300" in text
    assert "--overlay-opacity 0.8" in text
    assert "--lut-dir /opt/luts" in text
    assert "--overlay /tmp/mark.png" in text


def test_cli_args_omit_absent_optionals():
    text = " ".join(engine_cli_args(cfg()))
    assert "--lut-dir" not in text
    assert "--overlay " not in text
