Adds Smooth Card.

## Smooth Card

Takes the jags out of a card in the shape you see. Vertex > Smooth Card
(Ctrl+V), or the button in the sidebar panel.

- Smooths the edges of the card as it looks after the modifier, not the flat
  cage, so the overall bend is kept.
- Keeps the card width. Blender's Smooth Vertices collapses a bent card toward
  a line and moves the root and tip.
- The root, the tip and unselected vertices never move.
- Smooth (0-1, default 0.8) sets how much small detail is removed.
- Spacing: Keep leaves the rows where they are along the card; Even also gives
  every face the same length.

Measured on a bent card with jagged edges:

| | before | Smooth Card | Smooth Vertices |
|---|---|---|---|
| sharpest corner | 26 deg | 8 deg | 41 deg |
| width error | 20% | 7% | card collapsed |
| root / tip moved | - | 0 | moved |

Undo and redo behave the same as Blender's own smoothing tools.

## Known issues

- On a card that is already smooth but has very uneven face sizes, the shape
  can shift slightly (about 0.02 units on a 4 unit card).
