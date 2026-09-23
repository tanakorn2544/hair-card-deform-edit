# SPDX-License-Identifier: GPL-2.0-or-later
"""Operators: orientation alignment, WYSIWYG transform, and setup helpers."""

import math

import bmesh
import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty
from bpy_extras import view3d_utils
from mathutils import Matrix, Vector

from . import solver

ORIENTATION_NAME = "Deform"


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------

def _prefs():
    try:
        return bpy.context.preferences.addons[__package__].preferences
    except Exception:
        return None


def _settings(context):
    return context.scene.hair_deform_edit


def _edit_target(context):
    ob = context.edit_object
    if ob is None or ob.type != 'MESH':
        return None, None, "Not in mesh Edit Mode"
    bm = bmesh.from_edit_mesh(ob.data)
    return ob, bm, None


def _view3d_override():
    for win in bpy.context.window_manager.windows:
        for area in win.screen.areas:
            if area.type != 'VIEW_3D':
                continue
            for region in area.regions:
                if region.type == 'WINDOW':
                    return dict(window=win, screen=win.screen, area=area, region=region)
    return None


def set_custom_orientation(context, matrix, name=ORIENTATION_NAME):
    """Create or update a custom transform orientation and make it active.

    ``transform.create_orientation`` needs a View3D context and copies the
    *current* orientation, so we create it once and then write the matrix in
    directly.
    """
    slot = context.scene.transform_orientation_slots[0]
    override = _view3d_override()
    if override is None:
        return False, "No 3D Viewport available"

    try:
        with context.temp_override(**override):
            bpy.ops.transform.create_orientation(
                name=name, use_view=False, use=True, overwrite=True)
    except Exception as exc:  # pragma: no cover - defensive
        return False, "Could not create orientation: %s" % exc

    custom = slot.custom_orientation
    if custom is None:
        return False, "Orientation slot did not accept the custom orientation"
    try:
        custom.matrix = matrix
    except Exception as exc:  # pragma: no cover - defensive
        return False, "Could not assign matrix: %s" % exc
    slot.type = custom.name
    return True, custom.name


def compute_deform_frame(context, ob, bm, prefs=None):
    """Orthonormal world frame for the current selection, or (None, error)."""
    prefs = prefs or _prefs()
    include_ns = bool(prefs and prefs.include_nonseparable)
    eps = float(prefs.epsilon) if prefs else 1e-3

    if not solver.has_editmode_deform(ob):
        return None, "No visible deform modifier (enable Display in Edit Mode)"

    indices, _ = solver.selected_indices(bm)
    if not indices:
        return None, "Nothing selected"

    active = solver.active_vert_index(bm)
    settings = _settings(context)
    if settings.frame_source == 'ACTIVE' and active is not None:
        indices = [active]
    elif len(indices) > 64:
        step = max(1, len(indices) // 64)
        indices = indices[::step]

    proxy = solver.DeformProxy(ob, bm, include_ns)
    try:
        proxy.build()
        frame, _ = proxy.deform_frame(indices, eps)
    finally:
        proxy.free()

    if frame is None:
        return None, "Could not evaluate the deform"
    return frame, None


# ---------------------------------------------------------------------------
# setup helper
# ---------------------------------------------------------------------------

class HAIRDEFORM_OT_setup_cage(bpy.types.Operator):
    """Turn on Display in Edit Mode and On Cage for every deform modifier"""

    bl_idname = "hair_deform_edit.setup_cage"
    bl_label = "Show Deform On Cage"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        ob = context.object
        return ob is not None and ob.type == 'MESH' and bool(ob.modifiers)

    def execute(self, context):
        ob = context.object
        touched = 0
        for md in ob.modifiers:
            if md.type not in solver.ALL_DEFORM:
                continue
            if not (md.show_in_editmode and md.show_on_cage):
                md.show_in_editmode = True
                md.show_on_cage = True
                touched += 1
        if not touched:
            self.report({'INFO'}, "Deform modifiers already display on the cage")
        else:
            self.report({'INFO'}, "Enabled edit-cage display on %d modifier(s)" % touched)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# orientation
# ---------------------------------------------------------------------------

class HAIRDEFORM_OT_align_orientation(bpy.types.Operator):
    """Align the transform orientation to the deformed shape you can see"""

    bl_idname = "hair_deform_edit.align_orientation"
    bl_label = "Align Orientation To Deform"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.edit_object is not None and context.edit_object.type == 'MESH'

    def execute(self, context):
        ob, bm, err = _edit_target(context)
        if err:
            self.report({'WARNING'}, err)
            return {'CANCELLED'}

        frame, err = compute_deform_frame(context, ob, bm)
        if err:
            self.report({'WARNING'}, err)
            return {'CANCELLED'}

        ok, msg = set_custom_orientation(context, frame)
        if not ok:
            self.report({'WARNING'}, msg)
            return {'CANCELLED'}
        self.report({'INFO'}, "Transform orientation '%s' aligned to the deform" % msg)
        return {'FINISHED'}


class HAIRDEFORM_OT_clear_orientation(bpy.types.Operator):
    """Go back to the Global transform orientation"""

    bl_idname = "hair_deform_edit.clear_orientation"
    bl_label = "Reset Orientation"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        context.scene.hair_deform_edit.auto_align = False
        slot = context.scene.transform_orientation_slots[0]
        try:
            slot.type = 'GLOBAL'
        except Exception:
            pass
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# deform-space transform (move / rotate / scale)
# ---------------------------------------------------------------------------

_AXIS_LABEL = {0: "X", 1: "Y", 2: "Z"}

_MODE_LABEL = {
    'TRANSLATE': "Move",
    'ROTATE': "Rotate",
    'RESIZE': "Scale",
    'SHRINK_FATTEN': "Shrink/Fatten",
}


class _Island:
    """Everything the solve needs for ONE object in multi-object edit mode.

    Blender lets several meshes share an Edit Mode session and transforms the
    selection across all of them at once. Each object has its own modifier
    stack, its own matrix and its own deform inverse, so the state cannot be
    kept in flat attributes on the operator - it has to be per object.
    """

    __slots__ = ("ob", "bm", "selected", "indices", "weights", "prop_origin",
                 "orig", "proxy", "jac", "start_deformed", "warm")

    def __init__(self, ob, bm, selected):
        self.ob = ob
        self.bm = bm
        self.selected = selected
        self.indices = list(selected)
        self.weights = {}
        self.prop_origin = {}
        self.orig = {}
        self.proxy = None
        self.jac = None
        self.start_deformed = {}
        self.warm = None

    def weight(self, i):
        """Proportional influence for a vertex; 1.0 when the mode is off."""
        if not self.weights:
            return 1.0
        return self.weights.get(i, 0.0)

    def free(self):
        if self.proxy is not None:
            self.proxy.free()
            self.proxy = None


def _editable_objects(context):
    """Every mesh sharing this Edit Mode session, active one first."""
    obs = []
    try:
        obs = [o for o in context.objects_in_mode if o.type == 'MESH']
    except Exception:
        obs = []
    if not obs:
        ob = context.edit_object
        if ob is not None and ob.type == 'MESH':
            obs = [ob]
    active = context.edit_object
    if active in obs:
        obs.remove(active)
        obs.insert(0, active)
    return obs


def _snap_anchor_world(context, selected, deformed, active, pivot):
    """The selection point that snapping moves onto the target.

    CLOSEST is resolved per-frame against the actual candidate, so here it
    starts at the median and is refined during the drag.
    """
    ts = context.scene.tool_settings
    mode = getattr(ts, "snap_target", 'CLOSEST')
    pts = [deformed[i] for i in selected if i in deformed]
    if not pts:
        return pivot.copy()
    if mode == 'ACTIVE' and active is not None and active in deformed:
        return deformed[active].copy()
    if mode == 'CENTER':
        return pivot.copy()
    # MEDIAN and CLOSEST both start from the median of the selection.
    total = Vector((0.0, 0.0, 0.0))
    for p in pts:
        total = total + p
    return total / float(len(pts))


def _pivot_world(context, indices, start_deformed, active_index):
    """Pivot in world space, honouring the scene's pivot setting."""
    pivot_mode = context.scene.tool_settings.transform_pivot_point

    if pivot_mode == 'CURSOR':
        return context.scene.cursor.location.copy()

    if pivot_mode == 'ACTIVE_ELEMENT' and active_index in start_deformed:
        return start_deformed[active_index].copy()

    if pivot_mode == 'BOUNDING_BOX_CENTER':
        lo = Vector(start_deformed[indices[0]])
        hi = Vector(start_deformed[indices[0]])
        for i in indices:
            p = start_deformed[i]
            for ax in range(3):
                if p[ax] < lo[ax]:
                    lo[ax] = p[ax]
                if p[ax] > hi[ax]:
                    hi[ax] = p[ax]
        return (lo + hi) * 0.5

    # MEDIAN_POINT, and INDIVIDUAL_ORIGINS which has no meaning for loose verts
    acc = Vector((0.0, 0.0, 0.0))
    for i in indices:
        acc += start_deformed[i]
    return acc / len(indices)


def _visible_normals(ob, indices):
    """World-space normals of the DEFORMED result, per vertex index.

    Blender's Alt+S offsets the cage vertex along its normal by a fixed amount;
    the modifier then scales that offset, so on a card whose curve radius varies
    the same drag produces between 0.51x and 1.0x of the requested thickness.
    Offsetting along the visible normal instead keeps what is asked for and what
    appears the same thing.
    """
    out = {}
    try:
        dg = bpy.context.evaluated_depsgraph_get()
        evo = ob.evaluated_get(dg)
        me = evo.to_mesh()
    except Exception:
        return out
    try:
        nmat = ob.matrix_world.to_3x3().inverted().transposed()
        n_v = len(me.vertices)
        for i in indices:
            if 0 <= i < n_v:
                nrm = (nmat @ me.vertices[i].normal.copy())
                if nrm.length > 1e-12:
                    out[i] = nrm.normalized()
    finally:
        try:
            evo.to_mesh_clear()
        except Exception:
            pass
    return out


class HAIRDEFORM_OT_transform(bpy.types.Operator):
    """Move, rotate or scale so the deformed result follows the mouse.

    Solves the inverse of the deform stack, so what moves under the cursor is
    what you see, not the hidden undeformed cage.
    """

    bl_idname = "hair_deform_edit.transform"
    bl_label = "Deform-Space Transform"
    bl_options = {'REGISTER', 'UNDO', 'GRAB_CURSOR', 'BLOCKING'}

    mode: EnumProperty(
        name="Mode",
        items=(
            ('TRANSLATE', "Move", "Move in deform space"),
            ('ROTATE', "Rotate", "Rotate in deform space"),
            ('RESIZE', "Scale", "Scale in deform space"),
            ('SHRINK_FATTEN', "Shrink/Fatten",
             "Move along the visible surface normal, in deform space"),
        ),
        default='TRANSLATE',
        options={'SKIP_SAVE'},
    )

    @classmethod
    def poll(cls, context):
        """Returning False lets the key fall through to Blender's transform.

        That fall-through is what makes the always-on toggle safe: with no live
        deform on the object, G/R/S behave exactly as they always did.
        """
        space = context.space_data
        if space is None or space.type != 'VIEW_3D':
            return False
        obs = _editable_objects(context)
        if not obs:
            return False

        # Accept an object when a deform modifier exists at all. The edit-mode
        # display flags are switched on during invoke, because a freshly added
        # Curve modifier ships with them off - gating on them here is what made
        # the toggle look dead on a normal hair card.
        #
        # In a multi-object session it is enough that ONE object qualifies and
        # has a selection; the others simply do not contribute.
        prefs = _prefs()
        include_ns = bool(prefs and prefs.include_nonseparable)
        strict = prefs is not None and not prefs.auto_enable_cage
        for ob in obs:
            if strict:
                if not solver.has_editmode_deform(ob):
                    continue
            elif not solver.deformers_for_edit(ob, include_ns):
                continue
            try:
                if ob.data.total_vert_sel > 0:
                    return True
            except Exception:
                continue
        return False

    # -- state -------------------------------------------------------------
    def _draw_falloff(self, context):
        """The white circle showing the proportional radius.

        Blender always draws this during a proportional transform; without it
        there is no way to see how big the influence is, which makes the tool
        feel unpredictable. Drawn around the pivot in the deformed space the
        user is actually looking at.
        """
        import gpu
        from gpu_extras.batch import batch_for_shader

        ts = context.scene.tool_settings
        if not ts.use_proportional_edit or self.pivot is None:
            return
        rv3d = context.region_data
        if rv3d is None:
            return

        radius = ts.proportional_size
        # A screen-facing circle, so it reads the same from any viewing angle.
        vm = rv3d.view_matrix
        right = Vector((vm[0][0], vm[1][0], vm[2][0]))
        up = Vector((vm[0][1], vm[1][1], vm[2][1]))
        centre = self.pivot

        pts = []
        segs = 64
        for k in range(segs + 1):
            a = (k / segs) * 2.0 * math.pi
            pts.append(centre + (right * math.cos(a) + up * math.sin(a))
                       * radius)

        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        batch = batch_for_shader(shader, 'LINE_STRIP', {"pos": pts})
        gpu.state.blend_set('ALPHA')
        gpu.state.line_width_set(1.0)
        shader.bind()
        shader.uniform_float("color", (1.0, 1.0, 1.0, 0.55))
        batch.draw(shader)
        gpu.state.line_width_set(1.0)
        gpu.state.blend_set('NONE')

    def _cleanup(self, context):
        for isl in getattr(self, "islands", []):
            isl.free()
        self.proxy = None
        if context.area:
            context.area.header_text_set(None)
        if getattr(self, "_draw_handle", None) is not None:
            try:
                bpy.types.SpaceView3D.draw_handler_remove(
                    self._draw_handle, 'WINDOW')
            except Exception:
                pass
            self._draw_handle = None
        if getattr(self, "snap_ctx", None) is not None:
            self.snap_ctx.free()
            self.snap_ctx = None
        # Ctrl-toggling snap must not outlive the drag.
        if getattr(self, "snap_base", None) is not None:
            context.scene.tool_settings.use_snap = self.snap_base

    def _restore(self, context):
        for isl in getattr(self, "islands", []):
            if not isl.orig:
                continue
            for idx, co in isl.orig.items():
                isl.bm.verts[idx].co = co
            bmesh.update_edit_mesh(isl.ob.data, loop_triangles=False,
                                   destructive=False)
            isl.warm = None
        self.warm = None

    def _header(self, context):
        bits = ["Deform " + _MODE_LABEL[self.mode]]
        if self.axis is None:
            bits.append("view" if self.mode == 'ROTATE' else "free")
        else:
            space = "local" if self.axis_local else "global"
            kind = "plane" if (self.plane and self.mode != 'ROTATE') else "axis"
            bits.append("%s %s (%s)" % (kind, _AXIS_LABEL[self.axis], space))

        if self.mode == 'TRANSLATE':
            bits.append("D %.4f" % self.last_delta.length)
        elif self.mode == 'ROTATE':
            bits.append("%.2f deg" % math.degrees(self.angle))
        elif self.mode == 'SHRINK_FATTEN':
            bits.append("offset %.4f" % getattr(self, "offset", 0.0))
        else:
            bits.append("x %.4f" % self.factor)

        ts_s = context.scene.tool_settings
        if ts_s.use_snap:
            el = ",".join(sorted(ts_s.snap_elements)).lower()
            if self.snapped is not None:
                bits.append("SNAP %s" % el)
            else:
                bits.append("snap %s (no target)" % el)

        n_prop = sum(len(i.weights) for i in getattr(self, "islands", []))
        if n_prop:
            ts = context.scene.tool_settings
            bits.append("proportional %s r=%.3f (%d verts)"
                        % (ts.proportional_edit_falloff.lower(),
                           ts.proportional_size, n_prop))
        n_obj = len(getattr(self, "islands", []))
        if n_obj > 1:
            bits.append("%d objects" % n_obj)

        if self.last_residual is not None:
            bits.append("err %.2gmm" % (self.last_residual * 1000.0))
        if self.folded:
            bits.append("WARNING: deform folds over here - %d vert(s) cannot follow"
                        % self.folded)
        bits.append("[X/Y/Z axis, shift precision, LMB/Enter confirm, Esc cancel]")
        if context.area:
            context.area.header_text_set("  |  ".join(bits))

    # -- lifecycle ---------------------------------------------------------
    def invoke(self, context, event):
        prefs = _prefs()
        include_ns = bool(prefs and prefs.include_nonseparable)
        auto_cage = bool(prefs.auto_enable_cage) if prefs else True

        objects = _editable_objects(context)
        if not objects:
            return {'PASS_THROUGH'}

        fixed_all = []
        usable = []
        for ob in objects:
            if not solver.has_editmode_deform(ob):
                # A freshly added Curve modifier has edit-mode display switched
                # off, so the deformed shape is on screen but the cage is not.
                # Switch the flags on rather than silently doing nothing, which
                # is indistinguishable from the addon being broken.
                if auto_cage:
                    fixed = solver.enable_cage_display(ob, include_ns)
                    if fixed:
                        fixed_all.extend("%s/%s" % (ob.name, f) for f in fixed)
                if not solver.has_editmode_deform(ob):
                    continue
            usable.append(ob)
        if fixed_all:
            context.view_layer.update()
            self.report({'INFO'},
                        "Enabled edit-mode display for: %s" % ", ".join(fixed_all))
        if not usable:
            # Genuinely nothing to invert - let Blender's transform run.
            return {'PASS_THROUGH'}

        self.max_iter = int(prefs.max_iterations) if prefs else 10
        self.tol = float(prefs.tolerance) if prefs else 1e-6
        eps = float(prefs.epsilon) if prefs else 1e-3
        self.eps = eps

        self._draw_handle = None

        # One island per object. Proportional editing drags unselected
        # neighbours too, so each island solves for a superset of its own
        # selection.
        islands = []
        for ob in usable:
            bm = bmesh.from_edit_mesh(ob.data)
            bm.verts.ensure_lookup_table()
            sel = [v.index for v in bm.verts if v.select and not v.hide]
            if not sel:
                continue
            isl = _Island(ob, bm, sel)
            # Blender fixes proportional influence once, from the geometry as it
            # was when the transform started. Snapshot every vertex up front so
            # the falloff cannot crawl across the mesh as the drag moves things.
            isl.prop_origin = {v.index: v.co.copy() for v in bm.verts}
            isl.weights = solver.proportional_weights(
                context, ob, bm, sel, ob.matrix_world, isl.prop_origin)
            isl.indices = sorted(isl.weights) if isl.weights else list(sel)
            isl.orig = {i: bm.verts[i].co.copy() for i in isl.indices}
            islands.append(isl)

        if not islands:
            return {'PASS_THROUGH'}
        self.islands = islands

        # The active object stays the reference for the local axis frame and
        # for anything that still needs a single object.
        primary = islands[0]
        self.ob = primary.ob
        self.bm = primary.bm
        self.selected = primary.selected
        self.axis = None
        self.axis_local = False
        self.plane = False
        self.precision = False
        self.last_delta = Vector((0.0, 0.0, 0.0))
        self.angle = 0.0
        self.factor = 1.0
        self.offset = 0.0
        self.last_residual = None
        self.folded = 0
        self.warm = None
        self.snapped = None

        # Which point of the selection is the one that snaps. Blender calls this
        # Snap With: Closest/Center/Median/Active.
        self.snap_anchor = None
        self.snap_base = context.scene.tool_settings.use_snap

        # Built after start_deformed exists, further down.
        self.snap_ctx = None
        # Mirror the active object's weights under the original name.
        self.weights = primary.weights
        self.start_mouse = Vector((event.mouse_region_x, event.mouse_region_y))
        self.precision_anchor = None

        try:
            for isl in self.islands:
                isl.proxy = solver.DeformProxy(isl.ob, isl.bm, include_ns)
                isl.proxy.build()
                isl.jac, base_eval = isl.proxy.jacobian(isl.indices, eps)
                mw = isl.proxy.matrix_world
                isl.start_deformed = {i: mw @ solver._get(base_eval, i)
                                      for i in isl.indices}
        except Exception as exc:
            self._cleanup(context)
            self.report({'ERROR'}, "Could not evaluate the deform: %s" % exc)
            return {'CANCELLED'}

        self.proxy = primary.proxy
        self.jac = primary.jac

        # Pivot and selection statistics span every object, exactly as Blender's
        # own transform does in a multi-object session.
        all_deformed = {}
        all_indices = []
        for n, isl in enumerate(self.islands):
            for i in isl.indices:
                key = (n, i)
                all_deformed[key] = isl.start_deformed[i]
                all_indices.append(key)
        self.start_deformed = all_deformed
        self.indices = all_indices

        # Visible normals for Shrink/Fatten, keyed like the other maps.
        self.deformed_normals = {}
        for n, isl in enumerate(self.islands):
            local = [i for (m, i) in all_indices if m == n]
            for i, nrm in _visible_normals(isl.ob, local).items():
                self.deformed_normals[(n, i)] = nrm

        # One BVH build per drag: rebuilding per mouse-move is far too slow on
        # a dense scalp mesh. The dragged vertices are excluded so the anchor
        # cannot snap onto the geometry it is carrying.
        if context.scene.tool_settings.use_snap:
            try:
                excl = [all_deformed[k] for k in all_indices]
                sc = solver.SnapContext(
                    context, [i.ob for i in self.islands], exclude_points=excl)
                if sc.build():
                    self.snap_ctx = sc
            except Exception:
                self.snap_ctx = None

        active = solver.active_vert_index(primary.bm)
        active_key = (0, active) if active is not None else None
        self.pivot = _pivot_world(context, all_indices, all_deformed, active_key)

        # Snap With: which point of the selection is the one that lands on the
        # target. Blender offers Closest / Center / Median / Active.
        sel_keys = [(n, i) for n, isl in enumerate(self.islands)
                    for i in isl.selected]
        self.snap_anchor = _snap_anchor_world(
            context, sel_keys, all_deformed, active_key, self.pivot)
        self.depth_point = self.pivot.copy()

        # Local axes come from the deformed frame, so "local X" means along the
        # card as drawn, not along the undeformed cage.
        self.local_frame = solver.orthonormalize(
            primary.proxy.matrix3 @ primary.jac[primary.indices[0]])

        region = context.region
        rv3d = context.region_data
        self.region = region
        self.rv3d = rv3d
        self.start_world = view3d_utils.region_2d_to_location_3d(
            region, rv3d, self.start_mouse, self.depth_point)

        pivot_2d = view3d_utils.location_3d_to_region_2d(region, rv3d, self.pivot)
        if pivot_2d is None:
            pivot_2d = Vector((region.width * 0.5, region.height * 0.5))
        self.pivot_2d = pivot_2d

        # Screen-space reference for rotate/scale.
        ref = self.start_mouse - self.pivot_2d
        if ref.length < 4.0:
            ref = Vector((1.0, 0.0))
        self.ref_vec = ref
        self.ref_len = max(ref.length, 1e-6)

        # World units per screen pixel at the selection's depth. Shrink/Fatten
        # needs this so a drag means the same thickness whatever the zoom.
        try:
            p_a = view3d_utils.region_2d_to_location_3d(
                region, rv3d, self.pivot_2d, self.pivot)
            p_b = view3d_utils.region_2d_to_location_3d(
                region, rv3d, self.pivot_2d + Vector((100.0, 0.0)),
                self.pivot)
            self.world_per_px = max((p_b - p_a).length / 100.0, 1e-9)
        except Exception:
            self.world_per_px = 0.01
        self.prev_angle_raw = 0.0
        self.accum_angle = 0.0

        # Rotation axis points at the viewer, so dragging counter-clockwise on
        # screen turns the selection counter-clockwise.
        self.view_axis = (rv3d.view_rotation @ Vector((0.0, 0.0, 1.0))).normalized()

        self._header(context)
        context.window_manager.modal_handler_add(self)
        # Show the falloff circle for the duration of the drag.
        if context.scene.tool_settings.use_proportional_edit:
            try:
                self._draw_handle = bpy.types.SpaceView3D.draw_handler_add(
                    self._draw_falloff, (context,), 'WINDOW', 'POST_VIEW')
            except Exception:
                self._draw_handle = None

        return {'RUNNING_MODAL'}

    # -- mouse mapping -----------------------------------------------------
    def _raw_delta(self, mouse):
        world = view3d_utils.region_2d_to_location_3d(
            self.region, self.rv3d, mouse, self.depth_point)
        return world - self.start_world

    def _mouse_delta(self, context, event):
        mouse = Vector((event.mouse_region_x, event.mouse_region_y))
        if self.precision:
            if self.precision_anchor is None:
                self.precision_anchor = (mouse.copy(), self._raw_delta(mouse))
            anchor_mouse, anchor_delta = self.precision_anchor
            fine = self._raw_delta(anchor_mouse + (mouse - anchor_mouse) * 0.1)
            return anchor_delta + (fine - self._raw_delta(anchor_mouse))
        self.precision_anchor = None
        return self._raw_delta(mouse)

    def _mouse_angle(self, event):
        cur = Vector((event.mouse_region_x, event.mouse_region_y)) - self.pivot_2d
        if cur.length < 1e-6:
            return self.accum_angle
        raw = math.atan2(cur.y, cur.x) - math.atan2(self.ref_vec.y, self.ref_vec.x)
        # Unwrap, so passing 180 deg keeps accumulating instead of flipping sign.
        delta = raw - self.prev_angle_raw
        while delta > math.pi:
            delta -= 2.0 * math.pi
        while delta < -math.pi:
            delta += 2.0 * math.pi
        self.prev_angle_raw = raw
        self.accum_angle += delta * (0.1 if self.precision else 1.0)
        return self.accum_angle

    def _mouse_factor(self, event):
        cur = Vector((event.mouse_region_x, event.mouse_region_y)) - self.pivot_2d
        f = cur.length / self.ref_len
        if self.precision:
            f = 1.0 + (f - 1.0) * 0.1
        return f

    def _mouse_offset(self, event):
        """Shrink/Fatten offset in Blender units, from mouse distance.

        Mirrors Blender: moving away from the selection fattens, moving toward
        it shrinks, and the scale is tied to how big the selection looks on
        screen so it feels the same whether zoomed in or out.
        """
        cur = Vector((event.mouse_region_x, event.mouse_region_y))
        start = self.start_mouse
        ref = max(getattr(self, "ref_len", 100.0), 1.0)
        # signed by whether the cursor moved away from the pivot or toward it
        d_now = (cur - self.pivot_2d).length
        d_start = (start - self.pivot_2d).length
        px = d_now - d_start
        scale = getattr(self, "offset_scale", None)
        if scale is None:
            # world units per pixel, from the selection's on-screen size
            scale = getattr(self, "world_per_px", 0.01)
        off = px * scale
        if self.precision:
            off *= 0.1
        return off

    def _axis_vector(self):
        if self.axis is None:
            return None
        if self.axis_local:
            return Vector((self.local_frame[0][self.axis],
                           self.local_frame[1][self.axis],
                           self.local_frame[2][self.axis])).normalized()
        a = Vector((0.0, 0.0, 0.0))
        a[self.axis] = 1.0
        return a

    def _constrain(self, delta):
        a = self._axis_vector()
        if a is None:
            return delta
        if self.plane:
            return delta - a * delta.dot(a)
        return a * delta.dot(a)

    # -- application -------------------------------------------------------
    def _rebuild_weights(self, context):
        """Recompute the falloff after the radius changed mid-drag.

        A larger radius pulls in vertices that were not part of the solve, so
        the whole working set is rebuilt - including their original positions,
        which is what a cancel restores to.
        """
        all_indices = []
        all_deformed = {}
        for n, isl in enumerate(self.islands):
            isl.weights = solver.proportional_weights(
                context, isl.ob, isl.bm, isl.selected, isl.ob.matrix_world,
                isl.prop_origin)
            indices = sorted(isl.weights) if isl.weights else list(isl.selected)

            fresh = [i for i in indices if i not in isl.orig]
            if fresh:
                for i in fresh:
                    isl.orig[i] = isl.bm.verts[i].co.copy()
                # Newly included vertices need their undeformed start too.
                jac, base_eval = isl.proxy.jacobian(indices, self.eps)
                isl.jac = jac
                mw = isl.proxy.matrix_world
                isl.start_deformed = {i: mw @ solver._get(base_eval, i)
                                      for i in indices}
            # Vertices that dropped out of range go back where they started.
            for i in list(isl.indices):
                if i not in indices and i in isl.orig:
                    isl.bm.verts[i].co = isl.orig[i]
            isl.indices = indices
            isl.warm = None
            for i in indices:
                all_indices.append((n, i))
                all_deformed[(n, i)] = isl.start_deformed[i]

        self.indices = all_indices
        self.start_deformed = all_deformed

        # Widening the proportional radius pulls in vertices that had no
        # normal captured yet; without this Shrink/Fatten would leave them
        # behind while everything else moved.
        self.deformed_normals = {}
        for n, isl in enumerate(self.islands):
            local = [i for (m, i) in all_indices if m == n]
            for i, nrm in _visible_normals(isl.ob, local).items():
                self.deformed_normals[(n, i)] = nrm
        self.jac = self.islands[0].jac
        self.weights = self.islands[0].weights
        self.warm = None

    # -- primary-island views -------------------------------------------
    # Multi-object edit keeps state per object. These expose the active
    # object's slice under the original names so single-object callers and
    # diagnostics keep working unchanged.
    @property
    def orig(self):
        isl = getattr(self, "islands", None)
        return isl[0].orig if isl else {}

    @property
    def prop_origin(self):
        isl = getattr(self, "islands", None)
        return isl[0].prop_origin if isl else {}

    @property
    def vert_indices(self):
        """Plain vertex indices for the active object."""
        isl = getattr(self, "islands", None)
        return list(isl[0].indices) if isl else []

    def _weight(self, key):
        """Proportional-edit influence for a (island, vertex) key."""
        n, i = key
        return self.islands[n].weight(i)

    def _targets_for(self, context, event):
        """World-space target position per affected vertex.

        With proportional editing on, every vertex in range moves by its own
        weighted share - a partial translation, a partial rotation about the
        pivot, or a partial scale - exactly as Blender's own transform does.
        """
        if self.mode == 'TRANSLATE':
            delta = self._constrain(self._mouse_delta(context, event))

            # Snapping acts on the point the user is dragging - the median of
            # the selection - and the whole selection rides along by the same
            # correction. Snapping each vertex independently would tear the
            # selection apart onto different features.
            ts = context.scene.tool_settings
            self.snapped = None
            if ts.use_snap:
                anchor = self.snap_anchor + delta
                mouse = Vector((event.mouse_region_x, event.mouse_region_y))
                try:
                    hit = solver.apply_snap(context, anchor.copy(),
                                            self.snap_ctx, self.snap_anchor,
                                            region=self.region, rv3d=self.rv3d,
                                            mouse=mouse)
                except Exception:
                    hit = None
                if hit is not None and (hit - anchor).length > 1e-9:
                    delta = delta + (hit - anchor)
                    self.snapped = hit.copy()

            self.last_delta = delta
            return {i: self.start_deformed[i] + delta * self._weight(i)
                    for i in self.indices}

        if self.mode == 'ROTATE':
            self.angle = self._mouse_angle(event)
            axis = self._axis_vector()
            if axis is None:
                axis = self.view_axis
            pivot = self.pivot
            out = {}
            for i in self.indices:
                w = self._weight(i)
                if w <= 0.0:
                    out[i] = self.start_deformed[i]
                    continue
                rot = Matrix.Rotation(self.angle * w, 3, axis)
                out[i] = pivot + rot @ (self.start_deformed[i] - pivot)
            return out

        if self.mode == 'SHRINK_FATTEN':
            # Blender's own Alt+S moves the CAGE vertex along its normal by a
            # fixed amount. When curve point radius varies - any tapered hair
            # card - the modifier scales that offset differently along the
            # card, so an identical drag yields between 0.51x and 1.0x of the
            # requested thickness on screen. Here the offset is applied to the
            # VISIBLE position along the VISIBLE normal, so what is asked for
            # is what appears.
            self.offset = self._mouse_offset(event)
            out = {}
            for i in self.indices:
                w = self._weight(i)
                if w <= 0.0:
                    out[i] = self.start_deformed[i]
                    continue
                n = self.deformed_normals.get(i)
                if n is None:
                    out[i] = self.start_deformed[i]
                    continue
                out[i] = self.start_deformed[i] + n * (self.offset * w)
            return out

        # RESIZE
        self.factor = self._mouse_factor(event)
        pivot = self.pivot
        a = self._axis_vector()
        out = {}
        for i in self.indices:
            off = self.start_deformed[i] - pivot
            w = self._weight(i)
            fac = 1.0 + (self.factor - 1.0) * w
            if a is None:
                off = off * fac
            else:
                along = a * off.dot(a)
                perp = off - along
                if self.plane:
                    off = along + perp * fac
                else:
                    off = perp + along * fac
            out[i] = pivot + off
        return out

    def _apply(self, context, world_targets):
        """Solve each object against its own modifier stack.

        Objects in a shared Edit Mode session have independent deform stacks and
        matrices, so the inverse has to run per object rather than once for the
        active one.
        """
        worst = 0.0
        folded = 0
        moved = 0.0
        for key, p in world_targets.items():
            d = (p - self.start_deformed[key]).length
            if d > moved:
                moved = d
        scale = max(moved, 1e-9)

        for n, isl in enumerate(self.islands):
            sub = {i: world_targets[(n, i)] for i in isl.indices
                   if (n, i) in world_targets}
            if not sub:
                continue
            mw_inv = isl.proxy.matrix_world_inv
            targets = {i: mw_inv @ p for i, p in sub.items()}

            coords, residual, _ = isl.proxy.solve_targets(
                targets, jac=isl.jac, max_iter=self.max_iter, tol=self.tol,
                eps=self.eps, start=isl.warm)

            for i, co in coords.items():
                isl.bm.verts[i].co = co
            bmesh.update_edit_mesh(isl.ob.data, loop_triangles=False,
                                   destructive=False)

            # Reuse this frame's answer as the next frame's starting point.
            isl.warm = coords

            for r in residual.values():
                length = r.length
                if length > worst:
                    worst = length
                if length > 0.01 * scale:
                    folded += 1

        self.warm = self.islands[0].warm if self.islands else None
        self.folded = folded
        self.last_residual = worst

    def modal(self, context, event):
        if event.type == 'MOUSEMOVE':
            self.precision = event.shift
            try:
                self._apply(context, self._targets_for(context, event))
            except Exception as exc:
                self._restore(context)
                self._cleanup(context)
                self.report({'ERROR'}, "Solve failed: %s" % exc)
                return {'CANCELLED'}
            self._header(context)
            return {'RUNNING_MODAL'}

        # Ctrl toggles snapping mid-drag, matching Blender's own transform.
        if event.type in {'LEFT_CTRL', 'RIGHT_CTRL'} and event.value in {
                'PRESS', 'RELEASE'}:
            ts = context.scene.tool_settings
            ts.use_snap = (event.value == 'PRESS') != self.snap_base
            if ts.use_snap and self.snap_ctx is None:
                try:
                    excl = [self.start_deformed[k] for k in self.indices]
                    sc = solver.SnapContext(
                        context, [i.ob for i in self.islands],
                        exclude_points=excl)
                    if sc.build():
                        self.snap_ctx = sc
                except Exception:
                    self.snap_ctx = None
            try:
                self._apply(context, self._targets_for(context, event))
            except Exception:
                pass
            self._header(context)
            return {'RUNNING_MODAL'}

        # Resize the falloff mid-drag. Blender's Transform Modal Map binds
        # WHEELDOWNMOUSE and PAGE_UP to SIZE_UP, WHEELUPMOUSE and PAGE_DOWN to
        # SIZE_DOWN - the wheel is deliberately inverted, and this operator was
        # doing it backwards before.
        if self.weights is not None and event.value == 'PRESS' and \
                event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE',
                               'PAGE_UP', 'PAGE_DOWN'}:
            ts = context.scene.tool_settings
            if ts.use_proportional_edit:
                bigger = event.type in {'WHEELDOWNMOUSE', 'PAGE_UP'}
                step = (1.0 / 0.9) if bigger else 0.9
                ts.proportional_size = max(1e-4, ts.proportional_size * step)
                self._rebuild_weights(context)
                try:
                    self._apply(context, self._targets_for(context, event))
                except Exception:
                    pass
                self._header(context)
                return {'RUNNING_MODAL'}

        if event.value == 'PRESS' and event.type in {'X', 'Y', 'Z'}:
            axis = {'X': 0, 'Y': 1, 'Z': 2}[event.type]
            plane = event.shift and self.mode != 'ROTATE'
            if self.axis == axis and self.plane == plane:
                if not self.axis_local:
                    self.axis_local = True
                else:
                    self.axis = None
                    self.axis_local = False
            else:
                self.axis = axis
                self.axis_local = False
            self.plane = plane
            try:
                self._apply(context, self._targets_for(context, event))
            except Exception:
                pass
            self._header(context)
            return {'RUNNING_MODAL'}

        if event.type in {'LEFTMOUSE', 'RET', 'NUMPAD_ENTER'} and event.value == 'PRESS':
            self._cleanup(context)
            return {'FINISHED'}

        if event.type in {'RIGHTMOUSE', 'ESC'} and event.value == 'PRESS':
            self._restore(context)
            self._cleanup(context)
            return {'CANCELLED'}

        return {'RUNNING_MODAL'}


class HAIRDEFORM_OT_deform_grab(bpy.types.Operator):
    """Move vertices so the deformed result follows the mouse"""

    bl_idname = "hair_deform_edit.deform_grab"
    bl_label = "Deform-Space Grab"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        ob = context.edit_object
        return (ob is not None and ob.type == 'MESH'
                and context.space_data is not None
                and context.space_data.type == 'VIEW_3D')

    def invoke(self, context, event):
        return bpy.ops.hair_deform_edit.transform('INVOKE_DEFAULT', mode='TRANSLATE')


# ---------------------------------------------------------------------------
# diagnostics
# ---------------------------------------------------------------------------

class HAIRDEFORM_OT_report(bpy.types.Operator):
    """Report how far the edit cage is rotated away from the visible result"""

    bl_idname = "hair_deform_edit.report"
    bl_label = "Check Deform Skew"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return context.edit_object is not None and context.edit_object.type == 'MESH'

    def execute(self, context):
        ob, bm, err = _edit_target(context)
        if err:
            self.report({'WARNING'}, err)
            return {'CANCELLED'}
        frame, err = compute_deform_frame(context, ob, bm)
        if err:
            self.report({'WARNING'}, err)
            return {'CANCELLED'}
        ang = frame.to_quaternion().angle
        self.report({'INFO'},
                    "Visible result is rotated %.1f deg from the edit cage axes"
                    % (ang * 57.2957795))
        return {'FINISHED'}


CLASSES = (
    HAIRDEFORM_OT_setup_cage,
    HAIRDEFORM_OT_align_orientation,
    HAIRDEFORM_OT_clear_orientation,
    HAIRDEFORM_OT_transform,
    HAIRDEFORM_OT_deform_grab,
    HAIRDEFORM_OT_report,
)
