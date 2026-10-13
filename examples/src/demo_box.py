"""Demo part: small open box with rounded corners. Tests the design-slice-print chain."""
from cadgen import build123d as bd
from cadgen import glb, step, stl
from lib.bed import on_bed

LENGTH = 40.0  # outer X, mm
WIDTH = 30.0   # outer Y, mm
HEIGHT = 20.0  # outer Z, mm
WALL = 2.0     # side wall and floor thickness, mm
CORNER = 4.0   # outer vertical corner radius, mm


def open_box(length, width, height, wall, corner):
    outer = bd.extrude(bd.RectangleRounded(length, width, corner), amount=height)
    inner = bd.extrude(
        bd.RectangleRounded(length - 2 * wall, width - 2 * wall, corner - wall),
        amount=height,
    ).moved(bd.Location((0, 0, wall)))
    return outer - inner


@step(out="../STEP/demo_box.step")
@stl(out="../STL/demo_box.stl")
def demo_box():
    body = open_box(LENGTH, WIDTH, HEIGHT, WALL, CORNER)
    body.label = "demo_box"
    return body


@step(out="../STEP/demo_box_on_bed.step")
@glb(out="../GLB/demo_box_on_bed.glb")
def demo_box_on_bed():
    return on_bed(demo_box(), "demo_box")


if __name__ == "__main__":
    demo_box()
    demo_box_on_bed()
