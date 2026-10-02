# SPDX-License-Identifier: GPL-2.0-or-later
"""Add Card Segments and Card Length.

A texture laid on a straight UV strip only stays straight on screen when every
face of the card is close to a rectangle. Blender draws each quad as two
triangles; on a trapezoid or a skewed quad the two triangles stretch the
texture differently and the strands bend along the hidden diagonal. Few, long
segments make it worse: the bend of the card is then concentrated in a few
sharp corners.

Add Card Segments cuts new rows into the selected part of a card. The new rows
follow the bend of the card exactly and the existing rows do not move.

The cut is made in the cage; the modifiers then bend the new rows onto the
curve the card already follows.

Card Length stretches whole cards along their own length (see below).
"""

import bisect

import bmesh
import bpy
import numpy as np
from bpy.props import BoolProperty, FloatProperty, IntProperty
from mathutils import Vector

from . import solver
from .smooth import _arc, _catmull, rail_chains, rail_edges


# ---------------------------------------------------------------------------
# Card grid
# ---------------------------------------------------------------------------

def card_grids(bm):
    """Every card in the mesh as a grid of vertex indices.

    Returns a list of grids; grid[c][k] is the vertex in column c (one long
    edge of the card, side to side in order) at row k (root/tip end to end).
    Cards that are not a clean rows x columns grid of quads are skipped and
    counted in the second return value.
    """
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    rails = rail_edges(bm)
    chains = rail_chains(bm, rails)
    where = {}
    for ci, ch in enumerate(chains):
        for k, v in enumerate(ch):
            # a vertex at the joint of two chains cannot be placed in a grid
            where.setdefault(v, []).append((ci, k))

    parent = list(range(len(chains)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    links = {}
    for e in bm.edges:
        if e.index in rails or e.hide:
            continue
        a, b = e.verts[0].index, e.verts[1].index
        wa, wb = where.get(a), where.get(b)
        if not wa or not wb or len(wa) != 1 or len(wb) != 1:
            continue
        (ca, ka), (cb, kb) = wa[0], wb[0]
        if ca == cb:
            continue
        links.setdefault((min(ca, cb), max(ca, cb)), []).append(
            (ka, kb) if ca < cb else (kb, ka))
        ra, rb = find(ca), find(cb)
        if ra != rb:
            parent[ra] = rb

    groups = {}
    for ci in range(len(chains)):
        groups.setdefault(find(ci), []).append(ci)

    grids = []
    skipped = 0
    for members in groups.values():
        if len(members) < 2:
            continue
        nbr = {c: [] for c in members}
        for (a, b) in links:
            if a in nbr and b in nbr:
                nbr[a].append(b)
                nbr[b].append(a)
        ends = [c for c in members if len(nbr[c]) == 1]
        if len(ends) != 2 or any(len(nbr[c]) > 2 for c in members):
            skipped += 1
            continue
        order = [ends[0]]
        while len(order) < len(members):
            nxt = [c for c in nbr[order[-1]] if c not in order]
            if not nxt:
                break
            order.append(nxt[0])
        if len(order) != len(members):
            skipped += 1
            continue
        n = len(chains[order[0]])
        if any(len(chains[c]) != n for c in order):
            skipped += 1
            continue
        cols = [list(chains[order[0]])]
        flipped = {order[0]: False}
        ok = True
        for prev, cur in zip(order, order[1:]):
            pairs = links[(min(prev, cur), max(prev, cur))]
            # express as (k in prev, k in cur) on the ORIGINAL chain indices
            if prev > cur:
                pairs = [(b, a) for a, b in pairs]
            fp = flipped[prev]
            same = all(((n - 1 - a) if fp else a) == b for a, b in pairs)
            rev = all(((n - 1 - a) if fp else a) == n - 1 - b for a, b in pairs)
            if same:
                flipped[cur] = False
                cols.append(list(chains[cur]))
            elif rev:
                flipped[cur] = True
                cols.append(list(reversed(chains[cur])))
            else:
                ok = False
                break
            if len(pairs) != n:
                ok = False
                break
        if ok:
            grids.append(cols)
        else:
            skipped += 1
    return grids, skipped


# ---------------------------------------------------------------------------
# Add segments
# ---------------------------------------------------------------------------
# New rows are cut half way along the cage. The modifier then bends them onto
# exactly the curve the card already follows, so the shape does not change
# (measured 3e-7 off the true card) and the old rows do not move. UVs are cut
# with the faces. To round off corners left by hand edits, run Smooth Card
# afterwards - with more rows it has more to work with.

class HAIRDEFORM_OT_add_card_segments(bpy.types.Operator):
    bl_idname = "hair_deform_edit.add_card_segments"
    bl_label = "Add Card Segments"
    bl_description = (
        "Cut new rows into the selected part of a card. The new rows follow "
        "the card's bend exactly, UVs are cut with it, and the existing rows "
        "stay where they are")
    bl_options = {'REGISTER', 'UNDO'}

    cuts: IntProperty(
        name="Cuts", default=1, min=1, max=16,
        description="New rows added inside every selected face")

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH'

    def execute(self, context):
        from . import ops
        cuts = int(self.cuts)
        added = 0
        skipped = 0
        for ob in ops._editable_objects(context):
            if ob.type != 'MESH' or not ob.data.total_vert_sel:
                continue
            bm = bmesh.from_edit_mesh(ob.data)
            bm.verts.ensure_lookup_table()
            bm.edges.ensure_lookup_table()
            grids, sk = card_grids(bm)
            skipped += sk
            sel = {v.index for v in bm.verts if v.select and not v.hide}
            edges = []
            for g in grids:
                rows = len(g[0])
                rowsel = [any(col[k] in sel for col in g) for k in range(rows)]
                for col in g:
                    for k in range(rows - 1):
                        if rowsel[k] and rowsel[k + 1]:
                            e = bm.edges.get((bm.verts[col[k]], bm.verts[col[k + 1]]))
                            if e is not None:
                                edges.append(e)
            if not edges:
                continue
            # Subdivide only adds vertices, and they come after the existing
            # ones. (Comparing BMVert wrappers from before the cut is not
            # reliable: it reported old vertices as new.)
            n0 = len(bm.verts)
            bmesh.ops.subdivide_edges(bm, edges=edges, cuts=cuts,
                                      use_grid_fill=True, smooth=0.0)
            bm.verts.index_update()
            bm.edges.index_update()
            bm.faces.index_update()
            bm.verts.ensure_lookup_table()
            added += len(bm.verts) - n0
            for i in range(n0, len(bm.verts)):
                bm.verts[i].select_set(True)
            bm.select_flush_mode()
            bmesh.update_edit_mesh(ob.data, loop_triangles=True,
                                   destructive=True)

        if not added:
            self.report({'WARNING'},
                        "Nothing to cut - select at least two neighbouring "
                        "rows of a card")
            return {'CANCELLED'}
        msg = "Added %d vertices" % added
        if skipped:
            msg += "; %d mesh part(s) are not a clean card grid and were left" % skipped
            self.report({'WARNING'}, msg)
        else:
            self.report({'INFO'}, msg)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Card length
# ---------------------------------------------------------------------------
# The card is stretched along its own length in the cage: every row slides
# along the card's centre line to f times its distance from the root and
# keeps its width and shape; past the old tip the card carries on straight.
# The modifiers then bend the longer card like any other, so a card on a
# Curve modifier grows along its curve. The root row never moves and UVs are
# left alone (the texture stretches with the card, as with Scale).

_PER = 16


def _visible_world(ob, bm):
    """World positions of every vertex as the card is seen (after deform)."""
    from . import ops
    prefs = ops._prefs()
    include_ns = bool(prefs.include_nonseparable) if prefs else False
    mw = ob.matrix_world
    try:
        proxy = solver.DeformProxy(ob, bm, include_ns)
        try:
            proxy.build()
            flat = proxy.evaluate()
            return [mw @ solver._get(flat, i) for i in range(len(bm.verts))]
        finally:
            proxy.free()
    except Exception:
        return [mw @ v.co for v in bm.verts]


def _root_is_row0(ob, bm, grid, W):
    """Which end of the card is the root.

    A card on a Curve modifier grows from where its curve starts. Any other
    card: hair hangs from the scalp, so the clearly higher end (as seen,
    world Z) is the root. A card lying about flat: the wider end, then the
    end nearer the object's origin.
    """
    for m in ob.modifiers:
        if m.type == 'CURVE' and m.show_viewport and m.object is not None:
            i = {'X': 0, 'Y': 1, 'Z': 2}[m.deform_axis[-1]]
            sign = -1.0 if m.deform_axis.startswith('NEG') else 1.0
            a = sum(bm.verts[col[0]].co[i] for col in grid) * sign
            b = sum(bm.verts[col[-1]].co[i] for col in grid) * sign
            if abs(a - b) > 1e-9:
                return a < b
    ends = [sum((W[col[k]] for col in grid), Vector()) / len(grid)
            for k in (0, -1)]
    mid = [sum((W[col[k]] for col in grid), Vector()) / len(grid)
           for k in range(len(grid[0]))]
    length = sum((mid[k + 1] - mid[k]).length for k in range(len(mid) - 1))
    dz = ends[0].z - ends[1].z
    if abs(dz) > 0.2 * max(length, 1e-9):
        return dz > 0.0
    w0 = (W[grid[-1][0]] - W[grid[0][0]]).length
    w1 = (W[grid[-1][-1]] - W[grid[0][-1]]).length
    if abs(w0 - w1) > 0.02 * max(w0, w1, 1e-9):
        return w0 > w1
    o = ob.matrix_world.translation
    return (ends[0] - o).length <= (ends[1] - o).length


class _LengthPlan:
    """One card, ready to be stretched to any length factor."""

    def __init__(self, ob, bm, grid, flip, W):
        if not (_root_is_row0(ob, bm, grid, W) ^ flip):
            grid = [list(reversed(col)) for col in grid]
        self.grid = grid
        ncol = len(grid)
        # root and tip as seen, for dragging along the card on screen
        self.root_world = sum((W[col[0]] for col in grid), Vector()) / ncol
        self.tip_world = sum((W[col[-1]] for col in grid), Vector()) / ncol
        ncol, rows = len(grid), len(grid[0])
        V = [[bm.verts[i].co.copy() for i in col] for col in grid]
        C = [sum((V[c][k] for c in range(ncol)), Vector()) / ncol
             for k in range(rows)]
        self.offsets = [[V[c][k] - C[k] for c in range(ncol)]
                        for k in range(rows)]
        if rows >= 3:
            dense = _catmull([np.array(tuple(c)) for c in C], _PER)
            per = _PER
        else:
            dense = [np.array(tuple(c)) for c in C]
            per = 1
        self.D = [Vector(tuple(p)) for p in dense]
        self.S = [float(x) for x in _arc([np.array(tuple(p)) for p in self.D])]
        self.s = [self.S[min(k * per, len(self.S) - 1)] for k in range(rows)]
        self.t0 = [self._at(sk)[1] for sk in self.s]

    def _at(self, s):
        """Point and direction of the centre line at distance s from the root."""
        D, S = self.D, self.S
        n = len(D)
        if n < 2:
            return D[0].copy(), Vector((0.0, 0.0, 1.0))
        if s >= S[-1]:
            j = n - 2
            while j > 0 and (D[j + 1] - D[j]).length < 1e-12:
                j -= 1
            t = (D[j + 1] - D[j]).normalized()
            return D[-1] + t * (s - S[-1]), t
        if s <= 0.0:
            t = (D[1] - D[0]).normalized()
            return D[0] + t * s, t
        j = min(max(bisect.bisect_right(S, s) - 1, 0), n - 2)
        seg = S[j + 1] - S[j]
        f = (s - S[j]) / seg if seg > 1e-12 else 0.0
        return D[j].lerp(D[j + 1], f), (D[j + 1] - D[j]).normalized()

    def length(self):
        return self.S[-1]

    def apply(self, bm, factor):
        for k, sk in enumerate(self.s):
            if k == 0:
                continue
            c, t = self._at(sk * factor)
            rot = self.t0[k].rotation_difference(t).to_matrix()
            for col, off in zip(self.grid, self.offsets[k]):
                bm.verts[col[k]].co = c + rot @ off


class HAIRDEFORM_OT_card_length(bpy.types.Operator):
    bl_idname = "hair_deform_edit.card_length"
    bl_label = "Card Length"
    bl_description = (
        "Make the selected cards longer or shorter along their own length. "
        "The root stays, the card keeps its width and bend, and past the old "
        "tip it carries on straight. Drag toward the tip to lengthen, away "
        "from it to shorten, or type a number")
    bl_options = {'REGISTER', 'UNDO'}

    length: FloatProperty(
        name="Length", default=1.0, min=0.05, soft_max=4.0, step=5,
        precision=3,
        description="New length of each card, as a multiple of its current length")
    flip: BoolProperty(
        name="Invert", default=False, options={'SKIP_SAVE'},
        description=(
            "Grow from the other end of the card: the tip stays and the root "
            "end moves. Starts from the Invert checkbox in the side panel"))

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH'

    # -- planning ------------------------------------------------------------
    def _plan(self, context):
        from . import ops
        plans = []
        skipped = 0
        for ob in ops._editable_objects(context):
            if ob.type != 'MESH' or not ob.data.total_vert_sel:
                continue
            bm = bmesh.from_edit_mesh(ob.data)
            bm.verts.ensure_lookup_table()
            bm.edges.ensure_lookup_table()
            grids, sk = card_grids(bm)
            sel = {v.index for v in bm.verts if v.select and not v.hide}
            picked = [g for g in grids
                      if any(i in sel for col in g for i in col)]
            W = _visible_world(ob, bm) if picked else None
            cards = [_LengthPlan(ob, bm, g, self.flip, W) for g in picked]
            # parts that are selected but are not a clean card
            if sk:
                incard = {i for g in grids for col in g for i in col}
                if any(i not in incard for i in sel):
                    skipped += sk
            if cards:
                orig = {i: bm.verts[i].co.copy()
                        for c in cards for col in c.grid for i in col}
                plans.append((ob, bm, cards, orig))
        return plans, skipped

    def _apply(self, factor):
        for ob, bm, cards, _orig in self._plans:
            for c in cards:
                c.apply(bm, factor)
            bmesh.update_edit_mesh(ob.data, loop_triangles=False,
                                   destructive=False)

    def _restore(self):
        for ob, bm, _cards, orig in self._plans:
            for i, co in orig.items():
                bm.verts[i].co = co
            bmesh.update_edit_mesh(ob.data, loop_triangles=False,
                                   destructive=False)

    def _report_done(self, skipped):
        n = sum(len(c) for _o, _b, c, _r in self._plans)
        msg = "Card length %.3gx on %d card(s)" % (self.length, n)
        if skipped:
            self.report({'WARNING'}, msg + "; parts that are not a clean card "
                        "grid were left")
        else:
            self.report({'INFO'}, msg)

    def _use_checkbox(self, context):
        """Take Invert from the side-panel checkbox unless this run set it
        (redo panel, a script, or a keymap entry)."""
        try:
            if self.properties.is_property_set("flip"):
                return
        except Exception:
            pass
        st = getattr(context.scene, "hair_deform_edit", None)
        self.flip = bool(getattr(st, "length_invert", False))

    # -- run -----------------------------------------------------------------
    def execute(self, context):
        self._use_checkbox(context)
        self._plans, skipped = self._plan(context)
        if not self._plans:
            self.report({'WARNING'}, "No card selected - select any part of a card")
            return {'CANCELLED'}
        self._apply(float(self.length))
        self._report_done(skipped)
        return {'FINISHED'}

    def invoke(self, context, event):
        self._use_checkbox(context)
        self._plans, self._skipped = self._plan(context)
        if not self._plans:
            self.report({'WARNING'}, "No card selected - select any part of a card")
            return {'CANCELLED'}
        self._value = 1.0
        self._typed = ""
        self._last = (event.mouse_region_x, event.mouse_region_y)
        self._precise = False
        self._snap = False
        self._screen_dir(context)
        self._show(context)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _screen_dir(self, context):
        """Root-to-tip direction of the selected cards on screen, and how many
        pixels make one card length. Pulling the mouse one card length toward
        the tip doubles the cards."""
        from bpy_extras.view3d_utils import location_3d_to_region_2d
        region = getattr(context, "region", None)
        rv3d = getattr(context, "region_data", None)
        sx = sy = 0.0
        lens = []
        if region is not None and rv3d is not None:
            for _ob, _bm, cards, _orig in self._plans:
                for c in cards:
                    a = location_3d_to_region_2d(region, rv3d, c.root_world)
                    b = location_3d_to_region_2d(region, rv3d, c.tip_world)
                    if a is None or b is None:
                        continue
                    d = b - a
                    if d.length > 1e-6:
                        sx += d.x
                        sy += d.y
                        lens.append(d.length)
        n = (sx * sx + sy * sy) ** 0.5
        if lens and n > 1e-6 and sum(lens) / len(lens) > 20.0:
            self._dir = (sx / n, sy / n)
            self._px = sum(lens) / len(lens)
        else:
            # card seen end-on: drag right to lengthen
            self._dir = (1.0, 0.0)
            self._px = 300.0

    def _factor(self):
        if self._typed:
            try:
                return max(0.05, float(self._typed))
            except ValueError:
                pass
        v = max(0.05, self._value)
        if self._snap:
            v = max(0.05, round(v / 0.05) * 0.05)
        return v

    def _show(self, context):
        if self._typed:
            txt = "Card Length: [%s|]x" % self._typed
        else:
            txt = "Card Length: %.3fx" % self._factor()
        if self.flip:
            txt += "   (Inverted: growing from the tip end)"
        if context.area:
            context.area.header_text_set(txt)
        if context.workspace:
            context.workspace.status_text_set(
                "Drag toward the tip: longer   Away: shorter   Type a number   "
                "Shift: precise   "
                "Ctrl: steps of 0.05   F: invert   "
                "Click/Enter: confirm   Right-click/Esc: cancel")

    def _clear(self, context):
        if context.area:
            context.area.header_text_set(None)
        if context.workspace:
            context.workspace.status_text_set(None)

    _DIGITS = {
        'ZERO': "0", 'ONE': "1", 'TWO': "2", 'THREE': "3", 'FOUR': "4",
        'FIVE': "5", 'SIX': "6", 'SEVEN': "7", 'EIGHT': "8", 'NINE': "9",
        'NUMPAD_0': "0", 'NUMPAD_1': "1", 'NUMPAD_2': "2", 'NUMPAD_3': "3",
        'NUMPAD_4': "4", 'NUMPAD_5': "5", 'NUMPAD_6': "6", 'NUMPAD_7': "7",
        'NUMPAD_8': "8", 'NUMPAD_9': "9", 'PERIOD': ".", 'NUMPAD_PERIOD': ".",
    }

    def modal(self, context, event):
        t = event.type
        if t in {'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE',
                 'TRACKPADPAN', 'TRACKPADZOOM'}:
            return {'PASS_THROUGH'}
        if t in {'LEFT_SHIFT', 'RIGHT_SHIFT'}:
            self._precise = event.value == 'PRESS'
            return {'RUNNING_MODAL'}
        if t in {'LEFT_CTRL', 'RIGHT_CTRL'}:
            self._snap = event.value == 'PRESS'
            self._apply(self._factor())
            self._show(context)
            return {'RUNNING_MODAL'}
        if t == 'MOUSEMOVE':
            x, y = event.mouse_region_x, event.mouse_region_y
            dx, dy = x - self._last[0], y - self._last[1]
            self._last = (x, y)
            if not self._typed:
                along = dx * self._dir[0] + dy * self._dir[1]
                self._value += along / self._px * (0.1 if self._precise else 1.0)
                self._apply(self._factor())
                self._show(context)
            return {'RUNNING_MODAL'}
        if event.value != 'PRESS':
            return {'RUNNING_MODAL'}
        if t in self._DIGITS:
            self._typed += self._DIGITS[t]
            self._apply(self._factor())
            self._show(context)
            return {'RUNNING_MODAL'}
        if t == 'BACK_SPACE':
            self._typed = self._typed[:-1]
            self._apply(self._factor())
            self._show(context)
            return {'RUNNING_MODAL'}
        if t == 'F':
            self._restore()
            self.flip = not self.flip
            self._plans, self._skipped = self._plan(context)
            self._screen_dir(context)
            self._apply(self._factor())
            self._show(context)
            return {'RUNNING_MODAL'}
        if t in {'LEFTMOUSE', 'RET', 'NUMPAD_ENTER', 'SPACE'}:
            self.length = self._factor()
            self._apply(self.length)
            self._clear(context)
            self._report_done(self._skipped)
            return {'FINISHED'}
        if t in {'RIGHTMOUSE', 'ESC'}:
            self._restore()
            self._clear(context)
            return {'CANCELLED'}
        return {'RUNNING_MODAL'}


def menu_func(self, context):
    self.layout.operator(HAIRDEFORM_OT_add_card_segments.bl_idname,
                         icon='MOD_ARRAY')
    self.layout.operator(HAIRDEFORM_OT_card_length.bl_idname,
                         icon='ARROW_LEFTRIGHT')


CLASSES = (HAIRDEFORM_OT_add_card_segments, HAIRDEFORM_OT_card_length)
