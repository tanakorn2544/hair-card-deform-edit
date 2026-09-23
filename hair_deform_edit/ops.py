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
}


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
        ob = context.edit_object
        if ob is None or ob.type != 'MESH':
            return False
        space = context.space_data
        if space is None or space.type != 'VIEW_3D':
            return False
        # Accept the object when a deform modifier exists at all. The edit-mode
        # display flags are switched on during invoke, because a freshly added
        # Curve modifier ships with them off - gating on them here is what made
        # the toggle look dead on a normal hair card.
        prefs = _prefs()
        include_ns = bool(prefs and prefs.include_nonseparable)
        if prefs is not None and not prefs.auto_enable_cage:
            if not solver.has_editmode_deform(ob):
                return False
        elif not solver.deformers_for_edit(ob, include_ns):
            return False

        # Proportional editing and snapping are both supported in deform space,
        # so the override stays active for them. Nothing else here should take
        # the key away from the user.

        try:
            return ob.data.total_vert_sel > 0
        except Exception:
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
        if getattr(self, "proxy", None) is not None:
            self.proxy.free()
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
        if not getattr(self, "orig", None):
            return
        for idx, co in self.orig.items():
            self.bm.verts[idx].co = co
        bmesh.update_edit_mesh(self.ob.data, loop_triangles=False, destructive=False)
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
        else:
            bits.append("x %.4f" % self.factor)

        ts_s = context.scene.tool_settings
        if ts_s.use_snap:
            el = ",".join(sorted(ts_s.snap_elements)).lower()
            if self.snapped is not None:
                bits.append("SNAP %s" % el)
            else:
                bits.append("snap %s (no target)" % el)

        if self.weights:
            ts = context.scene.tool_settings
            bits.append("proportional %s r=%.3f (%d verts)"
                        % (ts.proportional_edit_falloff.lower(),
                           ts.proportional_size, len(self.weights)))

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
        ob, bm, err = _edit_target(context)
        if err:
            self.report({'WARNING'}, err)
            return {'CANCELLED'}

        prefs = _prefs()
        include_ns = bool(prefs and prefs.include_nonseparable)
        auto_cage = bool(prefs.auto_enable_cage) if prefs else True

        if not solver.has_editmode_deform(ob):
            # A freshly added Curve modifier has edit-mode display switched off,
            # so the deformed shape is on screen but the cage is not. Switch the
            # display flags on rather than silently doing nothing, which is
            # indistinguishable from the addon being broken.
            fixed = solver.enable_cage_display(ob, include_ns) if auto_cage else []
            if fixed:
                context.view_layer.update()
                self.report({'INFO'},
                            "Enabled edit-mode display for: %s" % ", ".join(fixed))
            if not solver.has_editmode_deform(ob):
                # Genuinely nothing to invert - let Blender's transform run.
                return {'PASS_THROUGH'}

        self.max_iter = int(prefs.max_iterations) if prefs else 10
        self.tol = float(prefs.tolerance) if prefs else 1e-6
        eps = float(prefs.epsilon) if prefs else 1e-3
        self.eps = eps

        bm.verts.ensure_lookup_table()
        selected = [v.index for v in bm.verts if v.select and not v.hide]
        if not selected:
            return {'PASS_THROUGH'}

        # Proportional editing drags unselected neighbours too, each by a
        # weighted amount. Those vertices must be part of the solve, otherwise
        # the falloff region simply would not move.
        self.selected = selected

        # Blender fixes proportional influence once, from the geometry as it was
        # when the transform started. Snapshot every vertex up front so the
        # falloff cannot crawl across the mesh as the drag moves things.
        self.prop_origin = {v.index: v.co.copy() for v in bm.verts}
        self._draw_handle = None
        self.weights = solver.proportional_weights(
            context, ob, bm, selected, ob.matrix_world, self.prop_origin)
        if self.weights:
            indices = sorted(self.weights)
        else:
            indices = selected

        self.ob = ob
        self.bm = bm
        self.indices = indices
        self.orig = {i: bm.verts[i].co.copy() for i in indices}
        self.axis = None
        self.axis_local = False
        self.plane = False
        self.precision = False
        self.last_delta = Vector((0.0, 0.0, 0.0))
        self.angle = 0.0
        self.factor = 1.0
        self.last_residual = None
        self.folded = 0
        self.warm = None
        self.snapped = None

        # Which point of the selection is the one that snaps. Blender calls this
        # Snap With: Closest/Center/Median/Active.
        self.snap_anchor = None
        self.snap_base = context.scene.tool_settings.use_snap

        # One BVH build per drag: rebuilding per mouse-move is far too slow on
        # a dense scalp mesh.
        self.snap_ctx = None
        if context.scene.tool_settings.use_snap:
            try:
                sc = solver.SnapContext(context, ob)
                if sc.build():
                    self.snap_ctx = sc
            except Exception:
                self.snap_ctx = None
        if not hasattr(self, "weights"):
            self.weights = {}
        self.start_mouse = Vector((event.mouse_region_x, event.mouse_region_y))
        self.precision_anchor = None

        self.proxy = solver.DeformProxy(ob, bm, include_ns)
        try:
            self.proxy.build()
            self.jac, base_eval = self.proxy.jacobian(indices, eps)
        except Exception as exc:
            self._cleanup(context)
            self.report({'ERROR'}, "Could not evaluate the deform: %s" % exc)
            return {'CANCELLED'}

        mw = self.proxy.matrix_world
        self.start_deformed = {i: mw @ solver._get(base_eval, i) for i in indices}

        active = solver.active_vert_index(bm)
        self.pivot = _pivot_world(context, indices, self.start_deformed, active)

        # Snap With: which point of the selection is the one that lands on the
        # target. Blender offers Closest / Center / Median / Active.
        self.snap_anchor = _snap_anchor_world(
            context, self.selected, self.start_deformed, active, self.pivot)
        self.depth_point = self.pivot.copy()

        # Local axes come from the deformed frame, so "local X" means along the
        # card as drawn, not along the undeformed cage.
        self.local_frame = solver.orthonormalize(
            self.proxy.matrix3 @ self.jac[indices[0]])

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
        self.weights = solver.proportional_weights(
            context, self.ob, self.bm, self.selected, self.ob.matrix_world,
            self.prop_origin)
        indices = sorted(self.weights) if self.weights else list(self.selected)

        new = [i for i in indices if i not in self.orig]
        if new:
            for i in new:
                self.orig[i] = self.bm.verts[i].co.copy()
            # Newly included vertices need their undeformed start recorded too.
            jac, base_eval = self.proxy.jacobian(indices, self.eps)
            self.jac = jac
            mw = self.proxy.matrix_world
            self.start_deformed = {i: mw @ solver._get(base_eval, i)
                                   for i in indices}
        # Vertices that dropped out of range must go back where they started.
        for i in list(self.indices):
            if i not in indices and i in self.orig:
                self.bm.verts[i].co = self.orig[i]
        self.indices = indices
        self.warm = None

    def _weight(self, i):
        """Proportional-edit influence for this vertex (1.0 when it is off)."""
        if not self.weights:
            return 1.0
        return self.weights.get(i, 0.0)

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
                try:
                    hit = solver.apply_snap(context, anchor.copy(),
                                            self.snap_ctx, self.snap_anchor)
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
        mw_inv = self.proxy.matrix_world_inv
        targets = {i: mw_inv @ p for i, p in world_targets.items()}

        coords, residual, _ = self.proxy.solve_targets(
            targets, jac=self.jac, max_iter=self.max_iter, tol=self.tol,
            eps=self.eps, start=self.warm)

        for i, co in coords.items():
            self.bm.verts[i].co = co
        bmesh.update_edit_mesh(self.ob.data, loop_triangles=False, destructive=False)

        # Reuse this frame's answer as the next frame's starting point.
        self.warm = coords

        # Judge the residual against how far anything actually had to move.
        moved = 0.0
        for i in self.indices:
            d = (world_targets[i] - self.start_deformed[i]).length
            if d > moved:
                moved = d
        scale = max(moved, 1e-9)

        worst = 0.0
        folded = 0
        for r in residual.values():
            length = r.length
            if length > worst:
                worst = length
            if length > 0.01 * scale:
                folded += 1
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
                    sc = solver.SnapContext(context, self.ob)
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
