"""Round drink coaster: flat disc with a raised rim on the top edge."""
from cadgen import build123d as bd
from cadgen import glb, step, stl
from lib.bed import on_bed

DIAMETER = 90.0     # outer diameter, mm
THICKNESS = 4.0     # base disc thickness, mm
RIM_WIDTH = 2.0     # rim width, mm
RIM_HEIGHT = 1.0    # rim height above the base top, mm


def coaster(diameter, thickness, rim_width, rim_height):
    r = diameter / 2
    base = bd.extrude(bd.Circle(r), amount=thickness)
    rim = bd.extrude(bd.Circle(r) - bd.Circle(r - rim_width), amount=rim_height)
    return base + rim.moved(bd.Location((0, 0, thickness)))


@step(out="../STEP/test_coaster.step")
@stl(out="../STL/test_coaster.stl")
def test_coaster():
    body = coaster(DIAMETER, THICKNESS, RIM_WIDTH, RIM_HEIGHT)
    body.label = "test_coaster"
    return body


@step(out="../STEP/test_coaster_on_bed.step")
@glb(out="../GLB/test_coaster_on_bed.glb")
def test_coaster_on_bed():
    return on_bed(test_coaster(), "test_coaster")


if __name__ == "__main__":
    test_coaster()
    test_coaster_on_bed()
