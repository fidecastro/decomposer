"""What doctor reads from PipeWire's listing of cameras."""
from opal_c1.cli import pipewire_cameras


def _source(path, formats):
    return {"info": {"props": {"media.class": "Video/Source", "api.v4l2.path": path},
                     "params": {"EnumFormat": formats}}}


def test_a_camera_with_formats_is_usable():
    assert pipewire_cameras([_source("/dev/video40", [{"format": "I420"}])]) == {"/dev/video40": True}


def test_a_camera_listed_without_formats_is_flagged():
    # Seen on 2026-11-18: listed by PipeWire, hidden by Chrome.
    assert pipewire_cameras([_source("/dev/video40", [])]) == {"/dev/video40": False}


def test_other_objects_are_ignored():
    objects = [{"info": {"props": {"media.class": "Video/Device", "api.v4l2.path": "/dev/video40"}}},
               {"info": None}, {},
               {"info": {"props": {"media.class": "Video/Source"}}}]
    assert pipewire_cameras(objects) == {}
