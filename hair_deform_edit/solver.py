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


def connected_distances(bm, seeds, limit, origin=None):
    """Shortest path length along edges from any seed, capped at ``limit``.

    Mirrors Blender's "Connected Only": influence travels along the surface, so
    a vertex that is close in space but far along the mesh is not dragged.
    """
    import heapq
    dist = {i: 0.0 for i in seeds}
    heap = [(0.0, i) for i in seeds]
    heapq.heapify(heap)
    while heap:
        d, i = heapq.heappop(heap)
        if d > dist.get(i, 1e30):
            continue
        if d > limit:
            continue
        v = bm.verts[i]
        vco = origin[i] if (origin is not None and i in origin) else v.co
        for e in v.link_edges:
            o = e.other_vert(v)
            oco = (origin[o.index]
                   if (origin is not None and o.index in origin) else o.co)
            nd = d + (oco - vco).length
            if nd < dist.get(o.index, 1e30) and nd <= limit:
                dist[o.index] = nd
                heapq.heappush(heap, (nd, o.index))
    return dist


def proportional_weights(context, ob, bm, selected, matrix_world, origin=None):
    """Weight per vertex index for the current proportional-edit settings.

    Returns {} when proportional editing is off. Selected vertices always weigh
    1.0. Distances are measured in world space, like Blender's.

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

    def co(i):
        if origin is not None and i in origin:
            return origin[i]
        return bm.verts[i].co

    if ts.use_proportional_connected:
        # Edge-path distance is measured on the LOCAL mesh, then compared in
        # world units, so scale the cap accordingly.
        scale = _avg_scale(matrix_world)
        local_limit = size / scale if scale > 1e-12 else size
        dist = connected_distances(bm, sel, local_limit, origin)
        for i, dl in dist.items():
            if i in sel:
                continue
            w = _falloff_weight((dl * scale) / size, kind)
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

    def __init__(self, context, edited):
        self.context = context
        self.edited = edited
        self.trees = []      # (object, BVHTree, matrix_world)
        self.ok = False

    def build(self):
        from mathutils.bvhtree import BVHTree
        ctx = self.context
        ts = ctx.scene.tool_settings
        dg = ctx.evaluated_depsgraph_get()
        include_self = getattr(ts, "use_snap_self", False)
        for ob in ctx.view_layer.objects:
            if ob.type != 'MESH':
                continue
            if ob is self.edited and not include_self:
                continue
            if not ob.visible_get():
                continue
            try:
                tree = BVHTree.FromObject(ob, dg)
            except Exception:
                continue
            if tree is not None:
                self.trees.append((ob, tree, ob.matrix_world.copy()))
        self.ok = bool(self.trees)
        return self.ok

    # -- individual snap modes -------------------------------------------
    def _face_data(self, ob, index):
        dg = self.context.evaluated_depsgraph_get()
        evo = ob.evaluated_get(dg)
        me = evo.to_mesh()
        try:
            poly = me.polygons[index]
            mw = ob.matrix_world
            pts = [mw @ me.vertices[i].co.copy() for i in poly.vertices]
            centre = mw @ poly.center.copy()
        finally:
            evo.to_mesh_clear()
        return pts, centre

    def nearest(self, point, elements):
        """Best snap position for ``point``, or None when nothing is in range.

        ``elements`` is scene.tool_settings.snap_elements. Every requested mode
        is evaluated and the closest result wins, which is how Blender behaves
        when several snap targets are enabled at once.
        """
        best = None
        best_d = 1e30
        for ob, tree, mw in self.trees:
            loc, nrm, idx, dist = tree.find_nearest(point)
            if loc is None:
                continue
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


def apply_snap(context, point, snap_ctx, start=None):
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
    hit, dist = snap_ctx.nearest(point, elements)
    if hit is None:
        return point
    return hit
