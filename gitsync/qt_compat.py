"""Qt compatibility helpers for GitSync.

FreeCAD has used both the ``PySide`` shim and directly shipped PySide6 in
recent releases.  Keeping the imports in one small module lets the rest of
the workbench remain readable and makes the non-GUI Git code usable by tests
without importing Qt.
"""

try:  # FreeCAD's preferred compatibility module
    from PySide import QtCore, QtGui, QtWidgets
except ImportError:  # pragma: no cover - depends on the FreeCAD build
    try:
        from PySide6 import QtCore, QtGui, QtWidgets
    except ImportError:  # pragma: no cover - older FreeCAD builds
        from PySide2 import QtCore, QtGui, QtWidgets


def enum_value(owner, *names, default=None):
    """Return the first available enum member from a Qt enum owner.

    PySide2 exposes enum members directly on ``Qt.*`` while PySide6 often
    exposes them below a nested enum class.  This helper avoids scattering
    version checks throughout the UI code.
    """
    for name in names:
        if hasattr(owner, name):
            return getattr(owner, name)
    return default


def user_role():
    """Return Qt's user-data role across PySide versions."""
    value = enum_value(QtCore.Qt, "UserRole")
    if value is not None:
        return value
    return QtCore.Qt.ItemDataRole.UserRole


def exec_dialog(dialog):
    """Execute a dialog on both PySide2 and PySide6."""
    method = getattr(dialog, "exec", None)
    if method is None:
        method = dialog.exec_
    return method()


def button_ok():
    value = enum_value(QtWidgets.QDialogButtonBox, "Ok")
    if value is not None:
        return value
    return QtWidgets.QDialogButtonBox.StandardButton.Ok


def button_cancel():
    value = enum_value(QtWidgets.QDialogButtonBox, "Cancel")
    if value is not None:
        return value
    return QtWidgets.QDialogButtonBox.StandardButton.Cancel


def message_yes():
    value = enum_value(QtWidgets.QMessageBox, "Yes")
    if value is not None:
        return value
    return QtWidgets.QMessageBox.StandardButton.Yes


def message_no():
    value = enum_value(QtWidgets.QMessageBox, "No")
    if value is not None:
        return value
    return QtWidgets.QMessageBox.StandardButton.No


def message_ok():
    value = enum_value(QtWidgets.QMessageBox, "Ok")
    if value is not None:
        return value
    return QtWidgets.QMessageBox.StandardButton.Ok


def dialog_accepted():
    value = enum_value(QtWidgets.QDialog, "Accepted")
    if value is not None:
        return value
    return QtWidgets.QDialog.DialogCode.Accepted


def extended_selection():
    value = enum_value(QtWidgets.QAbstractItemView, "ExtendedSelection")
    if value is not None:
        return value
    return QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection


def align_center():
    value = enum_value(QtCore.Qt, "AlignCenter")
    if value is not None:
        return value
    return QtCore.Qt.AlignmentFlag.AlignCenter


def item_is_user_checkable():
    value = enum_value(QtCore.Qt, "ItemIsUserCheckable")
    if value is not None:
        return value
    return QtCore.Qt.ItemFlag.ItemIsUserCheckable


def checked_state():
    value = enum_value(QtCore.Qt, "Checked")
    if value is not None:
        return value
    return QtCore.Qt.CheckState.Checked


def unchecked_state():
    value = enum_value(QtCore.Qt, "Unchecked")
    if value is not None:
        return value
    return QtCore.Qt.CheckState.Unchecked


def wait_cursor():
    value = enum_value(QtCore.Qt, "WaitCursor")
    if value is not None:
        return value
    return QtCore.Qt.CursorShape.WaitCursor


def keep_aspect_ratio():
    value = enum_value(QtCore.Qt, "KeepAspectRatio")
    if value is not None:
        return value
    return QtCore.Qt.AspectRatioMode.KeepAspectRatio


def smooth_transformation():
    value = enum_value(QtCore.Qt, "SmoothTransformation")
    if value is not None:
        return value
    return QtCore.Qt.TransformationMode.SmoothTransformation


def dock_area(name: str):
    value = enum_value(QtCore.Qt, name)
    if value is not None:
        return value
    return getattr(QtCore.Qt.DockWidgetArea, name)


def expanding_policy():
    value = getattr(QtWidgets.QSizePolicy, "Expanding", None)
    if value is not None:
        return value
    return QtWidgets.QSizePolicy.Policy.Expanding


def fixed_policy():
    value = getattr(QtWidgets.QSizePolicy, "Fixed", None)
    if value is not None:
        return value
    return QtWidgets.QSizePolicy.Policy.Fixed


def header_stretch_mode():
    """Return the header resize mode that gives a column all spare width."""
    value = enum_value(QtWidgets.QHeaderView, "Stretch")
    if value is not None:
        return value
    return QtWidgets.QHeaderView.ResizeMode.Stretch


def header_contents_mode():
    """Return the header resize mode that sizes a column to its content."""
    value = enum_value(QtWidgets.QHeaderView, "ResizeToContents")
    if value is not None:
        return value
    return QtWidgets.QHeaderView.ResizeMode.ResizeToContents


def header_fixed_mode():
    """Return the header resize mode that keeps a column at a set width."""
    value = enum_value(QtWidgets.QHeaderView, "Fixed")
    if value is not None:
        return value
    return QtWidgets.QHeaderView.ResizeMode.Fixed


def relative_luminance(color) -> float:
    """Return the perceived brightness of a colour, 0.0 (black) to 1.0 (white)."""
    return 0.2126 * color.redF() + 0.7152 * color.greenF() + 0.0722 * color.blueF()


def palette_is_dark(palette) -> bool:
    """True when a widget's palette is a dark one.

    Used to decide whether GitSync has to supply its own light tree arrows:
    a theme's branch glyph can be light-on-light-by-antialiasing and all but
    disappear against a dark row.
    """
    return relative_luminance(palette.color(QtGui.QPalette.Base)) < 0.5


def custom_context_menu_policy():
    """Return the widget context-menu policy that routes clicks to a handler."""
    value = enum_value(QtCore.Qt, "CustomContextMenu")
    if value is not None:
        return value
    return QtCore.Qt.ContextMenuPolicy.CustomContextMenu


def line_edit_password_mode():
    value = enum_value(QtWidgets.QLineEdit, "Password")
    if value is not None:
        return value
    return QtWidgets.QLineEdit.EchoMode.Password


def line_edit_normal_mode():
    value = enum_value(QtWidgets.QLineEdit, "Normal")
    if value is not None:
        return value
    return QtWidgets.QLineEdit.EchoMode.Normal


def text_selectable_by_mouse():
    value = enum_value(QtCore.Qt, "TextSelectableByMouse")
    if value is not None:
        return value
    return QtCore.Qt.TextInteractionFlag.TextSelectableByMouse


def plain_text_format():
    value = enum_value(QtCore.Qt, "PlainText")
    if value is not None:
        return value
    return QtCore.Qt.TextFormat.PlainText


def set_plain_text(widget, text: str) -> None:
    widget.setTextFormat(plain_text_format())
    widget.setText(str(text))


def message_icon(name: str):
    value = getattr(QtWidgets.QMessageBox, name, None)
    if value is not None:
        return value
    owner = getattr(QtWidgets.QMessageBox, "Icon", QtWidgets.QMessageBox)
    return getattr(owner, name)


def message_buttons(*buttons):
    """Combine standard button flags in a way PySide2 and PySide6 both accept.

    A bare ``StandardButton`` member is not accepted by
    ``QMessageBox.setStandardButtons`` in PySide6, so the combined value is
    passed through the ``StandardButtons`` flag constructor when it exists.
    """
    combined = None
    for button in buttons:
        if button is None:
            continue
        combined = button if combined is None else combined | button
    if combined is None:
        return None
    try:
        return QtWidgets.QMessageBox.StandardButtons(combined)
    except Exception:
        return combined


def show_plain_message(parent, icon, title: str, text: str, buttons=None, default=None):
    """Show a non-rich-text message box and return its result."""
    box = QtWidgets.QMessageBox(parent)
    box.setIcon(icon)
    box.setWindowTitle(str(title))
    box.setTextFormat(plain_text_format())
    box.setText(str(text))
    if buttons is not None:
        box.setStandardButtons(message_buttons(buttons))
    else:
        # A QMessageBox needs its own button enum; passing a QDialogButtonBox
        # member is rejected by PySide6 even though the numeric value matches.
        box.setStandardButtons(message_buttons(message_ok()))
    if default is not None:
        box.setDefaultButton(default)
    return exec_dialog(box)


def show_trust_prompt(parent, icon, title: str, text: str) -> str:
    """Ask once, with the option to trust the source permanently.

    Returns ``"yes"`` (this session only), ``"trust"`` (remember the choice for
    the current repository), or ``"no"``.
    """
    accept_role = enum_value(QtWidgets.QMessageBox, "AcceptRole")
    if accept_role is None:
        accept_role = enum_value(QtWidgets.QMessageBox.ButtonRole, "AcceptRole")
    box = QtWidgets.QMessageBox(parent)
    box.setIcon(icon)
    box.setWindowTitle(str(title))
    box.setTextFormat(plain_text_format())
    box.setText(str(text))
    box.setStandardButtons(message_buttons(message_yes(), message_no()))
    box.setDefaultButton(message_no())
    trust_button = box.addButton("Trust this repository", accept_role)
    exec_dialog(box)
    clicked = box.clickedButton()
    if clicked is trust_button:
        return "trust"
    if clicked is not None and clicked is box.button(message_yes()):
        return "yes"
    return "no"
