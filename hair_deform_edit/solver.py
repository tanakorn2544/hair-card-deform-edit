# SPDX-License-Identifier: GPL-2.0-or-later
"""Deform-space solver.

The whole addon rests on one property of Blender's *deform* modifiers: the
deformed position of vertex i depends only on the base position of vertex i.
That separability means

  * the full per-vertex Jacobian costs 3 evaluations, not 3*N, and
  * every selected vertex can be Newton-solved in parallel, so one drag step
    costs a fixed handful of evaluations no matter how much is selected.

Modifiers that mix neighbours (smoothing, laplacian) break that assumption and
are excluded from the proxy unless the user opts in.
"""

import array
import math

import bmesh
import bpy
from mathutils import Matrix, Vector

# Deform modifiers where each vertex moves independently of its neighbours.
SEPARABLE_DEFORM = {
    'ARMATURE',
    'CAST',
    'CURVE',
    'DISPLACE',
    'HOOK',
    'LATTICE',
    'MESH_DEFORM',
    'SHRINKWRAP',
    'SIMPLE_DEFORM',
    'SURFACE_DEFORM',
    'WARP',
    'WAVE',
}

# Deform modifiers that mix neighbouring vertices: the Jacobian trick is only
# an approximation for these, so they are opt-in.
NONSEPARABLE_DEFORM = {
    'CORRECTIVE_SMOOTH',
    'LAPLACIANDEFORM',
    'LAPLACIANSMOOTH',
    'SMOOTH',
}

ALL_DEFORM = SEPARABLE_DEFORM | NONSEPARABLE_DEFORM

PROXY_OBJECT_NAME = "HD_deform_proxy"
PROXY_MESH_NAME = "HD_deform_proxy_mesh"


def visible_deform_modifiers(ob, include_nonseparable=False):
    """Deform modifiers that actually contribute to what the user sees."""
    allowed = SEPARABLE_DEFORM | (NONSEPARABLE_DEFORM if include_nonseparable else set())
    out = []
    for md in ob.modifiers:
        if md.type not in allowed:
            continue
        if not md.show_viewport:
            continue
        # In edit mode only modifiers flagged for edit-mode display are on screen.
        if ob.mode == 'EDIT' and not md.show_in_editmode:
            continue
        out.append(md)
    return out


def has_editmode_deform(ob):
    return bool(visible_deform_modifiers(ob))


def deformers_for_edit(ob, include_nonseparable=False):
    """Deform modifiers that COULD drive edit-mode display on this object.

    Unlike ``visible_deform_modifiers`` this ignores ``show_in_editmode``. A
    freshly added Curve modifier has both edit-mode display flags switched off,
    so a check that requires them reports "no deform here" on exactly the object
    the user is trying to edit. This answers "is there something to invert?"; the
    caller is responsible for switching the display flags on.
    """
    allowed = SEPARABLE_DEFORM | (NONSEPARABLE_DEFORM if include_nonseparable else set())
    return [md for md in ob.modifiers
            if md.type in allowed and md.show_viewport]


def enable_cage_display(ob, include_nonseparable=False):
    """Turn on edit-mode + on-cage display for this object's deformers.

    Returns the names of the modifiers that were actually changed, so the caller
    can tell the user what happened instead of silently editing their setup.
    """
    changed = []
    for md in deformers_for_edit(ob, include_nonseparable):
        touched = False
        if not md.show_in_editmode:
            md.show_in_editmode = True
            touched = True
        if not md.show_on_cage:
            md.show_on_cage = True
            touched = True
        if touched:
            changed.append(md.name)
    return changed


def cage_ready(ob):
    """True when every visible deform modifier also draws on the edit cage."""
    mods = visible_deform_modifiers(ob)
    return bool(mods) and all(md.show_on_cage for md in mods)


def _flat(n):
    return array.array('f', [0.0]) * (n * 3)


def _get(flat, i):
    j = i * 3
    return Vector((flat[j], flat[j + 1], flat[j + 2]))


def _set(flat, i, co):
    j = i * 3
    flat[j] = co[0]
    flat[j + 1] = co[1]
    flat[j + 2] = co[2]


def _finite(v):
    return (v.x == v.x and v.y == v.y and v.z == v.z
            and abs(v.x) < 1e18 and abs(v.y) < 1e18 and abs(v.z) < 1e18)


class DeformProxy:
    """A hidden stand-in object carrying only the deform part of the stack.

    Evaluating the real object in edit mode is unreliable (``evaluated_get().data``
    returns an empty mesh) and topology modifiers such as Subdivision destroy the
    1:1 vertex mapping we need. A proxy that holds a copy of the edit-cage
    geometry plus only the deform modifiers keeps indices aligned and is cheaper
    to re-evaluate.
    """

    def __init__(self, ob, bm, include_nonseparable=False):
        self.ob = ob
        self.bm = bm
        self.include_nonseparable = include_nonseparable
        self.proxy_ob = None
        self.proxy_me = None
        self.count = 0
        self.base = None          # flat base coords currently pushed
        self._scratch = None
        self.matrix_world = ob.matrix_world.copy()
        self.matrix_world_inv = self.matrix_world.inverted_safe()
        self.matrix3 = self.matrix_world.to_3x3()
        self.modifier_types = []

    # -- lifecycle ---------------------------------------------------------
    def build(self):
        ob = self.ob
        bm = self.bm
        bm.verts.ensure_lookup_table()
        self.count = len(bm.verts)

        me = bpy.data.meshes.new(PROXY_MESH_NAME)
        bm_copy = bm.copy()
        try:
            bm_copy.to_mesh(me)
        finally:
            bm_copy.free()

        proxy = bpy.data.objects.new(PROXY_OBJECT_NAME, me)
        proxy.matrix_world = ob.matrix_world.copy()

        mods = visible_deform_modifiers(ob, self.include_nonseparable)
        self.modifier_types = [md.type for md in mods]
        needed_groups = set()
        for src in mods:
            dst = proxy.modifiers.new(src.name, src.type)
            for prop in src.bl_rna.properties:
                if prop.is_readonly or prop.identifier in {"name", "type"}:
                    continue
                try:
                    setattr(dst, prop.identifier, getattr(src, prop.identifier))
                except Exception:
                    pass
            # Force it on for our evaluation regardless of edit-mode display flags.
            try:
                dst.show_viewport = True
                dst.show_in_editmode = True
            except Exception:
                pass
            vg = getattr(src, "vertex_group", "")
            if vg:
                needed_groups.add(vg)

        bpy.context.scene.collection.objects.link(proxy)
        self.proxy_ob = proxy
        self.proxy_me = me

        if needed_groups:
            self._copy_vertex_groups(needed_groups)

        # hide_set() keeps the modifier stack evaluating; hide_viewport = True
        # would silently disable it and hand back undeformed coordinates.
        try:
            proxy.hide_set(True)
        except Exception:
            pass
        proxy.hide_render = True

        self.base = _flat(self.count)
        me.vertices.foreach_get("co", self.base)
        self._scratch = array.array('f', self.base)
        bpy.context.view_layer.update()
        return self

    def _copy_vertex_groups(self, names):
        ob, bm, proxy = self.ob, self.bm, self.proxy_ob
        layer = bm.verts.layers.deform.active
        if layer is None:
            return
        index_by_name = {}
        for name in names:
            src = ob.vertex_groups.get(name)
            if src is None:
                continue
            proxy.vertex_groups.new(name=name)
            index_by_name[src.index] = name
        if not index_by_name:
            return
        buckets = {name: [] for name in index_by_name.values()}
        for i, v in enumerate(bm.verts):
            dvert = v[layer]
            for gidx, name in index_by_name.items():
                w = dvert.get(gidx)
                if w:
                    buckets[name].append((i, w))
        for name, pairs in buckets.items():
            vg = proxy.vertex_groups.get(name)
            if vg is None:
                continue
            for idx, w in pairs:
                vg.add([idx], w, 'REPLACE')

    def free(self):
        if self.proxy_ob is not None:
            try:
                bpy.data.objects.remove(self.proxy_ob, do_unlink=True)
            except Exception:
                pass
            self.proxy_ob = None
        if self.proxy_me is not None:
            try:
                bpy.data.meshes.remove(self.proxy_me, do_unlink=True)
            except Exception:
                pass
            self.proxy_me = None

    def __enter__(self):
        return self.build()

    def __exit__(self, *exc):
        self.free()
        return False

    # -- evaluation --------------------------------------------------------
    def push(self, flat):
        self.proxy_me.vertices.foreach_set("co", flat)
        self.proxy_me.update()

    def evaluate(self):
        """Deformed coordinates, object-local space, index-aligned with the bmesh."""
        bpy.context.view_layer.update()
        depsgraph = bpy.context.evaluated_depsgraph_get()
        ev = self.proxy_ob.evaluated_get(depsgraph)
        me = ev.to_mesh()
        n = len(me.vertices)
        out = _flat(n)
        me.vertices.foreach_get("co", out)
        ev.to_mesh_clear()
        return out

    def evaluate_with(self, flat):
        self.push(flat)
        return self.evaluate()

    # -- differential ------------------------------------------------------
    def jacobian(self, indices, eps=1e-3):
        """Per-vertex d(deformed)/d(base) as 3x3 matrices, in 3 evaluations.

        Only valid because separable deformers let every vertex be nudged at once.
        """
        return self.jacobian_at(self.base, indices, eps)

    def jacobian_at(self, flat, indices, eps=1e-3):
        """Jacobian measured around an arbitrary base configuration."""
        base_eval = self.evaluate_with(flat)
        columns = []
        for axis in range(3):
            probe = array.array('f', flat)
            for i in range(axis, len(probe), 3):
                probe[i] += eps
            columns.append(self.evaluate_with(probe))
        self.push(flat)

        out = {}
        for idx in indices:
            b = _get(base_eval, idx)
            cx = (_get(columns[0], idx) - b) / eps
            cy = (_get(columns[1], idx) - b) / eps
            cz = (_get(columns[2], idx) - b) / eps
            out[idx] = Matrix((
                (cx.x, cy.x, cz.x),
                (cx.y, cy.y, cz.y),
                (cx.z, cy.z, cz.z),
            ))
        return out, base_eval

    def _solve_pass(self, targets, jac=None, max_iter=10, tol=1e-6, eps=1e-3,
                    adaptive=True, start=None):
        """One Newton solve. See ``solve_targets`` for the public entry point.

        ``targets`` maps vertex index -> desired deformed position (local space).
        Returns (base_coords_by_index, residual_by_index, iterations_used).

        Every vertex carries its own damping, its own best-so-far result and its
        own converged flag. Convergence state must be per-vertex: a single hard
        vertex (one sitting where the deform folds over itself, say) would
        otherwise hold the whole selection at its starting position. Vertices
        drop out of the active set as they converge, and the shared Jacobian
        re-measurement is only paid while some vertex still needs it.

        ``start`` warm-starts from a previous solution. During a drag the target
        moves a little per frame, so the previous answer is nearly right and the
        solve finishes in one or two evaluations instead of ten.

        The inner loop runs on raw floats rather than Vector/Matrix objects:
        with a few thousand vertices selected the allocation of a temporary
        Vector per vertex per iteration costs more than the actual arithmetic.
        """
        indices = list(targets.keys())
        if not indices:
            return {}, {}, 0

        n3 = self.count * 3
        guess = array.array('f', self.base)
        if start:
            for i, co in start.items():
                if i < self.count and _finite(co):
                    _set(guess, i, co)

        # Flat target array, and flat 9-float inverse Jacobians.
        tgt = array.array('d', [0.0]) * n3
        for i in indices:
            co = targets[i]
            j = i * 3
            tgt[j] = co[0]
            tgt[j + 1] = co[1]
            tgt[j + 2] = co[2]

        def flatten_inv(mats, keys):
            out = {}
            for i in keys:
                m = mats[i].inverted_safe()
                out[i] = (m[0][0], m[0][1], m[0][2],
                          m[1][0], m[1][1], m[1][2],
                          m[2][0], m[2][1], m[2][2])
            return out

        inv = flatten_inv(jac, indices) if jac is not None else None

        got = self.evaluate_with(guess)

        damp = {}
        prev = {}
        best_err = {}
        best_co = {}
        res = {}
        active = list(indices)
        tol2 = tol * tol
        used = 0
        LIMIT = 1e18

        for step in range(max_iter):
            used = step + 1
            stalled = 0
            still = []
            for i in active:
                j = i * 3
                rx = tgt[j] - got[j]
                ry = tgt[j + 1] - got[j + 1]
                rz = tgt[j + 2] - got[j + 2]
                e2 = rx * rx + ry * ry + rz * rz
                # NaN fails every comparison, so this also filters blown-up verts.
                if not (e2 < LIMIT):
                    continue
                res[i] = (rx, ry, rz)

                prior = best_err.get(i)
                if prior is None or e2 < prior:
                    best_err[i] = e2
                    best_co[i] = (guess[j], guess[j + 1], guess[j + 2])

                if e2 < tol2:
                    continue

                p = prev.get(i)
                d = damp.get(i, 1.0)
                if p is not None:
                    if e2 > p:
                        d = d * 0.5
                        if d < 0.125:
                            d = 0.125
                        stalled += 1
                    elif adaptive and e2 > 0.09 * p:   # 0.3 in distance terms
                        stalled += 1
                    else:
                        d = d * 1.5
                        if d > 1.0:
                            d = 1.0
                    damp[i] = d
                prev[i] = e2
                still.append(i)

            active = still
            if not active:
                break

            # Re-measuring costs 3 evaluations for the whole mesh at once, so it
            # is worth it as soon as a meaningful share of the remaining
            # vertices is converging slowly.
            if inv is None or (adaptive and stalled * 2 >= len(active)):
                jac_now, got_now = self.jacobian_at(guess, active, eps)
                inv = flatten_inv(jac_now, active)
                got = got_now
                for i in active:
                    j = i * 3
                    rx = tgt[j] - got[j]
                    ry = tgt[j + 1] - got[j + 1]
                    rz = tgt[j + 2] - got[j + 2]
                    e2 = rx * rx + ry * ry + rz * rz
                    if not (e2 < LIMIT):
                        continue
                    res[i] = (rx, ry, rz)
                    if e2 < best_err.get(i, LIMIT):
                        best_err[i] = e2
                        best_co[i] = (guess[j], guess[j + 1], guess[j + 2])

            for i in active:
                r = res.get(i)
                if r is None:
                    continue
                m = inv[i]
                rx, ry, rz = r
                d = damp.get(i, 1.0)
                dx = (m[0] * rx + m[1] * ry + m[2] * rz) * d
                dy = (m[3] * rx + m[4] * ry + m[5] * rz) * d
                dz = (m[6] * rx + m[7] * ry + m[8] * rz) * d
                j = i * 3
                nx = guess[j] + dx
                ny = guess[j + 1] + dy
                nz = guess[j + 2] + dz
                if -LIMIT < nx < LIMIT and -LIMIT < ny < LIMIT and -LIMIT < nz < LIMIT:
                    guess[j] = nx
                    guess[j + 1] = ny
                    guess[j + 2] = nz
            got = self.evaluate_with(guess)

        # Per vertex, keep whichever attempt actually landed closest.
        out = {}
        final_res = {}
        for i in indices:
            j = i * 3
            rx = tgt[j] - got[j]
            ry = tgt[j + 1] - got[j + 1]
            rz = tgt[j + 2] - got[j + 2]
            e2 = rx * rx + ry * ry + rz * rz
            be = best_err.get(i)
            if not (e2 < LIMIT) or (be is not None and be < e2):
                co = best_co.get(i)
                if co is None:
                    out[i] = _get(self.base, i)
                    final_res[i] = Vector((0.0, 0.0, 0.0))
                else:
                    out[i] = Vector(co)
                    # residual belonging to that best attempt
                    final_res[i] = Vector((0.0, 0.0, 0.0)) if be is None else \
                        Vector((0.0, 0.0, be ** 0.5))
            else:
                out[i] = Vector((guess[j], guess[j + 1], guess[j + 2]))
                final_res[i] = Vector((rx, ry, rz))

        self.push(self.base)
        return out, final_res, used

    def solve_targets(self, targets, jac=None, max_iter=10, tol=1e-6, eps=1e-3,
                      adaptive=True, start=None, continuation=6):
        """Solve, then rescue any vertex that got stuck far from its target.

        A single Newton solve is local: it follows the deform's gradient downhill
        from where the vertex currently is. For a large rotation the correct base
        position can be far away along the curve - past a ridge the gradient does
        not cross - and Newton settles into a local minimum instead.

        Vertices that miss are retried by continuation: walk the target there in
        ``continuation`` small hops, re-solving and warm-starting at each one, so
        every hop stays inside the region where the local model is valid. Only
        the stuck vertices pay for this, and only when a solve actually misses,
        so ordinary dragging is unaffected.
        """
        out, res, used = self._solve_pass(
            targets, jac=jac, max_iter=max_iter, tol=tol, eps=eps,
            adaptive=adaptive, start=start)

        if not continuation or not targets:
            return out, res, used

        # Fast path: if every vertex already converged, skip the rescue pass
        # entirely. This is the common case while dragging, and it saves a full
        # extra deform evaluation per frame.
        worst = 0.0
        for i in targets:
            L = res[i].length
            if L > worst:
                worst = L
        if worst <= tol * 10.0:
            return out, res, used

        start_eval = self.evaluate_with(self.base)
        self.push(self.base)

        # A miss is judged against how far that vertex was asked to travel.
        stuck = []
        for i in targets:
            want = targets[i] - _get(start_eval, i)
            span = want.length
            if span < 1e-9:
                continue
            if res[i].length > max(tol * 10.0, 0.005 * span):
                stuck.append(i)
        if not stuck:
            return out, res, used

        hops = max(2, int(continuation))
        warm = {i: _get(self.base, i) for i in stuck}
        best = {i: out[i] for i in stuck}
        best_err = {i: res[i].length for i in stuck}
        best_res = {i: res[i] for i in stuck}
        total_used = used

        for hop in range(1, hops + 1):
            f = hop / float(hops)
            sub = {i: _get(start_eval, i) + (targets[i] - _get(start_eval, i)) * f
                   for i in stuck}
            hop_out, hop_res, hop_used = self._solve_pass(
                sub, jac=None, max_iter=max_iter, tol=tol, eps=eps,
                adaptive=adaptive, start=warm)
            total_used += hop_used
            warm = hop_out
            if hop == hops:
                for i in stuck:
                    if hop_res[i].length < best_err[i]:
                        best_err[i] = hop_res[i].length
                        best[i] = hop_out[i]
                        best_res[i] = hop_res[i]

        for i in stuck:
            out[i] = best[i]
            res[i] = best_res[i]
        return out, res, total_used

    # -- frames ------------------------------------------------------------
    def deform_frame(self, indices, eps=1e-3):
        """Orthonormal world-space frame describing how the selection is deformed."""
        jac, base_eval = self.jacobian(indices, eps)
        if not jac:
            return None, None
        acc = Matrix(((0, 0, 0), (0, 0, 0), (0, 0, 0)))
        for m in jac.values():
            for r in range(3):
                for c in range(3):
                    acc[r][c] += m[r][c]
        inv_n = 1.0 / len(jac)
        for r in range(3):
            for c in range(3):
                acc[r][c] *= inv_n
        world = self.matrix3 @ acc
        return orthonormalize(world), base_eval


def orthonormalize(m):
    """Gram-Schmidt on the columns, with graceful fallbacks for degenerate input."""
    x = Vector((m[0][0], m[1][0], m[2][0]))
    y = Vector((m[0][1], m[1][1], m[2][1]))
    z = Vector((m[0][2], m[1][2], m[2][2]))

    if x.length < 1e-9:
        x = y.cross(z)
    if x.length < 1e-9:
        x = Vector((1.0, 0.0, 0.0))
    x = x.normalized()

    y = y - x * y.dot(x)
    if y.length < 1e-9:
        alt = Vector((0.0, 0.0, 1.0)) if abs(x.z) < 0.9 else Vector((0.0, 1.0, 0.0))
        y = alt - x * alt.dot(x)
    y = y.normalized()

    z = x.cross(y)
    if z.length < 1e-9:
        z = Vector((0.0, 0.0, 1.0))
    z = z.normalized()
    y = z.cross(x).normalized()

    return Matrix(((x.x, y.x, z.x), (x.y, y.y, z.y), (x.z, y.z, z.z)))


def selected_indices(bm, limit=None):
    bm.verts.ensure_lookup_table()
    out = [v.index for v in bm.verts if v.select and not v.hide]
    if limit is not None and len(out) > limit:
        return out, True
    return out, False


def active_vert_index(bm):
    for elem in reversed(bm.select_history):
        if isinstance(elem, bmesh.types.BMVert):
            return elem.index
    return None


# ---------------------------------------------------------------------------
# Proportional editing
# ---------------------------------------------------------------------------
# These curves were measured against Blender 4.2 itself (move one vertex by a
# known delta, read back every vertex's displacement ratio) and reproduce its
# output to 0.000000 error. Distances are WORLD space, matching Blender: the
# proportional radius does not shrink when the object is scaled.

def _falloff_weight(t, kind):
    """t is distance/radius in 0..1. Returns Blender's influence weight.

    The radius is exclusive for every falloff including CONSTANT: a vertex
    sitting exactly at the radius gets zero weight. Verified against Blender
    4.2 with a vertex placed at exactly 1.5 units under a 1.5 radius.
    """
    if t >= 1.0:
        return 0.0
    if t <= 0.0:
        return 1.0
    if kind == 'CONSTANT':
        return 1.0
    if kind == 'LINEAR':
        return 1.0 - t
    if kind == 'SHARP':
        return (1.0 - t) ** 2
    if kind == 'ROOT':
        return math.sqrt(1.0 - t)
    if kind == 'SPHERE':
        return math.sqrt(1.0 - t * t)
    if kind == 'INVERSE_SQUARE':
        return 1.0 - t * t
    # SMOOTH is Blender's default
    return 1.0 - (3.0 * t * t - 2.0 * t * t * t)


def _geodesic_across_triangle(v0, v1, v2, d1, d2):
    """Distance to v0, given distances d1 at v1 and d2 at v2.

    Same estimate Blender's transform uses for Connected Only (a virtual
    source unfolded into the triangle's plane), so the falloff follows the
    surface the same way. Falls back to the shorter edge path.
    """
    v10 = v0 - v1
    v12 = v2 - v1
    if d1 != 0.0 and d2 != 0.0:
        d12 = v12.length
        if d12 * d12 > 0.0:
            u = v12 / d12
            n = v12.cross(v10)
            if n.length > 0.0:
                n.normalize()
                w = n.cross(u)
                x0 = v10.dot(u)
                y0 = abs(v10.dot(w))
                a = 0.5 * (1.0 + (d1 * d1 - d2 * d2) / (d12 * d12))
                hh = d1 * d1 - a * a * d12 * d12
                if hh > 0.0:
                    h = math.sqrt(hh)
                    sx, sy = a * d12, -h
                    x_int = sx + h * (x0 - sx) / (y0 + h)
                    if 0.0 <= x_int <= d12:
                        return math.hypot(x0 - sx, y0 - sy)
    return min(d1 + v10.length, d2 + (v0 - v2).length)


def connected_distances(bm, seeds, limit, coords=None):
    """Distance over the surface from the nearest seed - Blender's "Connected Only".

    Influence travels along the mesh, so a vertex that is close in space but
    far along the surface is not dragged. Distances are propagated along edges
    and ACROSS faces (not just along edges): walking edges only overestimates
    every diagonal and gives the far side of a card less pull than Blender does.

    ``coords`` maps vertex index to the position to measure on (world space);
    missing indices fall back to the vertex's own coordinate. Values beyond
    ``limit`` carry no weight, so propagation stops a little past it.
    """
    bm.verts.ensure_lookup_table()
    INF = float("inf")
    stop = limit * 2.0

    def co(v):
        if coords is not None:
            c = coords.get(v.index)
            if c is not None:
                return c
        return v.co

    dist = {}
    for i in seeds:
        dist[i] = 0.0
    seedset = set(seeds)

    def try_add(v0, v1, v2):
        """Relax v0 from v1 (edge) or from v1 and v2 (across a face)."""
        if v0.hide or v0.index in seedset:
            return False
        d0 = dist.get(v0.index, INF)
        d1 = dist.get(v1.index, INF)
        if d1 == INF or d0 <= d1:
            return False
        if v2 is not None:
            d2 = dist.get(v2.index, INF)
            if d2 == INF or d0 <= d2:
                return False
            nd = _geodesic_across_triangle(co(v0), co(v1), co(v2), d1, d2)
        else:
            nd = d1 + (co(v1) - co(v0)).length
        if nd < d0 and nd <= stop:
            dist[v0.index] = nd
            return True
        return False

    queue = []
    queued = set()
    for i in seeds:
        for e in bm.verts[i].link_edges:
            if not e.hide and e.index not in queued:
                queued.add(e.index)
                queue.append(e)

    while queue:
        nxt = []
        nxt_set = set()

        def push(v, skip):
            for e2 in v.link_edges:
                if e2 is not skip and not e2.hide and e2.index not in nxt_set:
                    nxt_set.add(e2.index)
                    nxt.append(e2)

        while queue:
            e = queue.pop()
            v1, v2 = e.verts
            d1 = dist.get(v1.index, INF)
            d2 = dist.get(v2.index, INF)
            if not e.link_loops or d1 == INF or d2 == INF:
                a_, b_ = (v1, v2) if d1 <= d2 else (v2, v1)
                if try_add(b_, a_, None):
                    push(b_, e)
            for l in e.link_loops:
                lo = l.link_loop_next.link_loop_next
                while lo is not l:
                    vo = lo.vert
                    if try_add(vo, v1, v2):
                        push(vo, e)
                    elif try_add(vo, v1, None) or try_add(vo, v2, None):
                        push(vo, e)
                    lo = lo.link_loop_next
        queue = nxt
    return {i: d for i, d in dist.items() if d <= limit}


def proportional_weights(context, ob, bm, selected, matrix_world, origin=None,
                         world=None):
    """Weight per vertex index for the current proportional-edit settings.

    Returns {} when proportional editing is off. Selected vertices always weigh
    1.0. Distances are measured in world space, like Blender's.

    ``world`` gives the VISIBLE (deformed) world position of every vertex. When
    supplied, distances are measured on the card the user is looking at, not on
    the undeformed cage. On a bent card the cage distances can be very
    different from the visible ones, and measuring there gives the wrong
    vertices the wrong share of the move - which shows up as a kink.

    ``origin`` supplies the ORIGINAL local coordinates ({index: Vector}) from
    before the drag started. Blender fixes proportional influence once at the
    start of a transform; recomputing it from vertices that the drag has already
    moved makes the region crawl across the mesh while the mouse moves, which
    looks like the falloff randomly changing shape. Always pass it during a
    modal transform.
    """
    ts = context.scene.tool_settings
    if not ts.use_proportional_edit:
        return {}
    size = float(ts.proportional_size)
    if size <= 0.0:
        return {i: 1.0 for i in selected}
    kind = ts.proportional_edit_falloff
    sel = set(selected)
    weights = {i: 1.0 for i in selected}

    bm.verts.ensure_lookup_table()

    if world is not None:
        if ts.use_proportional_connected:
            # Edge paths walked over the visible geometry, already in world
            # units, so the radius needs no scale correction.
            dist = connected_distances(bm, sel, size, world)
            for i, d in dist.items():
                if i in sel:
                    continue
                w = _falloff_weight(d / size, kind)
                if w > 0.0:
                    weights[i] = w
            return weights
        sel_world = [world[i] for i in selected if i in world]
        for v in bm.verts:
            if v.index in sel or v.hide or v.index not in world:
                continue
            pw = world[v.index]
            best = min((pw - s).length for s in sel_world)
            if best >= size:
                continue
            w = _falloff_weight(best / size, kind)
            if w > 0.0:
                weights[v.index] = w
        return weights

    def co(i):
        if origin is not None and i in origin:
            return origin[i]
        return bm.verts[i].co

    if ts.use_proportional_connected:
        cage = {v.index: matrix_world @ co(v.index) for v in bm.verts}
        dist = connected_distances(bm, sel, size, cage)
        for i, d in dist.items():
            if i in sel:
                continue
            w = _falloff_weight(d / size, kind)
            if w > 0.0:
                weights[i] = w
        return weights

    sel_world = [matrix_world @ co(i) for i in selected]
    for v in bm.verts:
        if v.index in sel or v.hide:
            continue
        pw = matrix_world @ co(v.index)
        best = min((pw - s).length for s in sel_world)
        if best >= size:
            continue
        w = _falloff_weight(best / size, kind)
        if w > 0.0:
            weights[v.index] = w
    return weights


def _avg_scale(m):
    return ((m.col[0].xyz.length + m.col[1].xyz.length + m.col[2].xyz.length)
            / 3.0)


# ---------------------------------------------------------------------------
# Snapping
# ---------------------------------------------------------------------------
# Snap targets are read off the EVALUATED (deformed) geometry of other objects,
# which is what the user sees and therefore what they expect to snap onto. The
# BVH tree is built once per drag: rebuilding it per mouse-move is far too slow
# on a dense scalp mesh.

class SnapContext:
    """Snap candidates gathered from every visible object except the edited one.

    ``hide_set`` is used to take the edited object out of the depsgraph for the
    duration of the drag, so a vertex never snaps onto the mesh it belongs to
    unless "Snap onto Itself" is enabled.
    """

    def __init__(self, context, edited, exclude_points=None):
        self.context = context
        self.edit_targets = []
        # World-space positions of the vertices being dragged. Blender never
        # snaps the moving selection onto itself; without this the anchor locks
        # onto the very vertex it is carrying and the drag appears frozen.
        self.exclude_points = list(exclude_points or ())
        self.exclude_r2 = 1e-6
        self.screen_ready = False
        self.screen_verts = []
        self.screen_edges = []
        self.screen_faces = []
        self.edge_segments = []
        # ``edited`` may be a single object or every object in a shared Edit
        # Mode session; all of them must be excluded from their own snapping.
        if edited is None:
            self.edited = set()
        elif hasattr(edited, "__iter__"):
            self.edited = set(edited)
        else:
            self.edited = {edited}
        self.trees = []      # (object, BVHTree, matrix_world)
        self.ok = False

    def build(self):
        """Collect snap targets.

        BVHTree.FromObject returns an EMPTY tree for an object that is in Edit
        Mode - verified: find_nearest gives None there while the same call works
        in Object Mode. Snapping onto the mesh being edited (the normal case
        when every hair card lives in one object) therefore has to go through
        scene.ray_cast, which does see the edit-mode result.
        """
        from mathutils.bvhtree import BVHTree
        ctx = self.context
        ts = ctx.scene.tool_settings
        dg = ctx.evaluated_depsgraph_get()
        # Blender's own defaults: snapping onto the edited mesh is ON.
        include_self = getattr(ts, "use_snap_self", True)
        include_edit = getattr(ts, "use_snap_edit", True)
        include_nonedit = getattr(ts, "use_snap_nonedit", True)

        self.edit_targets = []
        for ob in ctx.view_layer.objects:
            if ob.type != 'MESH':
                continue
            if not ob.visible_get():
                continue
            is_edited = ob in self.edited
            if is_edited:
                if not (include_self and include_edit):
                    continue
                # cannot be BVH'd while in edit mode - use the scene raycast
                self.edit_targets.append(ob)
                continue
            if not include_nonedit:
                continue
            try:
                tree = BVHTree.FromObject(ob, dg)
            except Exception:
                continue
            if tree is not None:
                self.trees.append((ob, tree, ob.matrix_world.copy()))
        self.ok = bool(self.trees) or bool(self.edit_targets)
        return self.ok

    def _raycast_edit(self, origin, direction):
        """Ray against objects in Edit Mode, via the scene (BVH cannot)."""
        if not self.edit_targets:
            return None, None, None
        try:
            dg = self.context.evaluated_depsgraph_get()
            ok, loc, nrm, idx, obj, mat = self.context.scene.ray_cast(
                dg, origin, direction)
        except Exception:
            return None, None, None
        if not ok or obj is None:
            return None, None, None
        # scene.ray_cast hands back the EVALUATED object, which is a distinct
        # datablock from the original - an identity test against the originals
        # always fails and silently discards every hit.
        orig = getattr(obj, "original", None) or obj
        for t in self.edit_targets:
            if t == orig or t.name == orig.name:
                return Vector(loc), t, idx
        return None, None, None

    # -- individual snap modes -------------------------------------------
    def _face_data(self, ob, index):
        """World-space corners of one evaluated face.

        Works for objects in Edit Mode too: evaluated_get().to_mesh() returns
        the deformed cage there, which is what is on screen.
        """
        dg = self.context.evaluated_depsgraph_get()
        evo = ob.evaluated_get(dg)
        me = evo.to_mesh()
        try:
            if index is None or index < 0 or index >= len(me.polygons):
                return [], None
            poly = me.polygons[index]
            mw = ob.matrix_world
            pts = [mw @ me.vertices[i].co.copy() for i in poly.vertices]
            centre = mw @ poly.center.copy()
        finally:
            evo.to_mesh_clear()
        return pts, centre

    def collect_screen(self, region, rv3d):
        """Cache every snap feature as a screen-space point.

        Blender's vertex/edge snapping is a SCREEN-SPACE search: it takes the
        feature nearest the cursor in pixels, whether or not the cursor is over
        a face. Relying on a ray hit fails whenever the surface is edge-on to
        the view - a flat hair card at a grazing angle is missed entirely, which
        is exactly the case that looked like "snapping does nothing".

        The view does not move during a modal transform, so this is built once.
        """
        from bpy_extras import view3d_utils

        self.screen_verts = []   # (Vector2, Vector3)
        self.screen_edges = []   # (Vector2, Vector3) midpoints
        self.screen_faces = []   # (Vector2, Vector3) centres
        self.edge_segments = []  # (world a, world b)

        dg = self.context.evaluated_depsgraph_get()
        objs = list(self.edit_targets) + [o for o, _t, _m in self.trees]
        for ob in objs:
            try:
                evo = ob.evaluated_get(dg)
                me = evo.to_mesh()
            except Exception:
                continue
            try:
                mw = ob.matrix_world
                wco = [mw @ v.co.copy() for v in me.vertices]
                for w in wco:
                    p2 = view3d_utils.location_3d_to_region_2d(region, rv3d, w)
                    if p2 is not None:
                        self.screen_verts.append((p2, w))
                for e in me.edges:
                    a = wco[e.vertices[0]]
                    b = wco[e.vertices[1]]
                    self.edge_segments.append((a, b))
                    mid = (a + b) * 0.5
                    p2 = view3d_utils.location_3d_to_region_2d(region, rv3d,
                                                               mid)
                    if p2 is not None:
                        self.screen_edges.append((p2, mid))
                for f in me.polygons:
                    c = mw @ f.center.copy()
                    p2 = view3d_utils.location_3d_to_region_2d(region, rv3d, c)
                    if p2 is not None:
                        self.screen_faces.append((p2, c))
            finally:
                try:
                    evo.to_mesh_clear()
                except Exception:
                    pass
        # Edge endpoints projected ONCE. The old code re-projected every edge
        # on every mouse move - 2220 edges x 2 projections per frame, which
        # measured 6.5ms of the 11.6ms frame on a 60-card scalp.
        self.edge_screen = []
        for a, b in self.edge_segments:
            pa = view3d_utils.location_3d_to_region_2d(region, rv3d, a)
            pb = view3d_utils.location_3d_to_region_2d(region, rv3d, b)
            if pa is None or pb is None:
                continue
            self.edge_screen.append((pa, pb, a, b))

        # Uniform grid over screen space so a search only visits nearby
        # candidates instead of the whole mesh.
        self._grid_cell = 64.0
        self._grid = {}

        def _bucket(store, p2, payload):
            key = (int(p2.x // self._grid_cell), int(p2.y // self._grid_cell))
            store.setdefault(key, []).append(payload)

        for p2, w in self.screen_verts:
            _bucket(self._grid, p2, ('V', p2, w))
        for p2, w in self.screen_edges:
            _bucket(self._grid, p2, ('M', p2, w))
        for p2, w in self.screen_faces:
            _bucket(self._grid, p2, ('F', p2, w))
        for pa, pb, a, b in self.edge_screen:
            mid = (pa + pb) * 0.5
            _bucket(self._grid, mid, ('E', (pa, pb, a, b), None))

        self.screen_ready = True
        return len(self.screen_verts)

    def _grid_near(self, mouse, radius_px):
        """Candidates whose screen cell is within radius of the cursor."""
        cell = getattr(self, "_grid_cell", 64.0)
        grid = getattr(self, "_grid", None)
        if not grid:
            return None
        span = int(radius_px // cell) + 1
        cx = int(mouse.x // cell)
        cy = int(mouse.y // cell)
        out = []
        for gx in range(cx - span, cx + span + 1):
            for gy in range(cy - span, cy + span + 1):
                b = grid.get((gx, gy))
                if b:
                    out.extend(b)
        return out

    def nearest_on_screen(self, region, rv3d, mouse, elements, max_px=None):
        """Feature nearest the cursor in pixels, as Blender's snapping works.

        Only candidates in nearby screen cells are examined, and edges use the
        projection cached at collect_screen time. Walking every vertex and
        re-projecting every edge each frame cost 11.6ms on a 60-card scalp,
        which is the lag that showed up in real use.
        """
        if not getattr(self, "screen_ready", False):
            self.collect_screen(region, rv3d)
        if max_px is None:
            max_px = _snap_pixel_radius(self.context)

        want_v = 'VERTEX' in elements
        want_m = 'EDGE_MIDPOINT' in elements
        want_f = bool(elements & {'FACE', 'FACE_NEAREST', 'FACE_PROJECT'})
        want_e = bool(elements & {'EDGE', 'EDGE_PERPENDICULAR'})

        best, best_px = None, 1e30

        # A generous search radius: the cursor may sit outside the snap
        # radius while an edge running past it is still within range.
        cands = self._grid_near(mouse, max_px * 2.0)
        if cands is None:
            cands = []
            for p2, w in self.screen_verts:
                cands.append(('V', p2, w))
            for p2, w in self.screen_edges:
                cands.append(('M', p2, w))
            for p2, w in self.screen_faces:
                cands.append(('F', p2, w))
            for pa, pb, a, b in getattr(self, "edge_screen", ()):
                cands.append(('E', (pa, pb, a, b), None))

        for kind, data, w in cands:
            if kind == 'E':
                if not want_e:
                    continue
                pa, pb, a, b = data
                ab = pb - pa
                L = ab.length_squared
                if L < 1e-12:
                    continue
                t = (mouse - pa).dot(ab) / L
                t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
                p2 = pa + ab * t
                d = (p2 - mouse).length
                if d >= best_px:
                    continue
                wpt = a + (b - a) * t
                if self._excluded(wpt):
                    continue
                best_px, best = d, wpt
                continue

            if kind == 'V' and not want_v:
                continue
            if kind == 'M' and not want_m:
                continue
            if kind == 'F' and not want_f:
                continue
            d = (data - mouse).length
            if d >= best_px:
                continue
            if self._excluded(w):
                continue
            best_px, best = d, w

        if best is not None and best_px <= max_px:
            return best, best_px
        return None, 1e30

    def under_mouse(self, region, rv3d, mouse, elements, max_px=None):
        """Snap to what the cursor is pointing at, the way Blender does.

        Primary path is a SCREEN-SPACE search (nearest_on_screen). A ray hit is
        only consulted for surface-type elements, where the point on the face
        itself matters. Ray-first was wrong: a flat hair card seen edge-on is
        missed by the ray entirely, so snapping appeared to do nothing on
        exactly the geometry this addon exists for.
        """
        from bpy_extras import view3d_utils

        if not self.trees and not self.edit_targets:
            return None, 1e30
        if max_px is None:
            max_px = _snap_pixel_radius(self.context)

        best, best_px = self.nearest_on_screen(region, rv3d, mouse, elements,
                                               max_px)

        # For face snapping the exact point on the surface beats the centre,
        # when the cursor really is over a face.
        if elements & {'FACE', 'FACE_NEAREST', 'FACE_PROJECT'}:
            origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, mouse)
            direction = view3d_utils.region_2d_to_vector_3d(region, rv3d,
                                                            mouse)
            loc, obj, idx = self._raycast_edit(origin, direction)
            if loc is None:
                hit_d = 1e30
                for ob, tree, mw in self.trees:
                    inv = mw.inverted()
                    l_org = inv @ origin
                    l_dir = (inv.to_3x3() @ direction).normalized()
                    try:
                        hloc, hnrm, hidx, hdist = tree.ray_cast(l_org, l_dir)
                    except Exception:
                        continue
                    if hloc is None:
                        continue
                    w = mw @ hloc
                    d = (w - origin).length
                    if d < hit_d:
                        hit_d, loc, obj, idx = d, w, ob, hidx
            if loc is not None and not self._excluded(loc):
                p2 = view3d_utils.location_3d_to_region_2d(region, rv3d, loc)
                if p2 is not None:
                    d_px = (p2 - mouse).length
                    if d_px <= max_px and d_px < best_px:
                        best, best_px = loc, d_px

        if best is None:
            return None, 1e30
        return best, best_px

    def _excluded(self, point):
        """True when this candidate is one of the vertices being dragged.

        Hashed to a coarse grid: a linear scan ran once per candidate per
        frame, which is O(candidates x selection) on every mouse move.
        """
        if not self.exclude_points:
            return False
        keys = getattr(self, "_excl_keys", None)
        if keys is None:
            q = 1e-3
            keys = set()
            for p in self.exclude_points:
                keys.add((round(p.x / q), round(p.y / q), round(p.z / q)))
            self._excl_keys = keys
            self._excl_q = q
        q = self._excl_q
        kx, ky, kz = (round(point.x / q), round(point.y / q),
                      round(point.z / q))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    if (kx + dx, ky + dy, kz + dz) in keys:
                        return True
        return False

    def _candidates(self, ob, idx, loc, elements):
        """Snap features of one face: the surface point, its verts, edges."""
        cands = []
        if 'FACE' in elements or 'FACE_NEAREST' in elements or \
                'FACE_PROJECT' in elements:
            cands.append(loc)
        if idx is None:
            return cands
        if not (elements & {'VERTEX', 'EDGE', 'EDGE_MIDPOINT',
                            'EDGE_PERPENDICULAR'}):
            return cands
        try:
            pts, centre = self._face_data(ob, idx)
        except Exception:
            return cands
        if not pts:
            return cands
        if 'VERTEX' in elements:
            cands.extend(pts)
        if 'EDGE' in elements or 'EDGE_PERPENDICULAR' in elements:
            for i in range(len(pts)):
                a = pts[i]
                b = pts[(i + 1) % len(pts)]
                ab = b - a
                L = ab.length_squared
                if L < 1e-18:
                    continue
                t = max(0.0, min(1.0, (loc - a).dot(ab) / L))
                cands.append(a + ab * t)
        if 'EDGE_MIDPOINT' in elements:
            for i in range(len(pts)):
                cands.append((pts[i] + pts[(i + 1) % len(pts)]) / 2.0)
        return cands

    def nearest(self, point, elements):
        """Best snap position for ``point``, or None when nothing is in range.

        ``elements`` is scene.tool_settings.snap_elements. Every requested mode
        is evaluated and the closest result wins, which is how Blender behaves
        when several snap targets are enabled at once.
        """
        best = None
        best_d = 1e30
        for ob, tree, mw in self.trees:
            # The tree lives in the object's local space; _face_data returns
            # world space. Mixing the two silently snaps to the wrong place.
            try:
                inv = mw.inverted()
            except Exception:
                continue
            loc, nrm, idx, dist = tree.find_nearest(inv @ point)
            if loc is None:
                continue
            loc = mw @ loc
            cands = []
            if 'FACE' in elements or 'FACE_NEAREST' in elements or \
                    'FACE_PROJECT' in elements:
                cands.append(loc)
            if idx is not None and (
                    'VERTEX' in elements or 'EDGE' in elements or
                    'EDGE_MIDPOINT' in elements):
                try:
                    pts, centre = self._face_data(ob, idx)
                except Exception:
                    pts, centre = [], None
                if 'VERTEX' in elements and pts:
                    cands.append(min(pts, key=lambda p: (p - point).length))
                if 'EDGE' in elements and pts:
                    for i in range(len(pts)):
                        a = pts[i]
                        b = pts[(i + 1) % len(pts)]
                        ab = b - a
                        L = ab.length_squared
                        if L < 1e-18:
                            continue
                        t = max(0.0, min(1.0, (point - a).dot(ab) / L))
                        cands.append(a + ab * t)
                if 'EDGE_MIDPOINT' in elements and pts:
                    for i in range(len(pts)):
                        cands.append((pts[i] + pts[(i + 1) % len(pts)]) / 2.0)
            for c in cands:
                d = (c - point).length
                if d < best_d:
                    best_d = d
                    best = c
        return best, best_d

    def free(self):
        self.trees = []


def increment_step(context):
    """Grid step for INCREMENT snapping, matching the viewport's grid."""
    space = None
    area = getattr(context, "area", None)
    if area is not None and area.type == 'VIEW_3D':
        space = area.spaces.active
    scale = 1.0
    if space is not None:
        scale = getattr(space.overlay, "grid_scale", 1.0) or 1.0
    return scale


def _snap_pixel_radius(context):
    """How near the cursor a feature must be, in pixels, to snap to it."""
    try:
        return float(context.preferences.view.ui_scale) * 35.0
    except Exception:
        return 35.0


def apply_snap(context, point, snap_ctx, start=None, region=None, rv3d=None,
               mouse=None):
    """Snap a world-space target according to the scene's snap settings.

    Returns the possibly-adjusted point. INCREMENT/GRID quantise the movement;
    the geometry modes pull onto the nearest evaluated surface feature.
    """
    ts = context.scene.tool_settings
    if not ts.use_snap:
        return point
    elements = set(ts.snap_elements)

    if elements & {'INCREMENT', 'GRID'}:
        step = increment_step(context)
        if step > 1e-9:
            if 'GRID' in elements or getattr(ts, "use_snap_grid_absolute", False):
                point = Vector((round(point.x / step) * step,
                                round(point.y / step) * step,
                                round(point.z / step) * step))
            elif start is not None:
                d = point - start
                point = start + Vector((round(d.x / step) * step,
                                        round(d.y / step) * step,
                                        round(d.z / step) * step))
        if not (elements - {'INCREMENT', 'GRID'}):
            return point

    if snap_ctx is None or not snap_ctx.ok:
        return point

    # Blender snaps to the feature under the cursor. Use the mouse ray whenever
    # the caller can supply it, and only fall back to nearest-in-3D otherwise.
    if region is not None and rv3d is not None and mouse is not None:
        hit, _px = snap_ctx.under_mouse(region, rv3d, mouse, elements)
        if hit is None:
            return point
        return hit

    hit, dist = snap_ctx.nearest(point, elements)
    if hit is None:
        return point
    return hit
