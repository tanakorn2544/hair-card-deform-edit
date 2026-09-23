# Hair Card Deform Edit

Blender 4.2 add-on for editing hair cards that are bent by a Curve (or Lattice,
Armature, Simple Deform) modifier.

## The problem

Put a Curve modifier on a hair card and edit it. The card on screen is bent, but
the vertices you grab still live in the flat, undeformed cage. Drag a vertex
right and it goes somewhere else. The further the card bends, the worse it gets.

## What this does

Turn the toggle on and G / R / S work in the space you actually see. Drag a
vertex to the right, it goes right. The add-on inverts the modifier stack every
frame so the result lands where you put it.

Works with proportional editing and snapping.

## Install

Edit > Preferences > Add-ons > Install from Disk, pick the zip, enable it.

Blender 4.2 only.

## Use

Sidebar (N) > Hair Deform tab > big ON/OFF toggle.

While it is on:

- `G` / `R` / `S` - move, rotate, scale in deformed space
- `Shift+Alt+G` / `R` / `S` - same thing, always available even with the toggle off
- `X` / `Y` / `Z` - axis constraint, `Shift+X` etc for plane
- `Ctrl` - toggle snapping mid-drag
- Wheel / PageUp / PageDown - proportional falloff size
- `Shift` - precision
- `Esc` / right click - cancel

On a plain mesh with no deform modifier the keys behave like stock Blender, so
you can leave the toggle on.

The add-on switches on "Display in Edit Mode" for the modifier when it needs to.
A freshly added Curve modifier has it off, which is why nothing would happen
otherwise.

## Notes

- Pivot point setting is respected, including 3D cursor.
- Proportional falloff is fixed when the drag starts, same as Blender.
- Snap targets are read off deformed geometry, so you snap onto what you see.
- Snapping sets position, not rotation. Align Rotation to Target is ignored.
- If the curve folds over on itself the inverse has no unique answer. The header
  warns instead of writing garbage.

## Speed

Measured on a 4.2 build, single-threaded:

| selection | rate |
|---|---|
| one card, 66 verts | ~1000 fps |
| 10 cards, 165 verts selected | ~260 fps |
| 50 cards, 1650 verts selected | ~45 fps |
| 100 cards, 6600 verts selected | ~14 fps |

Accuracy stays under 0.01% across Curve, Lattice, Armature, Simple Deform, and
chains of those, with or without Subsurf and Mirror.

## License

GPL-2.0-or-later.
