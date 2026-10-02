Adds card clean-up tools: Add Card Segments, Card Length and Jump to Curve.

## Add Card Segments

Vertex > Add Card Segments (Ctrl+V), or the Clean Up section of the sidebar.

- Cuts the selected faces into more rows (Cuts = rows added per face).
- Follow Card (default): new rows land exactly on the modifier bend and the
  existing rows do not move.
- Round Corners: also moves the new rows to soften existing corners.
- UVs are cut along with the faces.

More rows on a bent card means each face is closer to a rectangle, so a straight
UV strip no longer shows a skewed texture.

## Card Length

Makes whole cards longer or shorter along their own length.

- Select any part of a card. The root stays, the width and bend are kept, and
  past the old tip the card carries on straight. On a Curve modifier the longer
  card follows the curve further.
- Drag toward the tip to lengthen, away from it to shorten, or type a number
  (1.5 = 1.5x). Shift for fine control, Ctrl for steps of 0.05.
- The root is the higher end of the card. A card on a Curve modifier grows from
  where its curve starts.
- Invert checkbox next to the button: grow from the other end. F while dragging
  swaps it for one go. Also in the redo panel.
- UVs are not changed; the texture stretches with the card, as with Scale.

## Jump to Curve

`Shift+Alt+C` goes from the selected cards to the curves that bend them, and from
a curve back to its cards.

- Edit or Object Mode is kept.
- Going back returns to the card you came from with the same vertices selected.
- A hidden curve is shown for the jump and hidden again on the way back.
- Also in the Select menu and the sidebar. Shift+Alt+C is unused in Blender 4.2.

## Known issues

- Smooth Card: on a card that is already smooth but has very uneven face sizes,
  the shape can shift slightly (about 0.02 units on a 4 unit card).
