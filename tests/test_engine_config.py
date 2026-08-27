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


def test_zoom_and_pan_lines():
    base = cfg()
    assert engine_delta_lines(base, replace(base, zoom=2.0)) == ["zoom 2.0"]
    assert engine_delta_lines(
        base, replace(base, pan_x=0.5, pan_y=-0.25)
    ) == ["pan 0.5 -0.25"]


def test_capture_size_is_a_restart_field():
    assert cfg(in_width=3840, in_height=2160).needs_restart_from(cfg())
    assert not cfg(zoom=3.0, pan_x=1.0).needs_restart_from(cfg())


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


def test_blur_and_background_lines():
    base = cfg()
    assert engine_delta_lines(base, replace(base, blur=0.7)) == ["blur 0.7"]
    assert engine_delta_lines(
        base, replace(base, background="/tmp/bg.png")
    ) == ["background /tmp/bg.png"]
    assert engine_delta_lines(
        cfg(background="/tmp/bg.png"), base
    ) == ["background off"]


def test_seg_choices_are_restart_fields():
    assert cfg(seg_model="/opt/m.onnx").needs_restart_from(cfg())
    assert cfg(seg_device="cuda").needs_restart_from(cfg())
    assert not cfg(blur=0.9, background="/x.png").needs_restart_from(cfg())


def test_cli_args_carry_seg_and_blur():
    text = " ".join(engine_cli_args(cfg(
        blur=0.5, background="/tmp/bg.png",
        seg_model="/opt/m.onnx", seg_device="cuda",
    )))
    assert "--blur 0.5" in text
    assert "--background /tmp/bg.png" in text
    assert "--seg-model /opt/m.onnx" in text
    assert "--seg-device cuda" in text
    clean = " ".join(engine_cli_args(cfg()))
    assert "--blur" not in clean and "--seg-" not in clean


def test_model_chain_projections():
    base = cfg(models=(("/m/a.onnx", "cpu"), ("/m/b.onnx", "cuda")),
               model_strengths=(1.0, 0.5))
    text = " ".join(engine_cli_args(base))
    assert "--model /m/a.onnx:cpu:1.0" in text
    assert "--model /m/b.onnx:cuda:0.5" in text
    # Membership changes restart; strength changes are live lines.
    assert cfg(models=(("/m/a.onnx", "cpu"),)).needs_restart_from(cfg())
    tweaked = replace(base, model_strengths=(1.0, 0.9))
    assert not tweaked.needs_restart_from(base)
    assert engine_delta_lines(base, tweaked) == ["model-strength 1 0.9"]


def test_blur_style_is_live():
    base = cfg(blur=0.5)
    styled = replace(base, blur_style=1)
    assert not styled.needs_restart_from(base)
    assert engine_delta_lines(base, styled) == ["blur-style 1"]
    assert "--blur-style 1" in " ".join(engine_cli_args(styled))
