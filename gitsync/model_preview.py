"""Static model rendering and normal model opening helpers."""

from __future__ import annotations

import errno
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

from .git_service import MODEL_EXTENSIONS
from .qt_compat import QtGui, QtWidgets

try:  # pragma: no cover - only available inside FreeCAD
    import FreeCAD as App
except ImportError:  # pragma: no cover
    App = None

try:  # pragma: no cover - only available in the FreeCAD GUI
    import FreeCADGui as Gui
except ImportError:  # pragma: no cover
    Gui = None


class PreviewError(RuntimeError):
    """Raised when a model cannot be rendered."""


def is_supported_model(path: str) -> bool:
    return Path(path).suffix.lower() in MODEL_EXTENSIONS


class ModelPreview:
    """Render a model to a PNG-backed pixmap for the compact preview panel."""

    def __init__(self, apply_home: bool = True, allowed_root: Optional[str] = None) -> None:
        self.apply_home = apply_home
        self.allowed_root = allowed_root
        self._cache_path = Path(tempfile.mkdtemp(prefix="GitSyncPreview-"))
        try:
            os.chmod(str(self._cache_path), 0o700)
        except OSError:
            pass
        if self._cache_path.is_symlink() or not self._cache_path.is_dir():
            raise PreviewError("Unable to create a secure preview cache directory.")

    def _cache_dir(self) -> Path:
        return self._cache_path

    def _cleanup_cache(self) -> None:
        cutoff = time.time() - 7 * 24 * 60 * 60
        try:
            for item in self._cache_path.iterdir():
                if item.is_file() and item.stat().st_mtime < cutoff:
                    item.unlink()
        except OSError:
            pass

    def __del__(self):
        try:
            shutil.rmtree(str(self._cache_path), ignore_errors=True)
        except Exception:
            pass

    @staticmethod
    def _validate_path(path: str, allowed_root: Optional[str]) -> Path:
        candidate = Path(path).expanduser()
        if allowed_root:
            root = Path(allowed_root).expanduser().resolve()
            if not candidate.is_absolute():
                candidate = root / candidate
            try:
                raw_candidate = Path(os.path.abspath(str(candidate)))
                raw_relative = raw_candidate.relative_to(root)
            except ValueError as exc:
                raise PreviewError("The selected model is outside the Git workspace.") from exc
            current = root
            for part in raw_relative.parts:
                current = current / part
                if current.is_symlink():
                    raise PreviewError("Symbolic links are not allowed in GitSync model paths.")
            try:
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(root)
            except (OSError, ValueError) as exc:
                raise PreviewError("The selected model is not a regular workspace file.") from exc
            if not resolved.is_file():
                raise PreviewError("The selected model is not a regular file.")
            return resolved
        return candidate.resolve()

    @staticmethod
    def validate_model_path(path: str, allowed_root: Optional[str] = None) -> Path:
        """Public entry point for the shared path checks.

        Rejects paths outside the workspace and symlinks anywhere in the path,
        so every caller that hands a repository path to something outside
        FreeCAD goes through here.
        """
        return ModelPreview._validate_path(path, allowed_root)

    def _preview_target(self, model_path: Path, allowed_root: Optional[str] = None) -> Path:
        """Return the repository-relative PNG path for a model path."""
        model = self._validate_path(str(model_path), allowed_root or self.allowed_root)
        target = model.with_suffix(".png")
        root_value = allowed_root or self.allowed_root
        if root_value:
            root = Path(root_value).expanduser().resolve()
            try:
                raw_target = Path(os.path.abspath(str(target)))
                relative_target = raw_target.relative_to(root)
            except ValueError as exc:
                raise PreviewError("The preview path is outside the Git workspace.") from exc
            current = root
            for part in relative_target.parts:
                current = current / part
                if current.is_symlink():
                    raise PreviewError("Symbolic links are not allowed in GitSync preview paths.")
        if target.exists() and not target.is_file():
            raise PreviewError("The preview target is not a regular file: {}".format(target))
        return target

    @staticmethod
    def _persist_image(source: Path, target: Path) -> None:
        """Move a rendered PNG to the repo, including cross-device filesystems."""
        try:
            os.replace(str(source), str(target))
            return
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=".gitsync-preview-",
            suffix=".png",
            dir=str(target.parent),
        )
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            shutil.copyfile(str(source), str(temporary))
            os.replace(str(temporary), str(target))
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass

    def ensure_png(
        self,
        path: str,
        allowed_root: Optional[str] = None,
        force: bool = False,
        width: int = 640,
        height: int = 420,
    ):
        """Return a model pixmap and persist it beside the model as ``<stem>.png``."""
        root = allowed_root or self.allowed_root
        model = self._validate_path(path, root)
        target = self._preview_target(model, root)
        if not force:
            try:
                if target.is_file() and target.stat().st_mtime >= model.stat().st_mtime:
                    pixmap = QtGui.QPixmap(str(target))
                    if not pixmap.isNull():
                        return pixmap, False
            except OSError:
                pass
        pixmap = self.render_pixmap(
            str(model),
            width=width,
            height=height,
            allowed_root=root,
            persist_path=str(target),
        )
        return pixmap, True

    def refresh_pngs(
        self,
        model_paths,
        allowed_root: Optional[str] = None,
        force: bool = True,
        width: int = 640,
        height: int = 420,
        progress=None,
    ):
        """Regenerate repository PNGs and return written paths plus errors."""
        written = []
        errors = []
        values = list(model_paths)
        total = len(values)
        for index, value in enumerate(values, 1):
            relative = str(value)
            if progress is not None:
                try:
                    progress(index, total, relative)
                except Exception:
                    pass
            try:
                self.ensure_png(
                    relative,
                    allowed_root=allowed_root,
                    force=force,
                    width=width,
                    height=height,
                )
                model = self._validate_path(relative, allowed_root or self.allowed_root)
                target = self._preview_target(model, allowed_root or self.allowed_root)
                written.append(target)
            except Exception as exc:
                errors.append({"path": relative, "error": str(exc)})
        return {"written": written, "errors": errors}

    @staticmethod
    def _visible_view_widgets():
        """Return currently visible FreeCAD 3D view widgets."""
        if Gui is None:
            return set()
        try:
            main_window = Gui.getMainWindow()
            return {
                widget
                for widget in main_window.findChildren(QtWidgets.QWidget)
                if widget.metaObject().className() == "Gui::View3DInventor"
                and widget.isVisible()
            }
        except Exception:
            return set()

    @staticmethod
    def _hide_new_view_widgets(previous_widgets) -> None:
        """Hide temporary 3D views before the GUI can paint them."""
        if Gui is None:
            return
        try:
            main_window = Gui.getMainWindow()
            for widget in main_window.findChildren(QtWidgets.QWidget):
                if (
                    widget.metaObject().className() == "Gui::View3DInventor"
                    and widget.isVisible()
                    and widget not in previous_widgets
                ):
                    widget.hide()
        except Exception:
            # Rendering can still proceed through the View3DInventor object;
            # hiding the Qt widget is an isolation optimization.
            pass

    @staticmethod
    def _document_for_model(path: Path):
        if App is None or Gui is None:
            raise PreviewError("FreeCAD is not available for model preview.")
        extension = path.suffix.lower()
        if extension == ".fcstd":
            # Do not close or re-orient a document that the user already has
            # open.  The camera is restored by render_pixmap.
            try:
                existing = App.getDocumentByPath(str(path))
            except Exception:
                existing = None
            if existing is not None:
                return existing, False
            try:
                # A visible/temporary document is required to obtain a
                # View3DInventor instance, but the caller hides its Qt view
                # before the event loop can paint it.
                try:
                    return App.openDocument(str(path), False, True), True
                except TypeError:
                    return App.openDocument(str(path)), True
            except Exception as exc:
                raise PreviewError("Unable to open FreeCAD document: {}".format(exc)) from exc

        name = "GitSyncPreview_{}".format(uuid.uuid4().hex[:12])
        try:
            document = App.newDocument(name, "GitSync Preview")
        except TypeError:
            document = App.newDocument(name)
        try:
            if extension in {".step", ".stp", ".iges", ".igs"}:
                try:
                    import Import  # type: ignore

                    Import.insert(str(path), document.Name)
                except ImportError as exc:
                    raise PreviewError("FreeCAD Import support is unavailable.") from exc
            elif extension in {".stl", ".obj", ".ply"}:
                try:
                    import Mesh  # type: ignore

                    Mesh.insert(str(path), document.Name)
                except ImportError as exc:
                    raise PreviewError("FreeCAD Mesh support is unavailable.") from exc
            elif extension == ".brep":
                try:
                    import Part  # type: ignore

                    Part.insert(str(path), document.Name)
                except ImportError as exc:
                    raise PreviewError("FreeCAD Part support is unavailable.") from exc
            else:
                raise PreviewError("Unsupported model format: {}".format(extension))
            try:
                document.recompute()
            except Exception:
                pass
            return document, True
        except Exception:
            try:
                App.closeDocument(document.Name)
            except Exception:
                pass
            raise

    @staticmethod
    def _view_for_document(document):
        if Gui is None:
            return None
        try:
            gui_document = Gui.getDocument(document.Name)
            if gui_document is not None:
                view = getattr(gui_document, "ActiveView", None)
                if view is not None:
                    return view
        except Exception:
            pass
        return None

    @staticmethod
    def _set_home(view) -> None:
        if view is None:
            return
        try:
            view.viewIsometric()
        except Exception:
            try:
                view.viewAxonometric()
            except Exception:
                pass
        try:
            # The factor argument is accepted by current FreeCAD bindings and
            # avoids relying on deprecated global view messages.
            view.fitAll(1.0)
        except Exception:
            try:
                view.fitAll()
            except Exception:
                try:
                    # ViewFit is available in older FreeCAD releases.
                    from FreeCADGui import SendMsgToActiveView  # type: ignore

                    SendMsgToActiveView("ViewFit")
                except Exception:
                    pass

    @staticmethod
    def _save_image(view, output: Path, width: int, height: int) -> None:
        if view is None:
            raise PreviewError("The model did not produce a 3D view.")
        attempts = [
            (str(output), int(width), int(height), "White"),
            (str(output), int(width), int(height), "Current"),
            (str(output), int(width), int(height)),
        ]
        last_error: Optional[Exception] = None
        for args in attempts:
            try:
                view.saveImage(*args)
                if output.exists() and output.stat().st_size > 0:
                    return
            except Exception as exc:
                last_error = exc
        if last_error is not None:
            raise PreviewError("Unable to render preview: {}".format(last_error))
        raise PreviewError("FreeCAD did not produce a preview image.")

    def render_pixmap(
        self,
        path: str,
        width: int = 640,
        height: int = 420,
        allowed_root: Optional[str] = None,
        persist_path: Optional[str] = None,
    ) -> QtGui.QPixmap:
        """Render ``path`` without leaving a temporary document open."""
        if App is None or Gui is None:
            raise PreviewError("Model previews require the FreeCAD GUI.")
        model_path = self._validate_path(path, allowed_root or self.allowed_root)
        if not model_path.is_file():
            raise PreviewError("The selected model no longer exists: {}".format(model_path))
        if not is_supported_model(str(model_path)):
            raise PreviewError("Unsupported model format: {}".format(model_path.suffix))
        persist_target = None
        if persist_path:
            persist_target = self._preview_target(model_path, allowed_root or self.allowed_root)

        old_document = None
        try:
            old_document = App.ActiveDocument
        except Exception:
            old_document = None
        old_name = getattr(old_document, "Name", None)

        document = None
        close_document = False
        view = None
        old_camera = None
        before_documents = set()
        before_view_widgets = self._visible_view_widgets()
        try:
            before_documents = set(App.listDocuments().keys())
        except Exception:
            pass
        pixmap = QtGui.QPixmap()
        output = self._cache_dir() / "preview_{}_{}.png".format(
            uuid.uuid4().hex, int(time.time() * 1000)
        )
        try:
            document, close_document = self._document_for_model(model_path)
            if getattr(document, "Name", None) in before_documents:
                close_document = False
            try:
                view = self._view_for_document(document)
                if close_document:
                    self._hide_new_view_widgets(before_view_widgets)
                    if old_name:
                        try:
                            if App.getDocument(old_name) is not None:
                                App.setActiveDocument(old_name)
                        except Exception:
                            pass
                if view is not None:
                    try:
                        old_camera = view.getCamera()
                    except Exception:
                        old_camera = None
                if self.apply_home:
                    self._set_home(view)
                if Gui is not None:
                    try:
                        Gui.updateGui()
                    except Exception:
                        pass
                self._save_image(view, output, width, height)
                image_path = output
                if persist_target is not None:
                    self._persist_image(output, persist_target)
                    image_path = persist_target
                pixmap = QtGui.QPixmap(str(image_path))
            except PreviewError:
                raise
            except Exception as exc:
                raise PreviewError("Unable to render preview: {}".format(exc)) from exc
        finally:
            # Restore a user's camera when an already-open document was used.
            if view is not None and old_camera is not None:
                try:
                    view.setCamera(old_camera)
                except Exception:
                    pass
            if document is not None and close_document:
                try:
                    App.closeDocument(document.Name)
                except Exception:
                    pass
            # FCStd files can open dependency documents.  Close only documents
            # created by this preview; never touch the user's existing set.
            try:
                for name in list(App.listDocuments().keys()):
                    if name not in before_documents and name != getattr(document, "Name", None):
                        App.closeDocument(name)
            except Exception:
                pass
            if old_name:
                try:
                    if App.getDocument(old_name) is not None:
                        App.setActiveDocument(old_name)
                except Exception:
                    pass
            try:
                output.unlink()
            except OSError:
                pass

        self._cleanup_cache()
        if pixmap.isNull():
            raise PreviewError("The rendered preview image could not be loaded.")
        return pixmap

    @staticmethod
    def _open_imported_model(model_path: Path):
        """Open an exchange-format model in a normal editable document."""
        extension = model_path.suffix.lower()
        name = "GitSyncOpen_{}".format(uuid.uuid4().hex[:12])
        try:
            document = App.newDocument(name, model_path.stem)
        except Exception as exc:
            raise PreviewError("Unable to create a document for the model: {}".format(exc)) from exc
        try:
            if extension in {".step", ".stp", ".iges", ".igs"}:
                try:
                    import ImportGui  # type: ignore
                except ImportError:
                    ImportGui = None
                if ImportGui is not None:
                    ImportGui.insert(str(model_path), document.Name)
                else:
                    import Import  # type: ignore

                    Import.insert(str(model_path), document.Name)
            elif extension in {".stl", ".obj", ".ply"}:
                import Mesh  # type: ignore

                Mesh.insert(str(model_path), document.Name)
            elif extension == ".brep":
                import Part  # type: ignore

                Part.insert(str(model_path), document.Name)
            else:
                raise PreviewError("Unsupported model format: {}".format(extension))
            document.recompute()
            App.setActiveDocument(document.Name)
            if Gui is not None:
                try:
                    Gui.updateGui()
                except Exception:
                    pass
            return document
        except Exception:
            try:
                App.closeDocument(document.Name)
            except Exception:
                pass
            raise

    @staticmethod
    def open_model(path: str, allowed_root: Optional[str] = None):
        """Open a selected repository model as a normal FreeCAD document."""
        if App is None:
            raise PreviewError("FreeCAD is not available.")
        model_path = ModelPreview._validate_path(path, allowed_root)
        if not model_path.is_file():
            raise PreviewError("The selected model no longer exists: {}".format(model_path))
        if not is_supported_model(str(model_path)):
            raise PreviewError("Unsupported model format: {}".format(model_path.suffix))
        if model_path.suffix.lower() == ".fcstd":
            # Gui.open() dispatches generic file handlers and does not accept
            # FCStd documents in FreeCAD 26.3.  App.openDocument is the
            # supported API for native FreeCAD files.
            try:
                return App.openDocument(str(model_path))
            except Exception as exc:
                raise PreviewError("Unable to open FreeCAD document: {}".format(exc)) from exc
        return ModelPreview._open_imported_model(model_path)


__all__ = ["PreviewError", "ModelPreview", "is_supported_model"]
