# Hair Card Deform Edit v1.6.0

Blender **4.2** add-on. Edit hair cards in the space you see while a Curve,
Lattice, Armature or Simple Deform modifier is bending them.

![demo](https://raw.githubusercontent.com/tanakorn2544/hair-card-deform-edit/main/docs/demo.gif)

## The problem it fixes

Put a Curve modifier on a hair card and go into Edit Mode. The card on screen is
bent, but the vertices you grab still live in the flat, undeformed cage. Drag a
vertex right and it goes somewhere else. The harder the card bends, the further
off it lands.

## What you get

Flip one toggle and `G` / `R` / `S` move, rotate and scale in the deformed space
you are looking at. Drag a vertex right, it goes right.

Works with proportional editing and snapping, not instead of them.

## Install

1. Download `hair_deform_edit-1.6.0.zip` below.
2. Blender: **Edit > Preferences > Add-ons > Install from Disk**, pick the zip.
3. Enable it.
4. Sidebar (`N`) > **Hair Deform** tab > **Deform Edit: ON**.

Blender 4.2 only.

## Keys

| Key | Does |
|---|---|
| `G` / `R` / `S` | move / rotate / scale in deformed space |
| `Shift+Alt+G` / `R` / `S` | same, works even with the toggle off |
| `X` / `Y` / `Z` | axis constraint (`Shift+X` for plane) |
| `Ctrl` | snapping on/off mid-drag |
| Wheel, `PageUp` / `PageDown` | proportional falloff size |
| `Shift` | precision |
| `Esc` / right click | cancel |

On a mesh with no deform modifier the keys behave like stock Blender, so the
toggle can stay on.

## Measured

Accuracy, across Curve, Lattice, Armature, Simple Deform and chains of those,
with and without Subsurf and Mirror:

| Operation | Error |
|---|---|
| Move | 0.0005 - 0.002% |
| Rotate | 0.007% to 60 degrees, ~1% at 90 |
| Scale | 0.04% |
| Cancel restore | exact |

Speed, single-threaded:

| Selection | Rate |
|---|---|
| one card, 66 verts | ~1000 fps |
| 10 cards, 165 verts | ~260 fps |
| 50 cards, 1650 verts | ~45 fps |
| 100 cards, 6600 verts | ~14 fps |

Proportional falloff weights match Blender's own to 1e-06 across all seven
falloff types. Snapped vertices land on the target surface to 0.0.

## Notes

- Pivot point is respected, including 3D cursor.
- The add-on switches on "Display in Edit Mode" for the modifier when it needs
  to. A freshly added Curve modifier ships with it off, which is why nothing
  would appear to happen otherwise.
- Proportional falloff is fixed when the drag starts, same as Blender.
- Snap targets come from deformed geometry, so you snap onto what you see.
- Snapping sets position, not rotation. Align Rotation to Target is ignored.
- If the curve folds over on itself the inverse has no unique answer; the header
  warns rather than writing garbage.

## Assets

- `hair_deform_edit-1.6.0.zip` - the add-on, install this
- `demo.mp4` - the demo above, full quality
