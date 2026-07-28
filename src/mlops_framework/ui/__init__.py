"""UI package — server-rendered HTML + vanilla JS Management UI.

The UI is a thin layer over the JSON API. It is mounted by
:func:`mlops_framework.api.app.create_app` when ``mount_ui=True``.

The full implementation lives in :mod:`mlops_framework.ui.mount`; this
package re-exports the public function.
"""

from mlops_framework.ui.mount import mount_ui

__all__ = ["mount_ui"]
