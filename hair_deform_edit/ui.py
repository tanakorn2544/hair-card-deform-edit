# SPDX-License-Identifier: GPL-2.0-or-later
"""Sidebar panel, settings, and the auto-align selection handler."""

import bpy
from bpy.props import BoolProperty, EnumProperty, PointerProperty

from . import keymaps, ops, solver

_last_selection_key = None


def _toggle_update(self, context):
    """Install or remove the G/R/S overrides when the toggle flips."""
    keymaps.sync(self.override_grs)


class HairDeformSettings(bpy.types.PropertyGroup):
    auto_align: BoolProperty(
        name="Auto Align Orientation",
        description=(
            "Keep the transform orientation locked to the deformed shape as the "
            "selection changes, so G/R/S always work in the space you see"),
        default=False,
    )
    frame_source: EnumProperty(
        name="Frame From",
        description="Which vertices define the orientation",
        items=(
            ('SELECTION', "Selection", "Average the deform over the whole selection"),
            ('ACTIVE', "Active Vertex", "Use the active vertex only - best for long curved cards"),
        ),
        default='SELECTION',
    )


class HairDeformPrefs(bpy.types.AddonPreferences):
    bl_idname = __package__

    override_grs: BoolProperty(
        name="Deform-Space G / R / S",
        description=(
            "Use deform space for the normal Move, Rotate and Scale keys while "
            "editing a mesh that has a live deform modifier. Meshes without one "
            "keep Blender's standard behaviour"),
        default=True,
        update=_toggle_update,
    )
    auto_enable_cage: BoolProperty(
        name="Auto-Enable Edit Cage",
        description=(
            "A newly added Curve modifier has edit-mode display switched off, so "
            "the cage you grab is the flat undeformed one. Switch those flags on "
            "automatically the first time you transform such an object"),
        default=True,
    )
    max_iterations: bpy.props.IntProperty(
        name="Solver Iterations",
        description="Newton refinement steps per drag update. Higher is more exact, slower",
        default=10, min=1, max=32,
    )
    tolerance: bpy.props.FloatProperty(
        name="Tolerance",
        description="Stop refining once the result is this close to the target",
        default=1e-6, min=1e-9, max=1e-2, precision=9,
    )
    epsilon: bpy.props.FloatProperty(
        name="Probe Epsilon",
        description="Finite-difference step used to measure the deform. Shrink for tiny meshes",
        default=1e-3, min=1e-6, max=1.0, precision=6,
    )
    include_nonseparable: BoolProperty(
        name="Include Smoothing Modifiers",
        description=(
            "Also account for Smooth / Corrective Smooth / Laplacian modifiers. "
            "These mix neighbouring vertices, so the solve becomes approximate"),
        default=False,
    )

    def draw(self, context):
        col = self.layout.column()
        col.prop(self, "override_grs")
        col.prop(self, "auto_enable_cage")
        col.separator()
        col.prop(self, "max_iterations")
        col.prop(self, "tolerance")
        col.prop(self, "epsilon")
        col.prop(self, "include_nonseparable")


class HAIRDEFORM_PT_panel(bpy.types.Panel):
    bl_label = "Hair Card Deform Edit"
    bl_idname = "HAIRDEFORM_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Hair Deform"

    @classmethod
    def poll(cls, context):
        ob = context.object
        return ob is not None and ob.type == 'MESH'

    def draw(self, context):
        layout = self.layout
        ob = context.object
        settings = context.scene.hair_deform_edit
        prefs = ops._prefs()

        deform_mods = solver.visible_deform_modifiers(ob) if ob else []
        all_deform = [m for m in ob.modifiers if m.type in solver.ALL_DEFORM] if ob else []

        # --- the main switch
        box = layout.box()
        if prefs is not None:
            row = box.row()
            row.scale_y = 1.5
            on = prefs.override_grs
            row.prop(prefs, "override_grs",
                     text="Deform Edit: ON" if on else "Deform Edit: OFF",
                     toggle=True,
                     icon='CHECKBOX_HLT' if on else 'CHECKBOX_DEHLT')
            if on:
                box.label(text="G / R / S work in deform space", icon='INFO')
            else:
                box.label(text="G / R / S are Blender's default", icon='INFO')

        # --- status
        box = layout.box()
        auto = bool(prefs.auto_enable_cage) if prefs else True
        if not all_deform:
            box.label(text="No deform modifier on this object", icon='INFO')
            box.label(text="G / R / S stay normal here")
        elif not deform_mods:
            if auto:
                box.label(text="Edit-mode display is off", icon='INFO')
                box.label(text="G / R / S will switch it on")
            else:
                box.label(text="Deform is off in Edit Mode", icon='ERROR')
            box.operator("hair_deform_edit.setup_cage", icon='EDITMODE_HLT')
        elif not solver.cage_ready(ob):
            box.label(text="On Cage is off - the dots you grab", icon='ERROR')
            box.label(text="are drawn on the flat cage")
            box.operator("hair_deform_edit.setup_cage", icon='EDITMODE_HLT')
        else:
            names = ", ".join(m.name for m in deform_mods)
            box.label(text="Live: %s" % names, icon='CHECKMARK')

        # Show the transform options that are in play.
        ts = context.scene.tool_settings
        if ts.use_proportional_edit:
            box.label(text="Proportional edit: %s r=%.3f"
                           % (ts.proportional_edit_falloff.title(),
                              ts.proportional_size), icon='PROP_ON')
        if ts.use_snap:
            box.label(text="Snap: %s (%s)"
                           % (", ".join(sorted(e.replace('_', ' ').title()
                                               for e in ts.snap_elements)),
                              getattr(ts, "snap_target", 'CLOSEST').title()),
                      icon='SNAP_ON')

        if context.mode != 'EDIT_MESH':
            layout.label(text="Enter Edit Mode to use the tools", icon='INFO')
            return

        col = layout.column(align=True)
        col.label(text="Run Manually")
        row = col.row(align=True)
        op = row.operator("hair_deform_edit.transform", text="Move", icon='VIEW_PAN')
        op.mode = 'TRANSLATE'
        op = row.operator("hair_deform_edit.transform", text="Rotate", icon='FILE_REFRESH')
        op.mode = 'ROTATE'
        op = row.operator("hair_deform_edit.transform", text="Scale", icon='FULLSCREEN_ENTER')
        op.mode = 'RESIZE'

        layout.separator()
        col = layout.column(align=True)
        col.label(text="Clean Up")
        col.operator("hair_deform_edit.smooth_card", icon='MOD_SMOOTH')

        layout.separator()
        col = layout.column(align=True)
        col.label(text="Transform Orientation")
        col.operator("hair_deform_edit.align_orientation", icon='ORIENTATION_LOCAL')
        row = col.row(align=True)
        row.prop(settings, "auto_align", toggle=True,
                 icon='AUTO' if settings.auto_align else 'DECORATE')
        row.operator("hair_deform_edit.clear_orientation", text="", icon='X')
        col.prop(settings, "frame_source", text="")

        layout.separator()
        layout.operator("hair_deform_edit.report", icon='DRIVER_ROTATIONAL_DIFFERENCE')


def _selection_key(context):
    ob = context.edit_object
    if ob is None or ob.type != 'MESH':
        return None
    me = ob.data
    try:
        total = me.total_vert_sel
    except Exception:
        return None
    if not total:
        return None
    import bmesh
    bm = bmesh.from_edit_mesh(me)
    active = solver.active_vert_index(bm)
    return (ob.name, total, active)


# The align work creates and deletes a proxy object, which itself emits
# depsgraph updates. Doing that inside depsgraph_update_post would re-enter the
# handler, so the handler only records that a refresh is due and a one-shot
# timer performs the work outside the update cycle.
_pending = False
_busy = False


def _run_pending():
    global _pending, _busy
    _pending = False
    context = bpy.context
    scene = context.scene
    settings = getattr(scene, "hair_deform_edit", None)
    if settings is None or not settings.auto_align:
        return None
    if context.mode != 'EDIT_MESH':
        return None
    ob = context.edit_object
    if ob is None or not solver.has_editmode_deform(ob):
        return None

    import bmesh
    _busy = True
    try:
        bm = bmesh.from_edit_mesh(ob.data)
        frame, _err = ops.compute_deform_frame(context, ob, bm)
        if frame is not None:
            ops.set_custom_orientation(context, frame)
    except Exception:
        pass
    finally:
        _busy = False
    return None


@bpy.app.handlers.persistent
def _depsgraph_handler(scene, depsgraph):
    global _last_selection_key, _pending
    if _busy or _pending:
        return
    context = bpy.context
    settings = getattr(scene, "hair_deform_edit", None)
    if settings is None or not settings.auto_align:
        return
    if context.mode != 'EDIT_MESH':
        return
    ob = context.edit_object
    if ob is None or not solver.has_editmode_deform(ob):
        return

    key = _selection_key(context)
    if key is None or key == _last_selection_key:
        return
    _last_selection_key = key
    _pending = True
    bpy.app.timers.register(_run_pending, first_interval=0.0)


CLASSES = (
    HairDeformSettings,
    HairDeformPrefs,
    HAIRDEFORM_PT_panel,
)


def register_handlers():
    if _depsgraph_handler not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_depsgraph_handler)


def unregister_handlers():
    global _last_selection_key
    _last_selection_key = None
    if _depsgraph_handler in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_depsgraph_handler)
