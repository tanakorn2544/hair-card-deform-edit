![demo](https://raw.githubusercontent.com/tanakorn2544/hair-card-deform-edit/main/docs/demo.gif)

Four fixes, all measured against a control run.

## Multi-object edit mode

Selecting several hair cards and entering Edit Mode used to move only the active
object. The operator read `context.edit_object`, which is the active object
alone.

Every mesh in the edit session now gets its own solve - own cage, own Jacobian,
own original positions - and the selection moves as one rigid group, the same
way Blender's own transform behaves.

| | before | after |
|---|---|---|
| 3 cards in Edit Mode | 1 moved | 3 moved |
| Placement error | - | 1e-06 |
| Esc restore | - | 0.0 |

## Snapping

Snapping did nothing at all when the cards lived in one object, and pulled to
the wrong geometry when it did fire. Three separate causes:

- `BVHTree.FromObject` returns an **empty tree** for an object in Edit Mode, so
  the mesh being edited had no snap targets whatsoever.
- `scene.ray_cast` returns the **evaluated** object, not the original. An
  identity check against the original discarded every hit.
- Snapping required a ray to hit a face. A flat hair card seen edge-on is missed
  by the ray entirely.

Snapping is now a screen-space search with a pixel radius, which is how Blender
does it: the vertex, edge or face nearest the cursor, no ray hit required. The
vertices being dragged are excluded so the anchor cannot snap onto the geometry
it is carrying.

| element | snapped | control (snap off) |
|---|---|---|
| Vertex | 0.0 | 2.027 |
| Edge | 0.0 | 2.027 |
| Face | 0.0 | 2.027 |
| Edge midpoint | 0.0 | 2.027 |

## Snapping performance

Edge snapping re-projected every edge to screen space on every mouse move.

Edge endpoints are now projected once per drag, and candidates are bucketed into
a screen-space grid so a search only visits cells near the cursor.

Measured on a 60-card scalp, 1560 verts:

| | before | after |
|---|---|---|
| Edge snap frame | 11.63 ms | 4.50 ms |
| Edge snap rate | 86 fps | 222 fps |
| Search alone | 6.57 ms | 0.25 ms |

## Alt+S on a tapered card

Blender's Shrink/Fatten offsets the **cage** vertex along its normal. The Curve
modifier then rescales that offset by the control point radius, so on a card
whose curve tapers the same drag produces different thickness depending where
the vertex sits - thin spots where the taper is tightest.

The offset is now applied to the visible position along the visible normal.

| vertex along card | Blender | this add-on |
|---|---|---|
| root | 1.00x | 1.00x |
| middle | 0.66x | 1.00x |
| taper | 0.51x | 1.00x |
| spread | 0.487 | 0.001 |

Bound to `Alt+S` while the toggle is on, and `Shift+Alt+F` always.

## Install

1. Download `hair_deform_edit-1.7.0.zip` below
2. Edit > Preferences > Add-ons > Install from Disk
3. Pick the zip, enable it
4. Sidebar (N) > Hair Deform tab > turn the toggle ON

Blender 4.2 only. Restart Blender after upgrading - add-on code loads once at
startup, so an already-running session keeps the old version.

## Keys

| key | action |
|---|---|
| `G` / `R` / `S` | move / rotate / scale in deformed space |
| `Alt+S` | shrink/fatten along the visible normal |
| `Shift+Alt+G` / `R` / `S` | same, always available |
| `Shift+Alt+F` | shrink/fatten, always available |
| `X` / `Y` / `Z` | axis constraint |
| `Shift+X` / `Y` / `Z` | plane constraint |
| `Ctrl` | toggle snapping mid-drag |
| Wheel / PageUp / PageDown | proportional falloff size |
| `Shift` | precision |
| `Esc` / right click | cancel |

## Known issues

- Undo during a heavy edit session can be unstable. Save before undoing.
  Creating a datablock while in Edit Mode is what triggers it, and it reproduces
  in stock Blender with this add-on uninstalled.
- Snapping sets position, not rotation. Align Rotation to Target is read but not
  applied.
- If the curve folds over on itself the inverse has no unique answer. The header
  warns rather than writing garbage.
