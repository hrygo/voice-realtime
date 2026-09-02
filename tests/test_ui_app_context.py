def test_context_module_exposes_typed_context() -> None:
    from sona.ui.app_context import UIAppContext

    assert UIAppContext.__name__ == "UIAppContext"
