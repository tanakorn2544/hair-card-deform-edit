# SPDX-License-Identifier: GPL-2.0-or-later
"""Jump Card <-> Curve.

One key (Shift+Alt+C) goes from the selected cards to the curves that bend
them (their Curve modifiers), and from a curve back to its cards. The mode is
kept: from card Edit Mode you land in curve Edit Mode, from Object Mode in
Object Mode. The card's own vertex selection is left alone, so jumping back
puts you where you were.

Curves are often hidden to keep the view clean. A hidden curve is shown for
the jump and hidden again when you jump back from it.
"""

import bpy
from bpy.props import BoolProperty

# curve name -> {"cards": [names], "hide": bool, "hide_viewport": bool,
#                "hide_select": bool}
# Kept for this Blender session only.
_came_from = {}


def _card_curves(ob):
    """Curve objects that bend this card (visible Curve modifiers)."""
    out = []
    for m in ob.modifiers:
        if (m.type == 'CURVE' and m.show_viewport and m.object is not None
                and m.object.type == 'CURVE' and m.object not in out):
            out.append(m.object)
    return out


def _curve_cards(curve, scene):
    """Mesh objects in this scene with a Curve modifier using ``curve``."""
    out = []
    for ob in scene.objects:
        if ob.type != 'MESH':
            continue
        for m in ob.modifiers:
            if m.type == 'CURVE' and m.object == curve:
                out.append(ob)
                break
    return out


def _sources(context, kind):
    """The objects to jump from: those in Edit Mode, or the selection."""
    if context.mode.startswith('EDIT'):
        obs = list(getattr(context, "objects_in_mode_unique_data", None)
                   or [context.edit_object])
    else:
        obs = list(context.selected_objects)
        act = context.view_layer.objects.active
        if act is not None and act not in obs and act.select_get():
            obs.append(act)
    return [o for o in obs if o is not None and o.type == kind]


def _make_reachable(ob, view_layer):
    """Show and unlock an object so it can be selected. Returns what was
    changed so it can be put back."""
    was = {"hide": ob.hide_get(view_layer=view_layer),
           "hide_viewport": ob.hide_viewport,
           "hide_select": ob.hide_select}
    if was["hide_viewport"]:
        ob.hide_viewport = False
    if was["hide"]:
        ob.hide_set(False, view_layer=view_layer)
    if was["hide_select"]:
        ob.hide_select = False
    return was


def _restore_hidden(ob, was, view_layer):
    if was.get("hide_select"):
        ob.hide_select = True
    if was.get("hide"):
        ob.hide_set(True, view_layer=view_layer)
    if was.get("hide_viewport"):
        ob.hide_viewport = True


def _switch(context, targets, edit):
    """Leave the current mode, select ``targets`` and enter the mode."""
    vl = context.view_layer
    if context.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for o in context.selected_objects:
        o.select_set(False)
    for o in targets:
        o.select_set(True)
    vl.objects.active = targets[0]
    if edit:
        bpy.ops.object.mode_set(mode='EDIT')


class HAIRDEFORM_OT_jump_curve(bpy.types.Operator):
    bl_idname = "hair_deform_edit.jump_curve"
    bl_label = "Jump Card / Curve"
    bl_description = (
        "From the selected cards, go to the curves that bend them. From a "
        "curve, go back to its cards. Keeps Edit or Object Mode. "
        "Shortcut: Shift+Alt+C")
    bl_options = {'REGISTER', 'UNDO'}

    frame: BoolProperty(
        name="Frame", default=False,
        description="Zoom the view to what you jumped to")

    @classmethod
    def poll(cls, context):
        ob = context.active_object
        return (context.area is not None and context.area.type == 'VIEW_3D'
                and ob is not None and ob.type in {'MESH', 'CURVE'}
                and context.mode in {'OBJECT', 'EDIT_MESH', 'EDIT_CURVE'})

    def execute(self, context):
        edit = context.mode != 'OBJECT'
        if context.active_object.type == 'MESH':
            return self._to_curve(context, edit)
        return self._to_card(context, edit)

    # -- card -> curve ------------------------------------------------------
    def _to_curve(self, context, edit):
        vl = context.view_layer
        cards = _sources(context, 'MESH')
        curves, users = [], {}
        for c in cards:
            for cu in _card_curves(c):
                if cu not in curves:
                    curves.append(cu)
                users.setdefault(cu.name, []).append(c.name)
        if not curves:
            self.report({'WARNING'}, "No Curve modifier with a curve on the "
                        "selected card(s)")
            return {'CANCELLED'}

        reach, lost = [], []
        for cu in curves:
            prev = _came_from.get(cu.name)
            was = _make_reachable(cu, vl)
            if prev is not None and not any(was.values()):
                # already shown by an earlier jump: keep what it will restore
                was = {k: prev.get(k, False)
                       for k in ("hide", "hide_viewport", "hide_select")}
            if not cu.visible_get(view_layer=vl):
                _restore_hidden(cu, was, vl)
                lost.append(cu.name)
                continue
            _came_from[cu.name] = dict(was, cards=users[cu.name])
            reach.append(cu)
        if not reach:
            self.report({'WARNING'}, "Curve %s is in a hidden or excluded "
                        "collection" % ", ".join(lost))
            return {'CANCELLED'}

        _switch(context, reach, edit)
        if self.frame:
            self._frame(context, edit)
        msg = "Curve: " + ", ".join(c.name for c in reach)
        if lost:
            self.report({'WARNING'}, msg + " (%s is in a hidden collection)"
                        % ", ".join(lost))
        else:
            self.report({'INFO'}, msg)
        return {'FINISHED'}

    # -- curve -> card ------------------------------------------------------
    def _to_card(self, context, edit):
        vl = context.view_layer
        scene = context.scene
        curves = _sources(context, 'CURVE')
        cards, back = [], []
        for cu in curves:
            users = _curve_cards(cu, scene)
            memo = _came_from.get(cu.name)
            if memo is not None:
                named = [o for o in users if o.name in memo["cards"]]
                if named:
                    users = named
            for o in users:
                if o not in cards and o.visible_get(view_layer=vl):
                    cards.append(o)
            if memo is not None:
                back.append((cu, memo))
        if not cards:
            self.report({'WARNING'}, "No visible card uses this curve")
            return {'CANCELLED'}

        _switch(context, cards, edit)
        for cu, memo in back:
            _came_from.pop(cu.name, None)
            _restore_hidden(cu, memo, vl)
        if self.frame:
            self._frame(context, edit)
        self.report({'INFO'}, "Card: " + ", ".join(c.name for c in cards))
        return {'FINISHED'}

    def _frame(self, context, edit):
        try:
            if edit:
                bpy.ops.object.mode_set(mode='OBJECT')
            bpy.ops.view3d.view_selected()
            if edit:
                bpy.ops.object.mode_set(mode='EDIT')
        except Exception:
            pass


def menu_func(self, context):
    self.layout.operator(HAIRDEFORM_OT_jump_curve.bl_idname,
                         icon='CURVE_BEZCURVE')


# Shift+Alt+C is free in every default keymap in Blender 4.2.
_KEYMAPS = ("Object Mode", "Mesh", "Curve")
_items = []


def register_keymaps():
    kc = bpy.context.window_manager.keyconfigs.addon
    if kc is None:
        return
    for name in _KEYMAPS:
        km = kc.keymaps.new(name=name, space_type='EMPTY')
        kmi = km.keymap_items.new(HAIRDEFORM_OT_jump_curve.bl_idname, 'C',
                                  'PRESS', shift=True, alt=True)
        _items.append((km, kmi))


def unregister_keymaps():
    for km, kmi in _items:
        try:
            km.keymap_items.remove(kmi)
        except Exception:
            pass
    _items.clear()


CLASSES = (HAIRDEFORM_OT_jump_curve,)
