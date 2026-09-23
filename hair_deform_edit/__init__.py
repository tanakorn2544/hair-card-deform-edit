# SPDX-License-Identifier: GPL-2.0-or-later
"""Hair Card Deform Edit - edit meshes in the space you actually see.

When a hair card is bent by a Curve (or Lattice, Armature, Simple Deform...)
modifier with edit-mode display on, the cage you grab still lives in the
undeformed space. Dragging "up" pushes the vertex along the flat card's axes,
not along the bent shape on screen.

This addon inverts the deform stack so the visible result follows the mouse.
"""

bl_info = {
    "name": "Hair Card Deform Edit",
    "author": "KornSensei",
    "version": (1, 6, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > Hair Deform / Edit Mode > G,R,S or Shift+Alt+G,R,S",
    "description": "Move, rotate and scale deformed hair cards in the space you see",
    "category": "Mesh",
}

import bpy

from . import keymaps, ops, solver, ui

_extra_keymaps = []


# Shift+Alt+<key> shortcuts. These never collide with anything Blender ships,
# so they work regardless of the G/R/S toggle - if the toggle is off, or if
# something else in the user's config eats the plain keys, these still run.
_EXTRA = (
    ('G', "hair_deform_edit.transform", 'TRANSLATE'),
    ('R', "hair_deform_edit.transform", 'ROTATE'),
    ('S', "hair_deform_edit.transform", 'RESIZE'),
    ('A', "hair_deform_edit.align_orientation", None),
)


def _register_extra_keymaps():
    """Explicit shortcuts that never collide with Blender's own."""
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if kc is None:
        return
    km = kc.keymaps.new(name="Mesh", space_type='EMPTY')
    for key, idname, mode in _EXTRA:
        kmi = km.keymap_items.new(idname, key, 'PRESS', shift=True, alt=True)
        if mode is not None:
            kmi.properties.mode = mode
        _extra_keymaps.append((km, kmi))


def _unregister_extra_keymaps():
    for km, kmi in _extra_keymaps:
        try:
            km.keymap_items.remove(kmi)
        except Exception:
            pass
    _extra_keymaps.clear()


def _apply_toggle():
    """Match the keymap to the saved preference.

    Called directly from ``register()`` and again from a one-shot timer, because
    on a cold start the addon keyconfig is sometimes not ready yet while on a
    manual enable it always is. ``keymaps.sync`` is idempotent, so running it
    twice is harmless; the timer simply catches the case the direct call missed.
    """
    try:
        prefs = ops._prefs()
        keymaps.sync(bool(prefs.override_grs) if prefs else True)
    except Exception:
        pass
    return None


def register():
    for cls in ui.CLASSES:
        bpy.utils.register_class(cls)
    for cls in ops.CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.hair_deform_edit = bpy.props.PointerProperty(
        type=ui.HairDeformSettings)
    ui.register_handlers()
    _register_extra_keymaps()
    _apply_toggle()
    bpy.app.timers.register(_apply_toggle, first_interval=0.0)


def unregister():
    keymaps.disable()
    _unregister_extra_keymaps()
    ui.unregister_handlers()
    if hasattr(bpy.types.Scene, "hair_deform_edit"):
        del bpy.types.Scene.hair_deform_edit
    for cls in reversed(ops.CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
    for cls in reversed(ui.CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
    # Remove any proxy left behind by a crashed modal run.
    for ob in list(bpy.data.objects):
        if ob.name.startswith(solver.PROXY_OBJECT_NAME):
            try:
                bpy.data.objects.remove(ob, do_unlink=True)
            except Exception:
                pass
    for me in list(bpy.data.meshes):
        if me.name.startswith(solver.PROXY_MESH_NAME):
            try:
                bpy.data.meshes.remove(me, do_unlink=True)
            except Exception:
                pass


if __name__ == "__main__":
    register()
