Fixes proportional editing on bent cards.

## Proportional editing kinked the card

The falloff was measured on the unbent cage instead of the card you see. On a
curved card that gave nearby vertices the wrong share of the move, so the card
kinked where the selection met the falloff. Distances are now measured on the
bent card.

Measured against Blender's own proportional move on the same visible card:

| Card | Before | After |
|---|---|---|
| Hook | 32% off | 0.3% |
| Hook, Connected Only | 24% off | 0.3% |
| Tapered hook | 23% off | 0.1% |
| Gentle curve | 4-6% off | 0.0% |

## Connected Only

Surface distance now spreads across faces as well as along edges, the same way
Blender measures it. Walking edges only overestimated diagonal distances.

## Falloff circle

The circle drew as an oval (about 2.4x taller than wide). It is round now and
matches Blender's radius.

## Known issues

- Ctrl+Z in Edit Mode can crash Blender 4.2 (reproduced without the add-on).
  Save before undoing.
- Snapping sets position only; Align Rotation to Target is ignored.
