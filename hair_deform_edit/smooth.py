# SPDX-License-Identifier: GPL-2.0-or-later
"""Smooth Card - take the jags out of a card in the shape you see.

A card pushed around vertex by vertex ends up with corners along its long
edges and faces of very different lengths. Blender's Smooth Vertices works on
the flat cage, so on a bent card it does not smooth what is on screen, and on a
strip one face wide it averages each side toward the other and pulls the card
narrower.

Here every long edge of the card (a "rail") is smoothed on its own, in deformed
space, and the cage is then solved so the visible card lands on the smoothed
shape. The first and last row of each rail never move, so the root stays on
the scalp.
"""

import math

import bmesh
import bpy
import numpy as np
from bpy.props import EnumProperty, FloatProperty
from mathutils import Matrix, Vector

from . import solver


# ---------------------------------------------------------------------------
# Card structure
# ---------------------------------------------------------------------------

def _find(parent, x):
    root = x
    while parent[root] != root:
        root = parent[root]
    while parent[x] != root:
        parent[x], x = root, parent[x]
    return root


def _principal_axis(points):
    """Long direction of a point cloud."""
    n = len(points)
    if n < 2:
        return Vector((1.0, 0.0, 0.0))
    c = sum(points, Vector()) / n
    m = Matrix(((0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)))
    for p in points:
        d = p - c
        for r in range(3):
            for k in range(3):
                m[r][k] += d[r] * d[k]
    v = Vector((0.9, 0.5, 0.3)).normalized()
    for _ in range(60):
        w = m @ v
        if w.length < 1e-20:
            return Vector((1.0, 0.0, 0.0))
        v = w.normalized()
    return v


def rail_edges(bm):
    """Indices of the edges that run along the card.

    Opposite edges of a quad belong to the same edge ring. On a card the rungs
    (across the width) chain into rings that run the full length, while the
    rails only chain across the width. Of the two rings through a quad, the
    longer one therefore holds the rungs. A square patch (as many rows as
    columns) falls back to the long direction of the undeformed card.
    """
    bm.edges.ensure_lookup_table()
    parent = list(range(len(bm.edges)))
    quads = [f for f in bm.faces if len(f.loops) == 4 and not f.hide]
    for f in quads:
        l0, l1, l2, l3 = list(f.loops)
        for a, b in ((l0.edge.index, l2.edge.index),
                     (l1.edge.index, l3.edge.index)):
            ra, rb = _find(parent, a), _find(parent, b)
            if ra != rb:
                parent[ra] = rb

    size = {}
    for e in bm.edges:
        r = _find(parent, e.index)
        size[r] = size.get(r, 0) + 1

    vote = {}
    ties = []
    for f in quads:
        l0, l1 = list(f.loops)[:2]
        a = _find(parent, l0.edge.index)
        b = _find(parent, l1.edge.index)
        if a == b:
            continue
        if size[a] > size[b]:
            vote[a] = vote.get(a, 0) + 1
            vote[b] = vote.get(b, 0) - 1
        elif size[b] > size[a]:
            vote[b] = vote.get(b, 0) + 1
            vote[a] = vote.get(a, 0) - 1
        else:
            ties.append((f, a, b, l0.edge, l1.edge))

    if ties:
        # Islands, for the long axis of each undeformed card.
        vpar = list(range(len(bm.verts)))
        for e in bm.edges:
            a, b = e.verts
            ra, rb = _find(vpar, a.index), _find(vpar, b.index)
            if ra != rb:
                vpar[ra] = rb
        members = {}
        for v in bm.verts:
            members.setdefault(_find(vpar, v.index), []).append(v.co.copy())
        axes = {}
        for f, a, b, ea, eb in ties:
            isl = _find(vpar, f.verts[0].index)
            ax = axes.get(isl)
            if ax is None:
                ax = axes[isl] = _principal_axis(members[isl])
            da = ea.verts[1].co - ea.verts[0].co
            db = eb.verts[1].co - eb.verts[0].co
            ta = abs(da.normalized().dot(ax)) if da.length > 1e-12 else 0.0
            tb = abs(db.normalized().dot(ax)) if db.length > 1e-12 else 0.0
            rail, rung = (a, b) if ta >= tb else (b, a)
            vote[rail] = vote.get(rail, 0) - 1
            vote[rung] = vote.get(rung, 0) + 1

    return {e.index for e in bm.edges
            if not e.hide and vote.get(_find(parent, e.index), 0) < 0}


def rail_chains(bm, rails):
    """Ordered vertex chains along each rail, end to end.

    Chains start and stop at a vertex that does not have exactly two rail
    neighbours (the root and tip rows, or a junction). Closed rails are left
    out: they have no ends to hold in place.
    """
    nbr = {}
    for ei in rails:
        a, b = bm.edges[ei].verts
        nbr.setdefault(a.index, []).append(b.index)
        nbr.setdefault(b.index, []).append(a.index)
    chains = []
    seen = set()
    for v, ns in nbr.items():
        if len(ns) == 2:
            continue
        for n in ns:
            key = (v, n) if v < n else (n, v)
            if key in seen:
                continue
            seen.add(key)
            chain = [v]
            prev, cur = v, n
            while True:
                chain.append(cur)
                ns2 = nbr[cur]
                if len(ns2) != 2:
                    break
                nxt = ns2[0] if ns2[1] == prev else ns2[1]
                key = (cur, nxt) if cur < nxt else (nxt, cur)
                if key in seen:
                    break
                seen.add(key)
                prev, cur = cur, nxt
            chains.append(chain)
    return chains


# ---------------------------------------------------------------------------
# Smoothing
# ---------------------------------------------------------------------------
# Each rail is solved as a penalised least-squares fit: stay close to where
# the vertices are, while keeping the third derivative along the rail small.
# A constant bend has a zero third derivative, so the overall curve of the
# card costs nothing and is kept; a corner or a single row pushed out of line
# is expensive and gets flattened out. Neighbour-averaging (Blender's Smooth
# Vertices, Laplacian) instead pulls every bend a little straighter per pass.
#
# Measured on a 14-row card bent 120 degrees: a 25 degree corner drops to
# about 4 degrees while a clean card moves under 0.005 of its length.

def _arc(points):
    s = [0.0]
    for a, b in zip(points, points[1:]):
        s.append(s[-1] + float(np.linalg.norm(b - a)))
    return s


def strength_to_alpha(strength):
    # 0.0 -> 0.01 (barely touches it), 1.0 -> 100 (only the overall bend left)
    return 10.0 ** (4.0 * strength - 2.0)


def fit_rail(points, movable, alpha):
    """Positions for one rail; entries where ``movable`` is False stay put."""
    P = np.asarray(points, dtype=float)
    n = len(P)
    if n < 4 or not any(movable):
        return P.copy()
    s = np.asarray(_arc(list(P)))
    total = s[-1]
    if total < 1e-12:
        return P.copy()
    rows = []
    for k in range(n - 3):
        ss = s[k:k + 4]
        span = ss[3] - ss[0]
        if span < 1e-12:
            continue
        c = np.zeros(n)
        ok = True
        for i in range(4):
            den = 1.0
            for j in range(4):
                if j != i:
                    den *= (ss[i] - ss[j])
            if abs(den) < 1e-30:
                ok = False
                break
            c[k + i] = 1.0 / den
        if not ok:
            continue
        # Scaled to be unit-free, and the same for a dense or a sparse part
        # of the card - otherwise dense rows would be smoothed far harder.
        rows.append(c * (span ** 3) / 6.0 * math.sqrt(span / total))
    if not rows:
        return P.copy()
    D = np.asarray(rows)
    A = np.eye(n) + alpha * (D.T @ D)
    free = [i for i in range(n) if movable[i]]
    fixed = [i for i in range(n) if not movable[i]]
    X = P.copy()
    rhs = P[free]
    if fixed:
        rhs = rhs - A[np.ix_(free, fixed)] @ P[fixed]
    try:
        X[free] = np.linalg.solve(A[np.ix_(free, free)], rhs)
    except np.linalg.LinAlgError:
        return P.copy()
    return X


def _catmull(P, per=8):
    """Densify a polyline with a centripetal Catmull-Rom spline."""
    n = len(P)
    if n < 3:
        return [np.asarray(p, dtype=float) for p in P]
    # Phantom end points by quadratic extrapolation, so the first and last
    # segment keep the rail's curvature instead of going straight.
    ext = ([3 * P[0] - 3 * P[1] + P[2]] + list(P)
           + [3 * P[-1] - 3 * P[-2] + P[-3]])
    out = []
    for k in range(1, n):
        p0, p1, p2, p3 = ext[k - 1], ext[k], ext[k + 1], ext[k + 2]

        def tj(ti, a, b):
            return ti + max(float(np.linalg.norm(b - a)), 1e-12) ** 0.5
        t0 = 0.0
        t1 = tj(t0, p0, p1)
        t2 = tj(t1, p1, p2)
        t3 = tj(t2, p2, p3)
        for m in range(per):
            t = t1 + (t2 - t1) * m / per
            a1 = (t1 - t) / (t1 - t0) * p0 + (t - t0) / (t1 - t0) * p1
            a2 = (t2 - t) / (t2 - t1) * p1 + (t - t1) / (t2 - t1) * p2
            a3 = (t3 - t) / (t3 - t2) * p2 + (t - t2) / (t3 - t2) * p3
            b1 = (t2 - t) / (t2 - t0) * a1 + (t - t0) / (t2 - t0) * a2
            b2 = (t3 - t) / (t3 - t1) * a2 + (t - t1) / (t3 - t1) * a3
            out.append((t2 - t) / (t2 - t1) * b1 + (t - t1) / (t2 - t1) * b2)
    out.append(np.asarray(P[-1], dtype=float))
    return out


def _resample(points, fracs):
    s = _arc(points)
    total = s[-1]
    out = []
    j = 0
    last = len(points) - 2
    for t in fracs:
        d = t * total
        while j < last and s[j + 1] < d:
            j += 1
        seg = s[j + 1] - s[j]
        f = (d - s[j]) / seg if seg > 1e-12 else 0.0
        f = 0.0 if f < 0.0 else (1.0 if f > 1.0 else f)
        out.append(points[j] * (1.0 - f) + points[j + 1] * f)
    return out


def smooth_rail(points, movable, strength, spacing):
    """New positions for one rail (list of Vectors). Only movable ones change.

    Spacing along the rail:
      KEEP  - each row keeps its share of the length between its fixed rows
      EVEN  - every face between two fixed rows gets the same length
    """
    P = np.asarray([tuple(p) for p in points], dtype=float)
    X = fit_rail(P, movable, strength_to_alpha(strength))
    n = len(P)
    k = 0
    while k < n:
        if not movable[k]:
            k += 1
            continue
        r0 = k
        while k < n and movable[k]:
            k += 1
        lo, hi = r0 - 1, k                      # the fixed rows around the run
        if lo < 0 or hi >= n:
            continue
        s0 = _arc(list(P[lo:hi + 1]))
        if s0[-1] < 1e-12:
            continue
        fr = [x / s0[-1] for x in s0]
        if spacing == 'EVEN':
            m = hi - lo
            fr = [j / m for j in range(m + 1)]
        # Place the run on the smoothed curve between the two fixed rows,
        # read off a dense spline so no new corners appear at the old rows.
        a = max(0, lo - 1)
        b = min(n - 1, hi + 1)
        dense = _catmull(X[a:b + 1])
        per = 8
        i0 = (lo - a) * per
        i1 = (hi - a) * per
        piece = dense[i0:i1 + 1]
        new = _resample(piece, fr)
        for j in range(1, hi - lo):
            X[lo + j] = new[j]
    return [Vector(tuple(x)) for x in X]


# ---------------------------------------------------------------------------
# Operator
# ---------------------------------------------------------------------------

class HAIRDEFORM_OT_smooth_card(bpy.types.Operator):
    bl_idname = "hair_deform_edit.smooth_card"
    bl_label = "Smooth Card"
    bl_description = (
        "Smooth the jags out of the selected part of a card, in the shape you "
        "see. Each long edge is smoothed on its own, so the card keeps its "
        "width. The root and tip rows do not move")
    bl_options = {'REGISTER', 'UNDO'}

    factor: FloatProperty(
        name="Smooth", default=0.8, min=0.0, max=1.0,
        description=("How much detail to remove. Low keeps small waves, "
                     "high leaves only the overall bend"))
    spacing: EnumProperty(
        name="Spacing",
        items=(
            ('KEEP', "Keep", "Rows stay where they are along the card; only "
             "the shape is smoothed"),
            ('EVEN', "Even", "Also give every face along the card the same "
             "length"),
        ),
        default='KEEP')

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH'

    def execute(self, context):
        from . import ops
        prefs = ops._prefs()
        include_ns = bool(prefs.include_nonseparable) if prefs else False
        max_iter = max(int(prefs.max_iterations) if prefs else 10, 10)
        tol = float(prefs.tolerance) if prefs else 1e-6
        eps = float(prefs.epsilon) if prefs else 1e-3

        moved = 0
        worst = 0.0
        missed = 0
        touched = 0
        for ob in ops._editable_objects(context):
            if ob.type != 'MESH' or not ob.data.total_vert_sel:
                continue
            bm = bmesh.from_edit_mesh(ob.data)
            bm.verts.ensure_lookup_table()
            bm.edges.ensure_lookup_table()
            sel = {v.index for v in bm.verts if v.select and not v.hide}
            chains = rail_chains(bm, rail_edges(bm))
            work = []
            for chain in chains:
                mov = [0 < k < len(chain) - 1 and chain[k] in sel
                       for k in range(len(chain))]
                if any(mov):
                    work.append((chain, mov))
            if not work:
                continue

            proxy = solver.DeformProxy(ob, bm, include_ns)
            try:
                proxy.build()
                flat = proxy.evaluate()
                mw = proxy.matrix_world
                targets = {}
                for chain, mov in work:
                    p0 = [mw @ solver._get(flat, i) for i in chain]
                    ps = smooth_rail(p0, mov, float(self.factor),
                                     self.spacing)
                    for k, i in enumerate(chain):
                        if mov[k]:
                            targets[i] = ps[k]
                if not targets:
                    continue
                inv = proxy.matrix_world_inv
                local = {i: inv @ p for i, p in targets.items()}
                jac, _ = proxy.jacobian(list(local), eps)
                coords, res, _ = proxy.solve_targets(
                    local, jac=jac, max_iter=max_iter, tol=tol, eps=eps)
                m3 = proxy.matrix3
            finally:
                proxy.free()

            span = 0.0
            for i, p in targets.items():
                span = max(span, (p - (mw @ solver._get(flat, i))).length)
            for i, co in coords.items():
                bm.verts[i].co = co
                r = (m3 @ res[i]).length
                worst = max(worst, r)
                if r > max(1e-4, 0.01 * span):
                    missed += 1
            bmesh.update_edit_mesh(ob.data, loop_triangles=False,
                                   destructive=False)
            moved += len(coords)
            touched += 1

        if not moved:
            self.report({'WARNING'},
                        "Nothing to smooth - select the card rows between "
                        "its root and tip")
            return {'CANCELLED'}
        if missed:
            self.report({'WARNING'},
                        "Smoothed %d vertices; %d could not reach the smooth "
                        "shape (the curve folds there)" % (moved, missed))
        else:
            self.report({'INFO'}, "Smoothed %d vertices" % moved)
        return {'FINISHED'}


def menu_func(self, context):
    self.layout.operator(HAIRDEFORM_OT_smooth_card.bl_idname,
                         icon='MOD_SMOOTH')


CLASSES = (HAIRDEFORM_OT_smooth_card,)
