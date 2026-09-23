# SPDX-License-Identifier: GPL-2.0-or-later
"""The always-on toggle: rebind G / R / S in the Mesh keymap.

Addon keymap items are inserted ahead of Blender's own, so ours gets the key
first. Crucially the operator's ``poll`` returns False when the object has no
live deform modifier, and Blender then falls through to the next match - the
stock ``transform.translate``. So the toggle costs nothing on ordinary meshes
and needs no bookkeeping to "restore" the default behaviour.
"""

import bpy

_items = []


def _refresh():
    """Force Blender to re-merge the keyconfigs.

    Without this the addon keymap exists but the merged 'user' keyconfig that
    actually dispatches events is stale, so the new binding does nothing until
    something else triggers a rebuild.
    """
    try:
        bpy.context.window_manager.keyconfigs.update()
    except Exception:
        pass

# key -> (operator mode, description)
_BINDINGS = (
    ('G', 'TRANSLATE'),
    ('R', 'ROTATE'),
    ('S', 'RESIZE'),
)


def is_active():
    return bool(_items)


def enable():
    """Install the G/R/S overrides. Safe to call twice."""
    if _items:
        return True
    kc = bpy.context.window_manager.keyconfigs.addon
    if kc is None:
        return False
    km = kc.keymaps.new(name="Mesh", space_type='EMPTY')
    for key, mode in _BINDINGS:
        kmi = km.keymap_items.new("hair_deform_edit.transform", key, 'PRESS')
        kmi.properties.mode = mode
        _items.append((km, kmi))
    _refresh()
    return True


def disable():
    """Remove the overrides, restoring stock G/R/S."""
    for km, kmi in _items:
        try:
            km.keymap_items.remove(kmi)
        except Exception:
            pass
    _items.clear()
    _refresh()


def sync(enabled):
    if enabled:
        enable()
    else:
        disable()
    return is_active()
