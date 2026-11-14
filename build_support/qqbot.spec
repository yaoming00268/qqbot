# Four executable roles embed precompiled modules and share the DLL tree.
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, copy_metadata, collect_submodules

root = Path(SPECPATH).parent
source_modules = [path.stem for path in root.glob("*.py")
                  if not path.name.startswith(("test_", "submit_"))
                  and path.stem not in {"conftest", "testing_sandbox", "renpy_bridge_agent"}]
resources = [(str(root / "console_tools/README.md"), "console_tools"),
             (str(root / "ui/web"), "ui/web"),
             (str(root / "console_tools/MAIN.md"), "console_tools"),
             (str(root / "console_tools/API.md"), "console_tools"),
             (str(root / "browser_extensions/gemini_connector"), "browser_extensions/gemini_connector"),
             (str(root / "renpy_bridge_agent.py"), "."), (str(root / "vm_guest"), "vm_guest"),
             (str(root / "config.yaml.example"), ".")]
resources += collect_data_files("rapidocr_onnxruntime")
resources += collect_data_files("webview", subdir="js")
resources += collect_data_files("webview", subdir="lib")
resources += copy_metadata("gemini-webapi")
resources += copy_metadata("curl-cffi")

a = Analysis([str(root / "build_support/entry.py")], pathex=[str(root)],
    binaries=collect_dynamic_libs("uiautomation"), datas=resources,
    hiddenimports=source_modules + ["console_tools.server", "console_tools.cli", "playwright.sync_api",
        "ui.webview_app", "ui.studio_api", "ui.window_effects", "webview.platforms.edgechromium"] + collect_submodules("agent_core"),
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=["torch", "torchvision", "tensorflow", "pandas", "scipy", "sympy", "matplotlib", "sklearn",
              "IPython", "notebook", "jupyter", "pytest", "tkinter", "PyQt5", "PyQt6", "PySide2",
              "matplotlib_inline", "debugpy", "ipykernel", "ipywidgets", "jupyter_client", "jupyter_core",
              "traitlets", "jedi", "parso", "eventlet", "gevent", "paramiko"],
    noarchive=False, optimize=0)
# Recent Qt uses Windows' unversioned ICU API. Conda's ICU 73 exposes suffixed
# symbols and shadows that system API if copied beside the executables.
# No other bundled dependency consumes ICU (verified during packaging).
a.binaries = [entry for entry in a.binaries if Path(entry[0]).name.casefold()
              not in {"icuuc.dll", "icudt73.dll"}]
pyz = PYZ(a.pure)
programs = [EXE(pyz, a.scripts, [], exclude_binaries=True, name=name, debug=False,
                bootloader_ignore_signals=False, strip=False, upx=False, console=console,
                contents_directory="_internal")
            for name, console in (("QQBot", True), ("QQBotStudio", False),
                                  ("QQBotCLI", True), ("QQBotMCP", True))]
suite = COLLECT(*programs, a.binaries, a.datas, strip=False, upx=False, name="QQBot")
